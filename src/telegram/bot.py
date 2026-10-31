from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import telegramify_markdown
from telegram import (
    BotCommand, MessageEntity,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, Update,
)
from telegram.constants import ChatType
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from src.config.favorites import FavoritesError
from src.config.preferences import PreferencesError, UserPreferences
from src.config.settings import TELEGRAM_ALLOWED_USERS_ENV
from src.providers.catalog import CatalogUnavailable
from src.service.chat import ChatEvent, ChatService, ServiceError
from src.utils.logger import get_logger
from src.utils.i18n import InputError, error_message, message

logger = get_logger("telegram")
_MODELS_PER_PAGE = 10
_PREVIOUS_PAGE = "⬅️ Previous"
_NEXT_PAGE = "➡️ Next"
_STT_PREVIOUS_PAGE = "⬅️ Previous STT"
_STT_NEXT_PAGE = "➡️ Next STT"
_MAX_TELEGRAM_FILE_BYTES = 22_000_000
_MAX_TELEGRAM_MESSAGE_CHARS = 4_090
_CHAT_IDLE_TIMEOUT = timedelta(hours=1)


@dataclass
class TelegramSession:
    user_id: str
    agent_id: str
    chat_id: str | None = None
    model_options: dict[str, str] = field(default_factory=dict)
    agent_options: dict[str, str] = field(default_factory=dict)
    last_message_at: datetime | None = None
    pending_new_chat_notice: bool = False
    language: str = "en"


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


def _msg(session: TelegramSession | None, template: str, **values: object) -> str:
    return message(session.language if session else "en", template, **values)


async def refresh_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        return
    text = update.effective_message.text if update.effective_message else None
    if text and text.startswith("/"):
        context.user_data.pop("settings_options", None)
    try:
        session.language = state.service.language(session.user_id)
    except (PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Could not load Telegram notification language: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "Could not load settings. Try /settings again."),
        )
        raise ApplicationHandlerStop from exc


async def _unauthorized(update: Update) -> None:
    user = update.effective_user
    if user:
        logger.info("Ignoring unauthorized Telegram user_id=%s", user.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    if _session(update, state) is None:
        await _unauthorized(update)
        return
    await update.effective_message.reply_text(
        "Available commands:\n\n"
        "/settings — Thinking, language, and active agent\n"
        "/newchat — Start a new chat for the current agent\n"
        "/selectagent — Choose the active agent\n"
        "/agent — Show the active agent\n"
        "/selectmodel — Browse models, prices, and pages\n"
        "/model — Show the model selected for the current chat\n"
        "/selectstt — Choose the speech-to-text model\n"
        "/stt — Show the current speech-to-text model\n"
        "/help — Show this help\n\n"
        "You can send text, images, audio, voice messages, and video directly."
    )


def _settings_view(
    service: ChatService, session: TelegramSession,
) -> tuple[str, ReplyKeyboardMarkup, dict[str, str]]:
    preferences = service.get_preferences(session.user_id)
    agents = service.list_agents(session.user_id)
    language = preferences["language"]
    agent_name = next(
        (agent["name"] for agent in agents if agent["id"] == session.agent_id),
        message(language, "None"),
    )
    visible = message(language, "visible" if preferences["show_thinking"] else "hidden")
    thinking_label = message(language, "Thinking: {visibility}", visibility=visible)
    agent_label = message(language, "Active agent")
    llm_label = message(language, "Default LLM model")
    stt_label = message(language, "Default STT model")
    close_label = message(language, "Close")
    options = {thinking_label: "thinking", agent_label: "agents", close_label: "close"}
    options[llm_label] = "default_llm"
    options[stt_label] = "default_stt"
    languages = []
    for code, label in (("en", "English"), ("es", "Español")):
        label = f"{'✅ ' if language == code else ''}{label}"
        options[label] = f"language:{code}"
        languages.append(label)
    return (
        message(language, "Settings\n\nThinking: {visibility}\nAlerts/errors: {language}\nActive agent: {agent}\nDefault LLM: {llm}\nDefault STT: {stt}\n\nUser defaults are shared with the web. Chats with no selected model inherit them.",
                visibility=visible, language=language.upper(), agent=agent_name,
                llm=preferences["default_llm_model"], stt=preferences["default_stt_model"]),
        ReplyKeyboardMarkup(
            [[thinking_label], languages, [agent_label], [llm_label], [stt_label], [close_label]],
            resize_keyboard=True, one_time_keyboard=True,
        ),
        options,
    )


def _clear_selection_menus(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (
        "settings_options", "agent_options", "model_options", "model_catalog",
        "model_favorites", "model_page", "model_target_default",
        "stt_options", "stt_catalog", "stt_page", "stt_target_default",
    ):
        context.user_data.pop(key, None)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    try:
        text, markup, options = _settings_view(state.service, session)
    except (PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Could not load Telegram settings: %s", exc)
        await update.effective_message.reply_text(_msg(session, "Could not load settings. Try /settings again."))
        return
    _clear_selection_menus(context)
    session.agent_options.clear()
    context.user_data["settings_options"] = options
    await update.effective_message.reply_text(text, reply_markup=markup)


async def settings_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    selected = (update.effective_message.text or "").strip()
    data = context.user_data.get("settings_options", {}).get(selected)
    if data is None:
        return False
    if session is None:
        await _unauthorized(update)
        return True
    try:
        if data == "agents":
            await select_agent(update, context)
            return True
        if data == "default_llm":
            await select_model(update, context, target_default=True)
            return True
        if data == "default_stt":
            await select_stt(update, context, target_default=True)
            return True
        if data == "close":
            context.user_data.pop("settings_options", None)
            await update.effective_message.reply_text(
                _msg(session, "Settings closed."), reply_markup=ReplyKeyboardRemove(),
            )
            return True
        if data == "thinking":
            current = state.service.get_preferences(session.user_id)
            changes = UserPreferences(show_thinking=not current["show_thinking"])
        elif data in ("language:en", "language:es"):
            changes = UserPreferences.model_validate({"language": data.rsplit(":", 1)[1]})
        else:
            raise InputError("Unknown settings option. Open /settings again.")
        saved = state.service.update_preferences(session.user_id, changes)
        session.language = saved["language"]
        await settings_command(update, context)
    except (PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Could not update Telegram settings: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "Could not load or save settings: {error}\nTry /settings again.",
                 error=error_message(session.language, exc)),
        )
    return True


def _create_session_chat(session: TelegramSession, service: ChatService) -> dict:
    preferences = service.get_preferences(session.user_id)
    agent_id = preferences["agent_id"]
    if agent_id is None:
        raise InputError("No agents are configured for this user.")
    chat = service.create_chat(session.user_id, agent_id)
    session.agent_id = agent_id
    session.chat_id = chat["id"]
    session.pending_new_chat_notice = True
    return chat


async def new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    try:
        chat = _create_session_chat(session, state.service)
    except (PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Could not create Telegram chat: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "Could not create a chat: {error}", error=error_message(session.language, exc)),
        )
        return
    session.last_message_at = datetime.now(timezone.utc)
    session.pending_new_chat_notice = True
    session.model_options.clear()
    session.agent_options.clear()
    _clear_selection_menus(context)
    await update.effective_message.reply_text(
        _msg(session, "New chat created: {chat_id}\nYou can now choose a model or send your first message.",
             chat_id=chat["id"]),
    )


def _prepare_message_session(session: TelegramSession, service: ChatService) -> bool:
    now = datetime.now(timezone.utc)
    starts_new_chat = (
        session.chat_id is None
        or (
            session.last_message_at is not None
            and now - session.last_message_at > _CHAT_IDLE_TIMEOUT
        )
    )
    if starts_new_chat:
        _create_session_chat(session, service)
    session.last_message_at = now
    notice = session.pending_new_chat_notice or starts_new_chat
    session.pending_new_chat_notice = False
    return notice


def _price_key(value: str | None) -> tuple[int, Decimal]:
    if value is None:
        return (1, Decimal("Infinity"))
    try:
        return (0, Decimal(value))
    except InvalidOperation:
        return (1, Decimal("Infinity"))


def _sort_models(
    models: list[dict[str, Any]], favorites: set[str] | None = None,
) -> list[dict[str, Any]]:
    favorites = favorites if favorites is not None else set()
    return sorted(
        models,
        key=lambda model: (
            model["id"] not in favorites,
            _price_key(model.get("pricing", {}).get("prompt")),
            _price_key(model.get("pricing", {}).get("completion")),
            model["name"].casefold(),
            model["id"],
        ),
    )


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _audio_format(mime_type: str) -> str:
    return {
        "audio/ogg": "ogg",
        "audio/mpeg": "mp3",
        "audio/mp4": "m4a",
        "audio/aac": "aac",
        "audio/flac": "flac",
        "audio/wav": "wav",
        "audio/webm": "webm",
    }.get(mime_type, mime_type.split("/", 1)[-1].split(";")[0])


def _convert_markdown(text: str) -> tuple[str, list[MessageEntity]]:
    try:
        plain, entities = telegramify_markdown.convert(text)
        return plain, [MessageEntity(**entity.to_dict()) for entity in entities]
    except Exception:
        logger.warning("Could not format Telegram Markdown; sending plain text")
        return text, []


def _shift_entities(entities: list[MessageEntity], offset: int) -> list[MessageEntity]:
    shifted = []
    for entity in entities:
        data = entity.to_dict()
        data["offset"] += offset
        shifted.append(MessageEntity(**data))
    return shifted


def _generation_message(
    parts: list[tuple[str, str]], notice: str = "", *, show_thinking: bool = True,
) -> tuple[str, list[MessageEntity]]:
    """Render reasoning, text and actions in their original event order."""
    text, entities = _convert_markdown(notice) if notice else ("", [])
    quote_start: int | None = None
    for kind, content in parts:
        if kind == "reasoning" and not show_thinking:
            continue
        thinking = show_thinking and kind in ("reasoning", "reasoning_tool")
        if not thinking and quote_start is not None:
            text += "\n\n"
            entities.append(MessageEntity(
                type=MessageEntity.EXPANDABLE_BLOCKQUOTE,
                offset=quote_start, length=_utf16_len(text) - quote_start,
            ))
            quote_start = None
        if thinking and quote_start is None:
            if text and not text.endswith("\n"):
                text += "\n\n"
            quote_start = _utf16_len(text)
        if kind in ("tool", "reasoning_tool"):
            if text and not text.endswith("\n"):
                text += "\n"
            entities.append(MessageEntity(
                type=MessageEntity.ITALIC, offset=_utf16_len(text), length=_utf16_len(content),
            ))
            text += content + "\n\n"
        else:
            plain, part_entities = _convert_markdown(content)
            entities.extend(_shift_entities(part_entities, _utf16_len(text)))
            text += plain
    if quote_start is not None and _utf16_len(text) > quote_start:
        entities.append(MessageEntity(
            type=MessageEntity.EXPANDABLE_BLOCKQUOTE,
            offset=quote_start, length=_utf16_len(text) - quote_start,
        ))
    return text, sorted(entities, key=lambda entity: (entity.offset, -entity.length))


async def _show_model_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: TelegramSession,
) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    models = context.user_data["model_catalog"]
    page = context.user_data["model_page"]
    page_count = max(1, (len(models) + _MODELS_PER_PAGE - 1) // _MODELS_PER_PAGE)
    page = max(0, min(page, page_count - 1))
    context.user_data["model_page"] = page
    target_default = context.user_data.get("model_target_default", False)
    if target_default:
        current_model = state.service.get_preferences(session.user_id)["default_llm_model"]
        scope = _msg(session, "Choose the user's default LLM model. This does not set a chat override.")
    else:
        chat = state.service.get_chat(session.user_id, session.agent_id, session.chat_id)
        current_model = chat["model"]["id"]
        scope = _msg(session, "User default" if chat["model_override"] is None else "Chat override")
    page_models = models[
        page * _MODELS_PER_PAGE:(page + 1) * _MODELS_PER_PAGE
    ]
    favorites = context.user_data.get("model_favorites", set())
    model_options: dict[str, str | None] = {}
    keyboard: list[list[str]] = []
    if not target_default:
        inherit = _msg(session, "Use user default")
        model_options[inherit] = None
        keyboard.append([inherit])
    else:
        keyboard.append([_msg(session, "Back")])
    for index, model in enumerate(page_models, page * _MODELS_PER_PAGE + 1):
        label = (
            f"{'✅ ' if model['id'] == current_model else ''}"
            f"{'⭐ ' if model['id'] in favorites else ''}"
            f"{index}. {model['name'][:28]} | "
            f"${model['pricing'].get('prompt') or 'n/d'} in / "
            f"${model['pricing'].get('completion') or 'n/d'} out"
        )
        model_options[label] = model["id"]
        keyboard.append([label])
    navigation = []
    if page > 0:
        navigation.append(_msg(session, _PREVIOUS_PAGE))
    if page < page_count - 1:
        navigation.append(_msg(session, _NEXT_PAGE))
    if navigation:
        keyboard.append(navigation)
    context.user_data["model_options"] = model_options
    current_label = next(
        (model["name"] for model in models if model["id"] == current_model),
        current_model,
    )
    await update.effective_message.reply_text(
        scope + "\n\n"
        + _msg(session, "Current model: {model}\n\nPage {page}/{pages}. {count} models available.\nChoose another model. Prices are USD per 1 million tokens (input / output). ZDR enabled:",
             model=current_label, page=page + 1, pages=page_count, count=len(models)),
        reply_markup=ReplyKeyboardMarkup(
            keyboard, resize_keyboard=True, one_time_keyboard=True,
        ),
    )


async def select_model(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, target_default: bool = False,
) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    if session.chat_id is None and not target_default:
        await update.effective_message.reply_text(
            _msg(session, "Create a chat first with /newchat.")
        )
        return

    query = "" if target_default else " ".join(context.args or []).strip()
    try:
        favorites = set(state.service.list_favorite_models(session.user_id))
        models = _sort_models(state.service.list_models(session.user_id, query, zdr=True), favorites)
    except FavoritesError as exc:
        logger.warning("Could not load Telegram model favorites: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "Could not load model favorites: {error}", error=error_message(session.language, exc)),
        )
        return
    except (CatalogUnavailable, ServiceError, ValueError) as exc:
        logger.warning("Could not load Telegram model options: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "I cannot load the models right now. Please try again in a few seconds.")
        )
        return
    if not models and target_default:
        await update.effective_message.reply_text(
            _msg(session, "No ZDR models found. Try /selectmodel followed by part of a model name.")
        )
        return
    _clear_selection_menus(context)
    context.user_data["model_catalog"] = models
    context.user_data["model_target_default"] = target_default
    context.user_data["model_favorites"] = favorites
    context.user_data["model_page"] = 0
    await _show_model_page(update, context, session)


async def model_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    action = (update.effective_message.text or "").strip()
    previous = {_PREVIOUS_PAGE, message("es", _PREVIOUS_PAGE)}
    following = {_NEXT_PAGE, message("es", _NEXT_PAGE)}
    if action not in previous | following:
        return False
    models = context.user_data.get("model_catalog")
    if not models:
        return False
    page = context.user_data.get("model_page", 0)
    context.user_data["model_page"] = page + (1 if action in following else -1)
    session = _session(update, context.application.bot_data["state"])
    if session is None or (session.chat_id is None and not context.user_data.get("model_target_default")):
        await _unauthorized(update)
        return True
    await _show_model_page(update, context, session)
    return True


async def model_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    selected_label = (update.effective_message.text or "").strip()
    model_options = context.user_data.get("model_options", {})
    target_default = context.user_data.get("model_target_default", False)
    if target_default and selected_label in {"Back", message("es", "Back")}:
        await settings_command(update, context)
        return True
    if selected_label not in model_options:
        return False
    model_id = model_options[selected_label]

    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None or (session.chat_id is None and not target_default):
        await update.effective_message.reply_text(
            _msg(session, "The session has expired. Use /newchat."),
            reply_markup=ReplyKeyboardRemove(),
        )
        return True
    try:
        if target_default:
            state.service.update_preferences(session.user_id, UserPreferences(default_llm_model=model_id))
        else:
            state.service.set_chat_model(
                session.user_id, session.agent_id, session.chat_id, model_id, zdr=True,
            )
    except (CatalogUnavailable, PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Could not select Telegram model %r: %s", model_id, exc)
        await update.effective_message.reply_text(
            _msg(session, "Could not load or save settings: {error}\nTry /settings again.",
                 error=error_message(session.language, exc)) if target_default else
            _msg(session, "That model could not be selected.\nUse /selectmodel to try again."),
            reply_markup=ReplyKeyboardRemove(),
        )
        return True
    session.model_options.clear()
    _clear_selection_menus(context)
    if target_default:
        await settings_command(update, context)
        return True
    if model_id is None:
        await update.effective_message.reply_text(
            _msg(session, "This chat now uses the user's default LLM model."),
            reply_markup=ReplyKeyboardRemove(),
        )
        return True
    await update.effective_message.reply_text(
        _msg(session, "✅ Model selected\n\n{model_id}\n\nThis selection applies to the current chat.\nZDR: enabled",
             model_id=model_id),
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
        await update.effective_message.reply_text(_msg(session, "No active chat. Use /newchat."))
        return
    try:
        chat = state.service.get_chat(session.user_id, session.agent_id, session.chat_id)
    except (ServiceError, ValueError) as exc:
        logger.warning("Could not read Telegram chat model: %s", exc)
        await update.effective_message.reply_text(_msg(session, "I could not read the current chat model."))
        return
    model = chat["model"]
    await update.effective_message.reply_text(
        _msg(session, "Current chat model:\n{model_id}\n\nZDR: {policy}",
             model_id=model["id"], policy=_msg(session, "enabled" if model.get("zdr", True) else "disabled"))
        + "\n" + _msg(session, "User default" if chat["model_override"] is None else "Chat override"),
    )


async def select_stt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, target_default: bool = False,
) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    if session.chat_id is None and not target_default:
        await update.effective_message.reply_text(_msg(session, "Create a chat first with /newchat."))
        return
    query = "" if target_default else " ".join(context.args or []).strip()
    try:
        models = state.service.list_transcription_models(session.user_id, query, zdr=True)
    except (CatalogUnavailable, ServiceError, ValueError) as exc:
        logger.warning("Could not load Telegram STT models: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "I cannot load speech-to-text models right now. Please try again later.")
        )
        return
    if not models and target_default:
        await update.effective_message.reply_text(
            _msg(session, "No ZDR speech-to-text models found. Try /selectstt followed by a search term.")
        )
        return
    _clear_selection_menus(context)
    context.user_data["stt_catalog"] = models
    context.user_data["stt_target_default"] = target_default
    context.user_data["stt_page"] = 0
    await _show_stt_page(update, context, session)


async def _show_stt_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: TelegramSession,
) -> None:
    models = context.user_data["stt_catalog"]
    state: TelegramBotState = context.application.bot_data["state"]
    target_default = context.user_data.get("stt_target_default", False)
    if target_default:
        current = state.service.get_preferences(session.user_id)["default_stt_model"]
        scope = _msg(session, "Choose the user's default STT model. This does not set a chat override.")
    else:
        chat = state.service.get_chat(session.user_id, session.agent_id, session.chat_id)
        current = chat["stt_model"]
        scope = _msg(session, "User default" if chat["stt_model_override"] is None else "Chat override")
    page = context.user_data["stt_page"]
    page_count = max(1, (len(models) + _MODELS_PER_PAGE - 1) // _MODELS_PER_PAGE)
    page = max(0, min(page, page_count - 1))
    context.user_data["stt_page"] = page
    page_models = models[page * _MODELS_PER_PAGE:(page + 1) * _MODELS_PER_PAGE]
    options: dict[str, str | None] = {}
    keyboard: list[list[str]] = []
    if not target_default:
        inherit = _msg(session, "Use user default")
        options[inherit] = None
        keyboard.append([inherit])
    else:
        keyboard.append([_msg(session, "Back")])
    for index, model in enumerate(page_models, page * _MODELS_PER_PAGE + 1):
        label = (
            f"{'✅ ' if model['id'] == current else ''}"
            f"{index}. {model['name'][:34]} | "
            f"${model.get('pricing_prompt') or 'n/d'} / 1M tokens"
        )
        options[label] = model["id"]
        keyboard.append([label])
    navigation = []
    if page > 0:
        navigation.append(_msg(session, _STT_PREVIOUS_PAGE))
    if page < page_count - 1:
        navigation.append(_msg(session, _STT_NEXT_PAGE))
    if navigation:
        keyboard.append(navigation)
    context.user_data["stt_options"] = options
    await update.effective_message.reply_text(
        scope + "\n\n"
        + _msg(session, "Current STT model: {model}\n\nPage {page}/{pages}. {count} ZDR models available.\nChoose a speech-to-text model. Prices are USD per 1 million input tokens:",
             model=current, page=page + 1, pages=page_count, count=len(models)),
        reply_markup=ReplyKeyboardMarkup(
            keyboard, resize_keyboard=True, one_time_keyboard=True,
        ),
    )


async def stt_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    action = (update.effective_message.text or "").strip()
    previous = {_STT_PREVIOUS_PAGE, message("es", _STT_PREVIOUS_PAGE)}
    following = {_STT_NEXT_PAGE, message("es", _STT_NEXT_PAGE)}
    if action not in previous | following:
        return False
    if not context.user_data.get("stt_catalog"):
        return False
    page = context.user_data.get("stt_page", 0)
    context.user_data["stt_page"] = page + (1 if action in following else -1)
    session = _session(update, context.application.bot_data["state"])
    if session is None or (session.chat_id is None and not context.user_data.get("stt_target_default")):
        await _unauthorized(update)
        return True
    await _show_stt_page(update, context, session)
    return True


async def stt_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    selected = (update.effective_message.text or "").strip()
    options = context.user_data.get("stt_options", {})
    target_default = context.user_data.get("stt_target_default", False)
    if target_default and selected in {"Back", message("es", "Back")}:
        await settings_command(update, context)
        return True
    if selected not in options:
        return False
    model_id = options[selected]
    session = _session(update, context.application.bot_data["state"])
    if session is None:
        await _unauthorized(update)
        return True
    state: TelegramBotState = context.application.bot_data["state"]
    if session.chat_id is None and not target_default:
        await update.effective_message.reply_text(_msg(session, "Create a chat first with /newchat."))
        return True
    try:
        if target_default:
            state.service.update_preferences(session.user_id, UserPreferences(default_stt_model=model_id))
        else:
            state.service.set_chat_transcription_model(session.user_id, session.agent_id, session.chat_id, model_id)
    except (CatalogUnavailable, PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Could not select Telegram transcription model: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "Could not load or save settings: {error}\nTry /settings again.",
                 error=error_message(session.language, exc)),
        )
        return True
    _clear_selection_menus(context)
    if target_default:
        await settings_command(update, context)
        return True
    if model_id is None:
        await update.effective_message.reply_text(
            _msg(session, "This chat now uses the user's default STT model."),
            reply_markup=ReplyKeyboardRemove(),
        )
        return True
    await update.effective_message.reply_text(
        _msg(session, "✅ STT model selected:\n{model_id}\n\nThis selection applies only to the current chat. ZDR: enabled.",
             model_id=model_id),
        reply_markup=ReplyKeyboardRemove(),
    )
    return True


async def show_stt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    if session.chat_id is None:
        await update.effective_message.reply_text(_msg(session, "Create a chat first with /newchat."))
        return
    chat = state.service.get_chat(session.user_id, session.agent_id, session.chat_id)
    current = chat["stt_model"]
    await update.effective_message.reply_text(
        _msg(session, "Current STT model:\n{model}\n\nZDR: enabled", model=current)
        + "\n" + _msg(session, "User default" if chat["stt_model_override"] is None else "Chat override"),
    )


async def select_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    try:
        agents = state.service.list_agents(session.user_id)
    except ServiceError as exc:
        logger.warning("Could not load Telegram agents: %s", exc)
        await update.effective_message.reply_text(_msg(session, "I could not load your agents right now."))
        return
    if not agents:
        await update.effective_message.reply_text(_msg(session, "No agents are configured for this user."))
        return

    options: dict[str, str] = {}
    keyboard: list[list[str]] = []
    for agent in agents:
        label = f"{'✅ ' if agent['id'] == session.agent_id else ''}{agent['name']} ({agent['id']})"
        options[label] = agent["id"]
        keyboard.append([label])
    session.agent_options = options
    _clear_selection_menus(context)
    context.user_data["agent_options"] = options.copy()
    await update.effective_message.reply_text(
        _msg(session, "Choose the agent for new and incoming messages:"),
        reply_markup=ReplyKeyboardMarkup(
            keyboard, resize_keyboard=True, one_time_keyboard=True,
        ),
    )


async def show_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    try:
        agents = state.service.list_agents(session.user_id)
    except ServiceError as exc:
        logger.warning("Could not read current Telegram agent: %s", exc)
        await update.effective_message.reply_text(_msg(session, "I could not read the current agent."))
        return
    current = next((agent["name"] for agent in agents if agent["id"] == session.agent_id), session.agent_id)
    await update.effective_message.reply_text(
        _msg(session, "Current agent:\n{name} ({agent_id})", name=current, agent_id=session.agent_id),
    )


async def agent_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    selected = (update.effective_message.text or "").strip()
    options = context.user_data.get("agent_options", {})
    agent_id = options.get(selected)
    if agent_id is None:
        return False
    session = _session(update, context.application.bot_data["state"])
    if session is None:
        await _unauthorized(update)
        return True
    state: TelegramBotState = context.application.bot_data["state"]
    try:
        state.service.update_preferences(session.user_id, UserPreferences(agent_id=agent_id))
    except (PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Could not save Telegram agent selection: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "Could not select the agent: {error}", error=error_message(session.language, exc)),
        )
        return True
    session.agent_id = agent_id
    session.chat_id = None
    session.agent_options.clear()
    _clear_selection_menus(context)
    await update.effective_message.reply_text(
        _msg(session, "✅ Agent selected: {agent_id}\nUse /newchat to create a chat for this agent.",
             agent_id=agent_id),
        reply_markup=ReplyKeyboardRemove(),
    )
    return True


async def _download_attachment(message, bot) -> dict:
    source = None
    modality = None
    mime_type = None
    name = None
    if message.photo:
        source = message.photo[-1]
        modality, mime_type, name = "image", "image/jpeg", "photo.jpg"
    elif message.video:
        source = message.video
        modality, mime_type, name = "video", message.video.mime_type or "video/mp4", "video.mp4"
    elif message.audio:
        source = message.audio
        modality, mime_type, name = "audio", message.audio.mime_type or "audio/mpeg", message.audio.file_name or "audio"
    elif message.voice:
        source = message.voice
        modality, mime_type, name = "audio", message.voice.mime_type or "audio/ogg", "voice.ogg"
    elif message.document:
        mime_type = message.document.mime_type or ""
        modality = mime_type.split("/", 1)[0]
        if modality not in {"image", "video", "audio"}:
            raise InputError("Only image, video and audio files are supported.")
        source = message.document
        name = message.document.file_name or f"attachment.{mime_type.split('/', 1)[1]}"
    if source is None or modality is None or mime_type is None or name is None:
        raise InputError("This message does not contain a supported attachment.")

    telegram_file = await bot.get_file(source.file_id)
    data = bytes(await telegram_file.download_as_bytearray())
    if len(data) > _MAX_TELEGRAM_FILE_BYTES:
        raise InputError("Telegram attachments must be 22 MB or smaller.")
    extension = mimetypes.guess_extension(mime_type) or f".{modality}"
    return {
        "modality": modality,
        "name": name,
        "mime_type": mime_type,
        "format": _audio_format(mime_type) if modality == "audio" else extension.removeprefix(".").split(";")[0],
        "data_url": f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}",
    }


async def _next_event(events) -> tuple[bool, ChatEvent | None]:
    def take_next() -> tuple[bool, ChatEvent | None]:
        try:
            return True, next(events)
        except StopIteration:
            return False, None

    return await asyncio.to_thread(take_next)


def _format_actions(actions: list[dict]) -> str:
    return "\n".join(
        f">{action['name']}("
        + ", ".join(
            f"{name}={json.dumps(value, ensure_ascii=False)}"
            for name, value in action["arguments"].items()
        )
        + ")"
        for action in actions
    )


def _final_messages(
    parts: list[tuple[str, str]], notice: str = "", *, show_thinking: bool = True,
) -> list[tuple[str, list[MessageEntity]]]:
    """Split the rendered timeline without discarding text or entity formatting."""
    text, entities = _generation_message(parts, notice, show_thinking=show_thinking)
    messages: list[tuple[str, list[MessageEntity]]] = []
    start = offset = 0
    while start < len(text):
        end = start
        length = 0
        while end < len(text):
            units = 2 if ord(text[end]) > 0xFFFF else 1
            if length + units > _MAX_TELEGRAM_MESSAGE_CHARS:
                break
            length += units
            end += 1
        chunk_entities = []
        for entity in entities:
            left = max(entity.offset, offset)
            right = min(entity.offset + entity.length, offset + length)
            if left < right:
                data = entity.to_dict()
                data.update(offset=left - offset, length=right - left)
                chunk_entities.append(MessageEntity(**data))
        messages.append((text[start:end], chunk_entities))
        start = end
        offset += length
    return messages


@dataclass
class _ReplyContent:
    parts: list[tuple[str, str]] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)

    def append(self, kind: str, text: str) -> None:
        if not text:
            return
        if self.parts and self.parts[-1][0] == kind and kind in ("text", "reasoning"):
            self.parts[-1] = (kind, self.parts[-1][1] + text)
        else:
            self.parts.append((kind, text))

    def add_action(self, action: dict) -> None:
        # Tool events have no phase field; retain the latest emitted token context.
        thinking = self.parts and self.parts[-1][0] in ("reasoning", "reasoning_tool")
        self.append("reasoning_tool" if thinking else "tool", _format_actions([action]))
        self.actions.append(action)

    def reconcile(self, event: ChatEvent) -> None:
        if event.data is not None:
            for kind, snapshot in (("reasoning", event.data["reasoning"]), ("text", event.text)):
                received = "".join(text for part_kind, text in self.parts if part_kind == kind)
                if not snapshot.startswith(received):
                    raise ValueError(f"Interrupted Telegram {kind} snapshot does not match the stream")
                self.append(kind, snapshot[len(received):])
            actions = event.data["actions"]
            if actions[:len(self.actions)] != self.actions:
                raise ValueError("Interrupted Telegram actions do not match the stream")
            for action in actions[len(self.actions):]:
                self.add_action(action)


class _ReplyStream:
    """Delivers one streamed reply with native drafts, falling back to message edits."""

    _DRAFT_INTERVAL = 0.3
    _EDIT_INTERVAL = 1.0

    def __init__(self, bot, chat_id: int, use_draft: bool) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.use_draft = use_draft
        self.draft_id = secrets.randbelow(2**31 - 2) + 1
        self.message = None
        self.last_text = ""
        self.last_push = 0.0
        self.paused_until = 0.0

    async def start(self) -> None:
        if self.use_draft:
            # An empty draft shows Telegram's native "Thinking..." placeholder.
            if await self._push_draft("", []):
                return
        await self.bot.send_chat_action(self.chat_id, "typing")

    async def update(self, text: str, entities: list[MessageEntity]) -> None:
        loop_time = asyncio.get_running_loop().time()
        interval = self._DRAFT_INTERVAL if self.use_draft else self._EDIT_INTERVAL
        if (
            not text or text == self.last_text
            or loop_time < self.paused_until or loop_time - self.last_push < interval
        ):
            return
        self.last_push = loop_time
        if self.use_draft and await self._push_draft(text, entities):
            self.last_text = text
            return
        try:
            if self.message is None:
                self.message = await self.bot.send_message(self.chat_id, text, entities=entities)
            else:
                await self.message.edit_text(text, entities=entities)
            self.last_text = text
        except RetryAfter as exc:
            self.paused_until = loop_time + exc.retry_after
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                logger.warning("Telegram streaming edit failed: %s", exc)

    async def finish(self, messages: list[tuple[str, list[MessageEntity]]]) -> None:
        first, *rest = messages
        if self.message is not None:
            try:
                if first[0] != self.last_text:
                    await self.message.edit_text(first[0], entities=first[1])
            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    raise
        else:
            await self._send(*first)
        for text, entities in rest:
            await self._send(text, entities)

    async def _push_draft(self, text: str, entities: list[MessageEntity]) -> bool:
        try:
            await self.bot.send_message_draft(
                self.chat_id, self.draft_id, text=text, entities=entities or None,
            )
            return True
        except RetryAfter as exc:
            self.paused_until = asyncio.get_running_loop().time() + exc.retry_after
            return True
        except TelegramError as exc:
            logger.warning("Telegram drafts unavailable, falling back to edits: %s", exc)
            self.use_draft = False
            return False

    async def _send(self, text: str, entities: list[MessageEntity]) -> None:
        for _ in range(3):
            try:
                await self.bot.send_message(self.chat_id, text, entities=entities)
                return
            except RetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
        await self.bot.send_message(self.chat_id, text, entities=entities)


async def _send_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    attachments: list[dict],
    *,
    new_chat_notice: bool = False,
) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None or session.chat_id is None:
        await update.effective_message.reply_text(_msg(session, "Create a chat first with /newchat."))
        return
    user_id, agent_id, chat_id = session.user_id, session.agent_id, session.chat_id
    chat = update.effective_chat
    stream = _ReplyStream(context.bot, chat.id, use_draft=chat.type == ChatType.PRIVATE)
    await stream.start()
    content = _ReplyContent()
    language = session.language
    try:
        preferences = state.service.get_preferences(user_id)
        show_thinking = preferences["show_thinking"]
        language = preferences["language"]
        notice = message(language, "**_New chat started_**\n\n") if new_chat_notice else ""
        # Sending may transcribe audio first; keep that blocking work off the event loop.
        events = await asyncio.to_thread(
            state.service.send,
            user_id, agent_id, chat_id, text,
            stream=True, attachments=attachments,
        )
        while True:
            has_event, event = await _next_event(events)
            if not has_event:
                break
            if event is None:
                continue
            if event.kind == "interrupted":
                content.reconcile(event)
                content.append(
                    "text", message(language, "\n\n_[Interrupted — your latest message is being processed.]_"),
                )
                await stream.finish(_final_messages(content.parts, notice, show_thinking=show_thinking))
                return
            if event.kind in ("text", "reasoning"):
                content.append(event.kind, event.text)
            elif event.kind == "tool":
                if event.data is None:
                    raise ValueError("Telegram tool event is missing its action data")
                content.add_action(event.data)
            else:
                continue
            preview = _final_messages(content.parts, notice, show_thinking=show_thinking)
            if preview:
                await stream.update(*preview[-1])
        if not any(kind == "text" for kind, _ in content.parts):
            content.append("text", message(language, "_(The model returned no text.)_"))
        await stream.finish(_final_messages(content.parts, notice, show_thinking=show_thinking))
    except Exception as exc:
        logger.exception("Telegram generation failed")
        await stream.finish([(
            message(language, "Generation failed: {error}", error=error_message(language, exc)), [],
        )])


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    if await settings_selected(update, context):
        return
    if await agent_selected(update, context):
        return
    if await stt_navigation(update, context):
        return
    if await stt_selected(update, context):
        return
    if await model_page(update, context):
        return
    if await model_selected(update, context):
        return
    _clear_selection_menus(context)
    try:
        new_chat_notice = _prepare_message_session(session, state.service)
    except (PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Could not prepare Telegram chat: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "Could not prepare the chat: {error}", error=error_message(session.language, exc)),
        )
        return
    asyncio.create_task(
        _send_generation(
            update, context, update.effective_message.text or "", [],
            new_chat_notice=new_chat_notice,
        )
    )


async def media_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    try:
        attachment = await _download_attachment(update.effective_message, context.bot)
        text = update.effective_message.caption or ""
        new_chat_notice = _prepare_message_session(session, state.service)
        asyncio.create_task(
            _send_generation(
                update, context, text, [attachment],
                new_chat_notice=new_chat_notice,
            )
        )
    except (TelegramError, PreferencesError, ServiceError, ValueError, OSError) as exc:
        await update.effective_message.reply_text(
            _msg(session, "Attachment rejected: {error}", error=error_message(session.language, exc)),
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram handler failed: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        state: TelegramBotState = context.application.bot_data["state"]
        session = _session(update, state)
        if session is not None:
            await update.effective_message.reply_text(
                _msg(session, "The operation failed. Please try again."),
            )


def build_application(token: str, service: ChatService | None = None) -> Application:
    """Build the basic Telegram application with its channel-local state."""
    allowed = load_allowed_users()
    state = TelegramBotState(service or ChatService())
    for telegram_id, user_id in allowed.items():
        try:
            profile = state.service.login(user_id)
            if not profile["agents"]:
                raise ValueError("user has no agents")
            preferences = state.service.get_preferences(user_id)
            state.sessions[telegram_id] = TelegramSession(
                user_id, preferences["agent_id"], language=preferences["language"],
            )
        except (OSError, PreferencesError, ServiceError, ValueError) as exc:
            logger.warning("Ignoring invalid Telegram mapping %s=%s: %s", telegram_id, user_id, exc)

    application = ApplicationBuilder().token(token).concurrent_updates(True).build()
    application.bot_data["state"] = state
    application.bot_data["commands"] = [
        BotCommand("settings", "Thinking, language, and agent"),
        BotCommand("newchat", "Start a new chat"),
        BotCommand("selectagent", "Choose the active agent"),
        BotCommand("agent", "Show the active agent"),
        BotCommand("selectmodel", "Browse models and prices"),
        BotCommand("model", "Show the current model"),
        BotCommand("selectstt", "Choose the STT model"),
        BotCommand("stt", "Show the current STT model"),
        BotCommand("help", "Show available commands"),
    ]
    async def set_commands(application: Application) -> None:
        await application.bot.set_my_commands(application.bot_data["commands"])

    application.post_init = set_commands
    application.add_handler(TypeHandler(Update, refresh_language), group=-1)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("newchat", new_chat))
    application.add_handler(CommandHandler("selectmodel", select_model))
    application.add_handler(CommandHandler("model", show_model))
    application.add_handler(CommandHandler("selectstt", select_stt))
    application.add_handler(CommandHandler("stt", show_stt))
    application.add_handler(CommandHandler("selectagent", select_agent))
    application.add_handler(CommandHandler("agent", show_agent))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL, media_message)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.add_error_handler(error_handler)
    logger.info("Telegram bot ready with %d approved users", len(state.sessions))
    return application
