from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import telegramify_markdown
from telegram import (
    BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity,
    ReplyKeyboardRemove, Update,
)
from telegram.constants import ChatType
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
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
_MAX_TELEGRAM_FILE_BYTES = 22_000_000
_MAX_TELEGRAM_MESSAGE_CHARS = 4_090
_CHAT_IDLE_TIMEOUT = timedelta(hours=1)
_PANEL_CALLBACK = "p"


@dataclass
class TelegramSession:
    user_id: str
    agent_id: str
    chat_id: str | None = None
    last_message_at: datetime | None = None
    pending_new_chat_notice: bool = False
    language: str = "en"


@dataclass
class TelegramBotState:
    service: ChatService
    sessions: dict[int, TelegramSession] = field(default_factory=dict)


@dataclass
class _Panel:
    """The single editable inline menu of a Telegram user: settings or a selector."""

    token: str
    chat_id: int
    view: str
    message_id: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    favorites: set[str] = field(default_factory=set)
    page: int = 0
    agent_id: str | None = None
    target_chat: str | None = None


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


_SETTINGS_VIEWS = frozenset({"main", "agents", "llm", "stt"})
_CHAT_VIEWS = frozenset({"chat_llm", "chat_stt"})


def _panel_button(panel: _Panel, text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=f"{_PANEL_CALLBACK}:{panel.token}:{action}")


def _llm_label(model: dict[str, Any], index: int, current: str | None, favorites: set[str]) -> str:
    return (
        f"{'✅ ' if model['id'] == current else ''}"
        f"{'⭐ ' if model['id'] in favorites else ''}"
        f"{index}. {model['name'][:28]} | "
        f"${model['pricing'].get('prompt') or 'n/d'} in / "
        f"${model['pricing'].get('completion') or 'n/d'} out"
    )


def _stt_label(model: dict[str, Any], index: int, current: str | None) -> str:
    return (
        f"{'✅ ' if model['id'] == current else ''}"
        f"{index}. {model['name'][:34]} | "
        f"${model.get('pricing_prompt') or 'n/d'} / 1M tokens"
    )


def _settings_main_view(
    service: ChatService, session: TelegramSession, panel: _Panel,
) -> tuple[str, InlineKeyboardMarkup]:
    preferences = service.get_preferences(session.user_id)
    agents = service.list_agents(session.user_id)
    language = preferences["language"]
    agent_name = next(
        (agent["name"] for agent in agents if agent["id"] == session.agent_id),
        message(language, "None"),
    )
    visible = message(language, "visible" if preferences["show_thinking"] else "hidden")
    languages = [
        _panel_button(panel, f"{'✅ ' if language == code else ''}{label}", f"lang:{code}")
        for code, label in (("en", "English"), ("es", "Español"))
    ]
    keyboard = [
        [_panel_button(panel, message(language, "Thinking: {visibility}", visibility=visible), "thinking")],
        languages,
        [_panel_button(panel, message(language, "Active agent"), "agents")],
        [_panel_button(panel, message(language, "Default LLM model"), "llm")],
        [_panel_button(panel, message(language, "Default STT model"), "stt")],
        [_panel_button(panel, message(language, "Close"), "close")],
    ]
    return (
        message(language, "Settings\n\nThinking: {visibility}\nAlerts/errors: {language}\nActive agent: {agent}\nDefault LLM: {llm}\nDefault STT: {stt}\n\nUser defaults are shared with the web. Chats with no selected model inherit them.",
                visibility=visible, language=language.upper(), agent=agent_name,
                llm=preferences["default_llm_model"], stt=preferences["default_stt_model"]),
        InlineKeyboardMarkup(keyboard),
    )


def _paged_rows(
    session: TelegramSession, panel: _Panel, label: Callable[[dict[str, Any], int], str],
) -> tuple[list[list[InlineKeyboardButton]], int]:
    page_count = max(1, (len(panel.items) + _MODELS_PER_PAGE - 1) // _MODELS_PER_PAGE)
    panel.page = max(0, min(panel.page, page_count - 1))
    start = panel.page * _MODELS_PER_PAGE
    rows = [
        [_panel_button(panel, label(item, index + 1), f"pick:{index}")]
        for index, item in enumerate(panel.items[start:start + _MODELS_PER_PAGE], start)
    ]
    navigation = []
    if panel.page > 0:
        navigation.append(_panel_button(panel, _msg(session, _PREVIOUS_PAGE), f"page:{panel.page - 1}"))
    if panel.page < page_count - 1:
        navigation.append(_panel_button(panel, _msg(session, _NEXT_PAGE), f"page:{panel.page + 1}"))
    if navigation:
        rows.append(navigation)
    return rows, page_count


def _panel_view(
    service: ChatService, session: TelegramSession, panel: _Panel,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the current view of a settings panel or selector."""
    if panel.view == "main":
        return _settings_main_view(service, session, panel)
    tail = (
        [_panel_button(panel, _msg(session, "Back"), "main")] if panel.view in _SETTINGS_VIEWS
        else [_panel_button(panel, _msg(session, "Close"), "close")]
    )
    if panel.view in ("agents", "select_agent"):
        rows = [
            [_panel_button(
                panel, f"{'✅ ' if agent['id'] == session.agent_id else ''}{agent['name']} ({agent['id']})",
                f"pick:{index}",
            )]
            for index, agent in enumerate(panel.items)
        ]
        return _msg(session, "Choose the agent for new and incoming messages:"), InlineKeyboardMarkup([*rows, tail])
    if panel.view in _CHAT_VIEWS:
        chat = service.get_chat_models(session.user_id, panel.agent_id, panel.target_chat)
        override = chat["model_override"] if panel.view == "chat_llm" else chat["stt_model_override"]
        current = chat["model"]["id"] if panel.view == "chat_llm" else chat["stt_model"]
        header = _msg(session, "User default" if override is None else "Chat override")
    else:
        preferences = service.get_preferences(session.user_id)
        current = preferences["default_llm_model" if panel.view == "llm" else "default_stt_model"]
        header = (
            _msg(session, "Choose the user's default LLM model. This does not set a chat override.")
            if panel.view == "llm" else
            _msg(session, "Choose the user's default STT model. This does not set a chat override.")
        )
    if panel.view in ("llm", "chat_llm"):
        rows, pages = _paged_rows(session, panel, lambda model, index: _llm_label(model, index, current, panel.favorites))
        name = next((model["name"] for model in panel.items if model["id"] == current), current)
        body = _msg(session, "Current model: {model}\n\nPage {page}/{pages}. {count} models available.\nChoose another model. Prices are USD per 1 million tokens (input / output). ZDR enabled:",
                    model=name, page=panel.page + 1, pages=pages, count=len(panel.items))
    else:
        rows, pages = _paged_rows(session, panel, lambda model, index: _stt_label(model, index, current))
        body = _msg(session, "Current STT model: {model}\n\nPage {page}/{pages}. {count} ZDR models available.\nChoose a speech-to-text model. Prices are USD per 1 million input tokens:",
                    model=current, page=panel.page + 1, pages=pages, count=len(panel.items))
    if panel.view in _CHAT_VIEWS:
        rows.insert(0, [_panel_button(panel, _msg(session, "Use user default"), "inherit")])
    return f"{header}\n\n{body}", InlineKeyboardMarkup([*rows, tail])


def _switch_agent(service: ChatService, session: TelegramSession, agent_id: str) -> None:
    service.update_preferences(session.user_id, UserPreferences(agent_id=agent_id))
    session.agent_id = agent_id
    session.chat_id = None


def _apply_panel_action(
    service: ChatService, session: TelegramSession, panel: _Panel, action: str,
) -> str | None:
    """Apply one button action; return a final message when the action completes a selector."""
    kind, _, argument = action.partition(":")
    settings = panel.view in _SETTINGS_VIEWS
    if kind == "main" and settings:
        panel.view, panel.items, panel.page = "main", [], 0
    elif kind == "thinking" and panel.view == "main":
        current = service.get_preferences(session.user_id)
        service.update_preferences(session.user_id, UserPreferences(show_thinking=not current["show_thinking"]))
    elif kind == "lang" and argument in ("en", "es") and panel.view == "main":
        saved = service.update_preferences(session.user_id, UserPreferences.model_validate({"language": argument}))
        session.language = saved["language"]
    elif kind == "agents" and panel.view == "main":
        panel.view, panel.items, panel.page = "agents", service.list_agents(session.user_id), 0
    elif kind == "llm" and panel.view == "main":
        favorites = set(service.list_favorite_models(session.user_id))
        models = _sort_models(service.list_models(session.user_id, "", zdr=True), favorites)
        panel.view, panel.items, panel.favorites, panel.page = "llm", models, favorites, 0
    elif kind == "stt" and panel.view == "main":
        models = service.list_transcription_models(session.user_id, "", zdr=True)
        panel.view, panel.items, panel.page = "stt", models, 0
    elif kind == "page" and argument.isdigit() and panel.view in ("llm", "stt", *_CHAT_VIEWS):
        panel.page = int(argument)
    elif kind == "inherit" and panel.view == "chat_llm":
        service.set_chat_model(session.user_id, panel.agent_id, panel.target_chat, None, zdr=True)
        return _msg(session, "This chat now uses the user's default LLM model.")
    elif kind == "inherit" and panel.view == "chat_stt":
        service.set_chat_transcription_model(session.user_id, panel.agent_id, panel.target_chat, None)
        return _msg(session, "This chat now uses the user's default STT model.")
    elif kind == "pick" and argument.isdigit() and int(argument) < len(panel.items):
        selected = panel.items[int(argument)]["id"]
        if panel.view == "agents":
            _switch_agent(service, session, selected)
        elif panel.view == "llm":
            service.update_preferences(session.user_id, UserPreferences(default_llm_model=selected))
        elif panel.view == "stt":
            service.update_preferences(session.user_id, UserPreferences(default_stt_model=selected))
        elif panel.view == "select_agent":
            _switch_agent(service, session, selected)
            return _msg(session, "✅ Agent selected: {agent_id}\nUse /newchat to create a chat for this agent.",
                        agent_id=selected)
        elif panel.view == "chat_llm":
            service.set_chat_model(session.user_id, panel.agent_id, panel.target_chat, selected, zdr=True)
            return _msg(session, "✅ Model selected\n\n{model_id}\n\nThis selection applies to the current chat.\nZDR: enabled",
                        model_id=selected)
        elif panel.view == "chat_stt":
            service.set_chat_transcription_model(session.user_id, panel.agent_id, panel.target_chat, selected)
            return _msg(session, "✅ STT model selected:\n{model_id}\n\nThis selection applies only to the current chat. ZDR: enabled.",
                        model_id=selected)
        panel.view, panel.items, panel.page = "main", [], 0
    else:
        raise InputError("Unknown menu option. Run the command again.")
    return None


async def _edit_panel(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _open_panel(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup: InlineKeyboardMarkup, panel: _Panel,
) -> None:
    """Send a new panel and disable the buttons of the user's previous one."""
    # Telegram rejects blank text, so the keyboard-removal message uses a visible character.
    with suppress(TelegramError):
        cleanup = await update.effective_message.reply_text(".", reply_markup=ReplyKeyboardRemove())
        await context.bot.delete_message(chat_id=panel.chat_id, message_id=cleanup.message_id)
    sent = await update.effective_message.reply_text(text, reply_markup=markup)
    panel.message_id = sent.message_id
    previous: _Panel | None = context.user_data.get("panel")
    context.user_data["panel"] = panel
    if previous is not None:
        with suppress(TelegramError):
            await context.bot.edit_message_reply_markup(
                chat_id=previous.chat_id, message_id=previous.message_id, reply_markup=None,
            )


def _new_panel(update: Update, view: str, session: TelegramSession | None = None) -> _Panel:
    return _Panel(
        secrets.token_hex(4), update.effective_chat.id, view,
        agent_id=session.agent_id if session else None,
        target_chat=session.chat_id if session else None,
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open one inline settings panel that is edited in place on every choice."""
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    panel = _new_panel(update, "main")
    try:
        text, markup = _panel_view(state.service, session, panel)
    except (PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Could not load Telegram settings: %s", exc)
        await update.effective_message.reply_text(_msg(session, "Could not load settings. Try /settings again."))
        return
    await _open_panel(update, context, text, markup, panel)


async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an inline-panel button by editing the panel instead of sending messages."""
    query = update.callback_query
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await query.answer()
        await _unauthorized(update)
        return
    parts = (query.data or "").split(":", 2)
    token, action = (parts + ["", ""])[1:3]
    panel: _Panel | None = context.user_data.get("panel")
    if (
        panel is None or panel.token != token or query.message is None
        or query.message.message_id != panel.message_id
        or (panel.target_chat is not None
            and (panel.agent_id, panel.target_chat) != (session.agent_id, session.chat_id))
    ):
        await query.answer(_msg(session, "This menu has expired. Run the command again."), show_alert=True)
        with suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)
        return
    try:
        if action == "close":
            context.user_data.pop("panel", None)
            await query.answer()
            if panel.view in _SETTINGS_VIEWS:
                await _edit_panel(query, _msg(session, "Settings closed."), None)
            else:
                with suppress(TelegramError):
                    await query.edit_message_reply_markup(reply_markup=None)
            return
        final = _apply_panel_action(state.service, session, panel, action)
        view = None if final is not None else _panel_view(state.service, session, panel)
    except (CatalogUnavailable, FavoritesError, PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Telegram menu action %r failed: %s", action, exc)
        await query.answer(error_message(session.language, exc)[:200], show_alert=True)
        return
    await query.answer()
    if final is not None:
        context.user_data.pop("panel", None)
        await _edit_panel(query, final, None)
    else:
        await _edit_panel(query, *view)


async def select_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    if session.chat_id is None:
        await update.effective_message.reply_text(_msg(session, "Create a chat first with /newchat."))
        return
    panel = _new_panel(update, "chat_llm", session)
    try:
        panel.favorites = set(state.service.list_favorite_models(session.user_id))
        query = " ".join(context.args or []).strip()
        panel.items = _sort_models(state.service.list_models(session.user_id, query, zdr=True), panel.favorites)
        text, markup = _panel_view(state.service, session, panel)
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
    await _open_panel(update, context, text, markup, panel)


async def select_stt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    if session.chat_id is None:
        await update.effective_message.reply_text(_msg(session, "Create a chat first with /newchat."))
        return
    panel = _new_panel(update, "chat_stt", session)
    try:
        query = " ".join(context.args or []).strip()
        panel.items = state.service.list_transcription_models(session.user_id, query, zdr=True)
        text, markup = _panel_view(state.service, session, panel)
    except (CatalogUnavailable, ServiceError, ValueError) as exc:
        logger.warning("Could not load Telegram STT models: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "I cannot load speech-to-text models right now. Please try again later.")
        )
        return
    await _open_panel(update, context, text, markup, panel)


async def select_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    panel = _new_panel(update, "select_agent")
    try:
        panel.items = state.service.list_agents(session.user_id)
    except ServiceError as exc:
        logger.warning("Could not load Telegram agents: %s", exc)
        await update.effective_message.reply_text(_msg(session, "I could not load your agents right now."))
        return
    if not panel.items:
        await update.effective_message.reply_text(_msg(session, "No agents are configured for this user."))
        return
    text, markup = _panel_view(state.service, session, panel)
    await _open_panel(update, context, text, markup, panel)


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
        if kind in ("tool", "reasoning_tool", "notice"):
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
            elif event.kind == "notice":
                content.append("notice", event.text)
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
    application.add_handler(CallbackQueryHandler(panel_callback, pattern=rf"^{_PANEL_CALLBACK}:"))
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
