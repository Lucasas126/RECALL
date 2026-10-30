from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config.settings import TELEGRAM_ALLOWED_USERS_ENV
from src.providers.catalog import CatalogUnavailable
from src.service.chat import ChatService, ServiceError
from src.utils.logger import get_logger

logger = get_logger("telegram")
_MODELS_PER_PAGE = 10
_PREVIOUS_PAGE = "⬅️ Previous"
_NEXT_PAGE = "➡️ Next"


@dataclass
class TelegramSession:
    user_id: str
    agent_id: str
    chat_id: str | None = None
    model_options: dict[str, str] = field(default_factory=dict)


@dataclass
class TelegramBotState:
    service: ChatService
    sessions: dict[int, TelegramSession] = field(default_factory=dict)


def load_allowed_users() -> dict[int, str]:
    """Load Telegram IDs mapped to existing RECALL user IDs from the environment."""
    raw = os.getenv(TELEGRAM_ALLOWED_USERS_ENV, "")
    allowed: dict[int, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            telegram_id, user_id = (part.strip() for part in entry.split("=", 1))
            parsed_id = int(telegram_id)
        except (ValueError, TypeError):
            logger.warning("Ignoring invalid Telegram user mapping: %r", entry)
            continue
        if not user_id:
            logger.warning("Ignoring Telegram mapping without a RECALL user ID: %r", entry)
            continue
        allowed[parsed_id] = user_id
    return allowed


def _session(update: Update, state: TelegramBotState) -> TelegramSession | None:
    user = update.effective_user
    return state.sessions.get(user.id) if user else None


async def _unauthorized(update: Update) -> None:
    user = update.effective_user
    if user:
        logger.info("Ignoring unauthorized Telegram user_id=%s", user.id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    await update.effective_message.reply_text(
        "RECALL is ready.\n\n"
        "Use /newchat to start a conversation and /selectmodel to choose a model.\n"
        "This phase is currently focused on testing the Telegram flow."
    )


async def new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    chat = state.service.create_chat(session.user_id, session.agent_id)
    session.chat_id = chat["id"]
    session.model_options.clear()
    context.user_data.pop("model_options", None)
    context.user_data.pop("model_catalog", None)
    context.user_data.pop("model_page", None)
    await update.effective_message.reply_text(
        f"New chat created: {chat['id']}\n"
        "You can now choose a model or send your first message."
    )


def _price_key(value: str | None) -> tuple[int, Decimal]:
    if value is None:
        return (1, Decimal("Infinity"))
    try:
        return (0, Decimal(value))
    except InvalidOperation:
        return (1, Decimal("Infinity"))


def _sort_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        models,
        key=lambda model: (
            _price_key(model.get("pricing", {}).get("prompt")),
            _price_key(model.get("pricing", {}).get("completion")),
            model["name"].casefold(),
            model["id"],
        ),
    )


async def _show_model_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: TelegramSession,
) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    models = context.user_data["model_catalog"]
    page = context.user_data["model_page"]
    page_count = (len(models) + _MODELS_PER_PAGE - 1) // _MODELS_PER_PAGE
    page = max(0, min(page, page_count - 1))
    context.user_data["model_page"] = page
    current_model = state.service.get_chat(
        session.user_id, session.agent_id, session.chat_id,
    )["model"]["id"]
    page_models = models[
        page * _MODELS_PER_PAGE:(page + 1) * _MODELS_PER_PAGE
    ]
    model_options: dict[str, str] = {}
    keyboard: list[list[str]] = []
    for index, model in enumerate(page_models, page * _MODELS_PER_PAGE + 1):
        label = (
            f"{'✅ ' if model['id'] == current_model else ''}"
            f"{index}. {model['name'][:28]} | "
            f"${model['pricing'].get('prompt') or 'n/d'} in / "
            f"${model['pricing'].get('completion') or 'n/d'} out"
        )
        model_options[label] = model["id"]
        keyboard.append([label])
    navigation = []
    if page > 0:
        navigation.append(_PREVIOUS_PAGE)
    if page < page_count - 1:
        navigation.append(_NEXT_PAGE)
    if navigation:
        keyboard.append(navigation)
    context.user_data["model_options"] = model_options
    current_label = next(
        (model["name"] for model in models if model["id"] == current_model),
        current_model,
    )
    await update.effective_message.reply_text(
        f"Current model: {current_label}\n\n"
        f"Page {page + 1}/{page_count}. "
        f"{len(models)} models available.\n"
        "Choose another model. Prices are USD per 1 million tokens "
        "(input / output). ZDR enabled:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard, resize_keyboard=True, one_time_keyboard=True,
        ),
    )


async def select_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    if session.chat_id is None:
        await update.effective_message.reply_text(
            "Create a chat first with /newchat."
        )
        return

    query = " ".join(context.args).strip()
    try:
        models = _sort_models(state.service.list_models(session.user_id, query, zdr=True))
    except (CatalogUnavailable, ServiceError, ValueError) as exc:
        logger.warning("Could not load Telegram model options: %s", exc)
        await update.effective_message.reply_text(
            "I cannot load the models right now. Please try again in a few seconds."
        )
        return
    if not models:
        await update.effective_message.reply_text(
            "No ZDR models found. Try /selectmodel followed by part of a model name."
        )
        return
    context.user_data["model_catalog"] = models
    context.user_data["model_page"] = 0
    await _show_model_page(update, context, session)


async def model_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    action = (update.effective_message.text or "").strip()
    if action not in {_PREVIOUS_PAGE, _NEXT_PAGE}:
        return False
    models = context.user_data.get("model_catalog")
    if not models:
        return False
    page = context.user_data.get("model_page", 0)
    context.user_data["model_page"] = page + (1 if action == _NEXT_PAGE else -1)
    session = _session(update, context.application.bot_data["state"])
    if session is None or session.chat_id is None:
        await _unauthorized(update)
        return True
    await _show_model_page(update, context, session)
    return True


async def model_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    selected_label = (update.effective_message.text or "").strip()
    model_options = context.user_data.get("model_options", {})
    model_id = model_options.get(selected_label)
    if model_id is None:
        return False

    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None or session.chat_id is None:
        await update.effective_message.reply_text(
            "The session has expired. Use /newchat.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return True
    try:
        state.service.set_chat_model(
            session.user_id, session.agent_id, session.chat_id, model_id, zdr=True,
        )
    except (CatalogUnavailable, ServiceError, ValueError) as exc:
        logger.warning("Could not select Telegram model %r: %s", model_id, exc)
        await update.effective_message.reply_text(
            "That model could not be selected.\n"
            "Use /selectmodel to try again.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return True
    session.model_options.clear()
    context.user_data.pop("model_options", None)
    context.user_data.pop("model_catalog", None)
    context.user_data.pop("model_page", None)
    await update.effective_message.reply_text(
        f"✅ Model selected\n\n"
        f"{model_id}\n\n"
        "This selection applies to the current chat.\n"
        "ZDR: enabled",
        reply_markup=ReplyKeyboardRemove(),
    )
    return True


async def show_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    if session.chat_id is None:
        await update.effective_message.reply_text("No active chat. Use /newchat.")
        return
    try:
        chat = state.service.get_chat(session.user_id, session.agent_id, session.chat_id)
    except (ServiceError, ValueError) as exc:
        logger.warning("Could not read Telegram chat model: %s", exc)
        await update.effective_message.reply_text("I could not read the current chat model.")
        return
    model = chat["model"]
    await update.effective_message.reply_text(
        f"Current chat model:\n{model['id']}\n\n"
        f"ZDR: {'enabled' if model.get('zdr', True) else 'disabled'}"
    )


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    if await model_page(update, context):
        return
    if await model_selected(update, context):
        return
    await update.effective_message.reply_text(
        "I received your message. Model responses will be enabled in the next phase."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram handler failed: %s", context.error, exc_info=context.error)


def build_application(token: str, service: ChatService | None = None) -> Application:
    """Build the basic Telegram application with its channel-local state."""
    allowed = load_allowed_users()
    state = TelegramBotState(service or ChatService())
    for telegram_id, user_id in allowed.items():
        try:
            profile = state.service.login(user_id)
            if not profile["agents"]:
                raise ValueError("user has no agents")
            state.sessions[telegram_id] = TelegramSession(user_id, profile["agents"][0]["id"])
        except (OSError, ServiceError, ValueError) as exc:
            logger.warning("Ignoring invalid Telegram mapping %s=%s: %s", telegram_id, user_id, exc)

    application = ApplicationBuilder().token(token).build()
    application.bot_data["state"] = state
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("newchat", new_chat))
    application.add_handler(CommandHandler("selectmodel", select_model))
    application.add_handler(CommandHandler("model", show_model))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.add_error_handler(error_handler)
    logger.info("Telegram bot ready with %d approved users", len(state.sessions))
    return application
