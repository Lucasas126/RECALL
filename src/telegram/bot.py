from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import telegramify_markdown
from telegram import MessageEntity, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import TelegramError
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
from src.service.chat import ChatEvent, ChatService, ServiceError
from src.utils.logger import get_logger

logger = get_logger("telegram")
_MODELS_PER_PAGE = 10
_PREVIOUS_PAGE = "⬅️ Previous"
_NEXT_PAGE = "➡️ Next"
_MAX_TELEGRAM_FILE_BYTES = 22_000_000
_MAX_TELEGRAM_MESSAGE_CHARS = 4_090


@dataclass
class TelegramSession:
    user_id: str
    agent_id: str
    chat_id: str | None = None
    model_options: dict[str, str] = field(default_factory=dict)
    agent_options: dict[str, str] = field(default_factory=dict)


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
    session.agent_options.clear()
    context.user_data.pop("model_options", None)
    context.user_data.pop("model_catalog", None)
    context.user_data.pop("model_page", None)
    context.user_data.pop("agent_options", None)
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


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


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


def _clip_entities(text: str, entities: list[MessageEntity]) -> list[MessageEntity]:
    text_length = _utf16_len(text)
    clipped = []
    for entity in entities:
        data = entity.to_dict()
        start = data["offset"]
        end = min(start + data["length"], text_length)
        if start < text_length and end > start:
            data["length"] = end - start
            clipped.append(MessageEntity(**data))
    return clipped


def _generation_message(
    thinking: str, response: str,
) -> tuple[str, list[MessageEntity]]:
    """Build a Telegram-formatted message with collapsible thinking."""
    thinking_plain, thinking_entities = _convert_markdown(thinking) if thinking.strip() else ("", [])
    response_plain, response_entities = _convert_markdown(response) if response.strip() else ("", [])
    text = thinking_plain + response_plain
    entities: list[MessageEntity] = []
    if thinking_plain:
        entities.append(MessageEntity(
            type=MessageEntity.EXPANDABLE_BLOCKQUOTE,
            offset=0,
            length=_utf16_len(thinking_plain),
        ))
        entities.extend(thinking_entities)
    entities.extend(_shift_entities(response_entities, _utf16_len(thinking_plain)))
    text = text[:_MAX_TELEGRAM_MESSAGE_CHARS]
    return text, _clip_entities(text, entities)


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
        await update.effective_message.reply_text("I could not load your agents right now.")
        return
    if not agents:
        await update.effective_message.reply_text("No agents are configured for this user.")
        return

    options: dict[str, str] = {}
    keyboard: list[list[str]] = []
    for agent in agents:
        label = f"{'✅ ' if agent['id'] == session.agent_id else ''}{agent['name']}"
        options[label] = agent["id"]
        keyboard.append([label])
    session.agent_options = options
    context.user_data["agent_options"] = options.copy()
    await update.effective_message.reply_text(
        "Choose the agent for new and incoming messages:",
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
        await update.effective_message.reply_text("I could not read the current agent.")
        return
    current = next((agent["name"] for agent in agents if agent["id"] == session.agent_id), session.agent_id)
    await update.effective_message.reply_text(f"Current agent:\n{current} ({session.agent_id})")


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
    session.agent_id = agent_id
    session.chat_id = None
    session.agent_options.clear()
    context.user_data.pop("agent_options", None)
    context.user_data.pop("model_options", None)
    context.user_data.pop("model_catalog", None)
    context.user_data.pop("model_page", None)
    await update.effective_message.reply_text(
        f"✅ Agent selected: {agent_id}\n"
        "Use /newchat to create a chat for this agent.",
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
            raise ValueError("Only image, video and audio files are supported.")
        source = message.document
        name = message.document.file_name or f"attachment.{mime_type.split('/', 1)[1]}"
    if source is None or modality is None or mime_type is None or name is None:
        raise ValueError("This message does not contain a supported attachment.")

    telegram_file = await bot.get_file(source.file_id)
    data = bytes(await telegram_file.download_as_bytearray())
    if len(data) > _MAX_TELEGRAM_FILE_BYTES:
        raise ValueError("Telegram attachments must be 22 MB or smaller.")
    extension = mimetypes.guess_extension(mime_type) or f".{modality}"
    return {
        "modality": modality,
        "name": name,
        "mime_type": mime_type,
        "format": extension.removeprefix(".").split(";")[0],
        "data_url": f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}",
    }


async def _next_event(events) -> tuple[bool, ChatEvent | None]:
    def take_next() -> tuple[bool, ChatEvent | None]:
        try:
            return True, next(events)
        except StopIteration:
            return False, None

    return await asyncio.to_thread(take_next)


async def _send_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, attachments: list[dict]) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None or session.chat_id is None:
        await update.effective_message.reply_text("Create a chat first with /newchat.")
        return
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    reply = await update.effective_message.reply_text("…")
    answer = ""
    thinking = ""
    last_sent = ""
    last_edit = 0.0
    try:
        events = state.service.send(
            session.user_id, session.agent_id, session.chat_id, text,
            stream=True, attachments=attachments,
        )
        while True:
            has_event, event = await _next_event(events)
            if not has_event:
                break
            if event is None:
                continue
            if event.kind == "text":
                answer += event.text
                now = asyncio.get_running_loop().time()
                preview, entities = _generation_message(thinking, answer)
                if now - last_edit >= 0.35 and preview and preview != last_sent:
                    await reply.edit_text(preview, entities=entities)
                    last_sent = preview
                    last_edit = now
            elif event.kind == "reasoning":
                thinking += event.text
                now = asyncio.get_running_loop().time()
                preview, entities = _generation_message(thinking, answer)
                if now - last_edit >= 0.35 and preview and preview != last_sent:
                    await reply.edit_text(preview, entities=entities)
                    last_sent = preview
                    last_edit = now
            elif event.kind == "interrupted":
                answer = event.text
                final_text, entities = _generation_message(thinking, answer)
                interrupted = final_text or "(generation interrupted)"
                suffix = "\n\n[Interrupted — your latest message is being processed.]"
                available = _MAX_TELEGRAM_MESSAGE_CHARS - len(suffix)
                await reply.edit_text(
                    interrupted[:available] + suffix,
                    entities=_clip_entities(interrupted[:available], entities),
                )
                return
        if not answer:
            answer = "(The model returned no text.)"
        first_chunk, entities = _generation_message(thinking, answer)
        if first_chunk != last_sent:
            await reply.edit_text(first_chunk, entities=entities)
        if len(answer) > _MAX_TELEGRAM_MESSAGE_CHARS:
            for start in range(_MAX_TELEGRAM_MESSAGE_CHARS, len(answer), _MAX_TELEGRAM_MESSAGE_CHARS):
                chunk, chunk_entities = _convert_markdown(
                    answer[start:start + _MAX_TELEGRAM_MESSAGE_CHARS],
                )
                await context.bot.send_message(
                    update.effective_chat.id, chunk, entities=chunk_entities,
                )
    except Exception as exc:
        logger.exception("Telegram generation failed")
        await reply.edit_text(f"Generation failed: {exc}")


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    if await agent_selected(update, context):
        return
    if await model_page(update, context):
        return
    if await model_selected(update, context):
        return
    asyncio.create_task(_send_generation(update, context, update.effective_message.text or "", []))


async def media_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    try:
        attachment = await _download_attachment(update.effective_message, context.bot)
        text = update.effective_message.caption or ""
        await update.effective_message.reply_text("Attachment received. Generating a response...")
        asyncio.create_task(_send_generation(update, context, text, [attachment]))
    except (TelegramError, ValueError, OSError) as exc:
        await update.effective_message.reply_text(f"Attachment rejected: {exc}")


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

    application = ApplicationBuilder().token(token).concurrent_updates(True).build()
    application.bot_data["state"] = state
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("newchat", new_chat))
    application.add_handler(CommandHandler("selectmodel", select_model))
    application.add_handler(CommandHandler("model", show_model))
    application.add_handler(CommandHandler("selectagent", select_agent))
    application.add_handler(CommandHandler("agent", show_agent))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL, media_message)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.add_error_handler(error_handler)
    logger.info("Telegram bot ready with %d approved users", len(state.sessions))
    return application
