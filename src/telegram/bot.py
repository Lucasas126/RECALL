from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, get_args

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
from src.config.models import ReasoningEffort
from src.config.preferences import PreferencesError, UserPreferences
from src.config.secrets import get_secret
from src.config.settings import ORCHESTRATOR, TELEGRAM_ALLOWED_USERS_ENV, TELEGRAM_CHAT_IDLE_MINUTES
from src.providers.catalog import CatalogUnavailable
from src.providers.ollama import is_ollama_cloud
from src.providers.telegram_notify import parse_allowed_users
from src.service.chat import ChatEvent, ChatService, RouteDecision, ServiceError
from src.utils.logger import get_logger
from src.terminal.approvals import ApprovalForbidden
from src.utils.i18n import InputError, UserMessageError, error_message, message
from src.utils.version import version_label

logger = get_logger("telegram")
_MODELS_PER_PAGE = 10
_PREVIOUS_PAGE = "⬅️ Previous"
_NEXT_PAGE = "➡️ Next"
_MAX_TELEGRAM_FILE_BYTES = 22_000_000
_MAX_TELEGRAM_MESSAGE_CHARS = 4_090
_CHAT_IDLE_TIMEOUT = timedelta(minutes=TELEGRAM_CHAT_IDLE_MINUTES)  # telegram.chat_idle_minutes
_PANEL_CALLBACK = "p"
_REROUTE_CALLBACK = "r"
_APPROVAL_CALLBACK = "ap"
_APPROVAL_COMMAND_CHARS = 900
# seconds the answer waits after an idle new chat is announced, so the user can still pick another place;
# opening the agent picker gives the user more time
_REROUTE_GRACE_SECONDS = 8
_REROUTE_PICKER_SECONDS = 60
# panel views that create a chat with the picked agent (instead of only saving a preference)
_CREATE_VIEWS = frozenset({"new_chat", "reroute_agent"})


@dataclass
class TelegramSession:
    user_id: str
    agent_id: str
    chat_id: str | None = None
    last_message_at: datetime | None = None
    language: str = "en"
    pending_reroute: "_Reroute | None" = None


@dataclass
class _Reroute:
    """A message answered in an auto-created (idle) chat that the user may replay elsewhere.

    Lives during the grace period: until a button is used, the grace timer fires or the user sends another
    message (which starts the pending generation first). Then it is dropped, which frees the attachments.
    """

    token: str
    previous_chat_id: str = ""  # empty when there is no previous chat (the very first message)
    previous_agent_id: str = ""
    previous_title: str = ""
    auto_chat_id: str = ""
    auto_agent_id: str = ""
    # set by the orchestrator: agent ids offered as buttons (uncertain pick), the ones answering by default, the grace
    candidates: list[str] = field(default_factory=list)
    default_agent_id: str = ""
    routed: bool = False
    uncertain: bool = False
    grace: float = 0
    text: str = ""
    attachments: list[dict] = field(default_factory=list)
    update: "Update | None" = None
    agent_name: str = ""
    chat_id: int = 0
    message_id: int = 0
    task: "asyncio.Task | None" = None
    timer: "asyncio.Task | None" = None


@dataclass
class TelegramBotState:
    service: ChatService
    sessions: dict[int, TelegramSession] = field(default_factory=dict)
    approvals: dict[str, "_ApprovalCard"] = field(default_factory=dict)  # pending approval id -> its message


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
    reroute_token: str | None = None


def load_allowed_users() -> dict[int, str]:
    """Load Telegram IDs mapped to existing RECALL user IDs from the environment."""
    allowed, invalid = parse_allowed_users(get_secret(TELEGRAM_ALLOWED_USERS_ENV))
    for entry in invalid:
        logger.warning("Ignoring invalid Telegram user mapping: %r", entry)
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
        f"{version_label()}\n\n"
        "Available commands:\n\n"
        "/settings — Thinking, language, new chat agent, and chat reasoning effort\n"
        "/newchat — Start a new chat (you pick the agent; also /new)\n"
        "/selectagent — Choose the agent for a new chat\n"
        "/agent — Show the active agent\n"
        "/selectmodel — Choose among your favorite models (star them in the web UI)\n"
        "/model — Show the model selected for the current chat\n"
        "/stt — Show the default speech-to-text model (change it in /settings)\n"
        "/dream — Last dream session, schedule and pending chats\n"
        "/dreamstart — Start a dream session now\n"
        "/dreamcancel — Stop the running dream session\n"
        "/help — Show this help\n\n"
        "You can send text, images, audio, voice messages, and video directly."
    )


_NO_FAVORITES = "You have no favorite models yet. Star the models you want in the web UI (model selector), then try again."
_SETTINGS_VIEWS = frozenset({"main", "agents", "llm", "stt", "chat_effort"})
_CHAT_VIEWS = frozenset({"chat_llm"})
_EFFORT_LEVELS = get_args(ReasoningEffort)


def _panel_button(panel: _Panel, text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=f"{_PANEL_CALLBACK}:{panel.token}:{action}")


# short provider names for the Telegram buttons and confirmations (models with the same name can come
# from different providers, so every line says which one serves it)
_PROVIDER_TAGS = {"openrouter": "OpenRouter", "ollama": "Ollama", "claude_subscription": "Claude"}


def _provider_tag(provider: object) -> str:
    """The short name of a provider id; a model without one is an OpenRouter model (the original catalog)."""
    name = provider if isinstance(provider, str) and provider else "openrouter"
    return _PROVIDER_TAGS.get(name, name)


def _with_provider(provider: str | None, model_id: str) -> str:
    """`Provider · model id` for confirmations."""
    return f"{_provider_tag(provider)} · {model_id}"


def _llm_label(model: dict[str, Any], index: int, current: str | None, favorites: set[str], zdr: bool) -> str:
    head = f"{'✅ ' if model['id'] == current else ''}{'⭐ ' if model['id'] in favorites else ''}"
    provider = model.get("provider")
    tag = _provider_tag(provider)
    if provider == "claude_subscription":  # no per-token price; never ZDR
        return f"{head}🧪 {index}. {tag} · {model['name'][:22]} | {'plan, no ZDR' if zdr else 'plan'}"
    if provider == "ollama":
        return f"{head}{'🏠' if model.get('local', True) else '☁️'} {index}. {tag} · {model['name'][:22]} | {'local' if model.get('local', True) else 'cloud'}"
    return (
        f"{head}{index}. {tag} · {model['name'][:22]} | "
        f"${model['pricing'].get('prompt') or 'n/d'} in / "
        f"${model['pricing'].get('completion') or 'n/d'} out"
    )


def _favorite_models(service: ChatService, user_id: str) -> tuple[list[dict[str, Any]], set[str]]:
    """The Telegram selector offers only the user's favorite models (no search); favorites come from the web UI."""
    favorites = set(service.list_favorite_models(user_id))
    models = [model for model in service.list_models(user_id, "") if model["id"] in favorites]
    return _sort_models(models, favorites), favorites


def _zdr_line(session: TelegramSession, model: dict[str, Any]) -> str:
    """The data-retention line of a chat model; empty unless ZDR is in play (required by the user or by this chat).

    Never 'enabled' for Claude subscription or Ollama models.
    """
    if not (model.get("zdr") or model.get("require_zdr")):
        return ""
    provider = model.get("provider")
    if provider == "claude_subscription":
        return _msg(session, "Not ZDR (Claude subscription, experimental)")
    if provider == "ollama" and is_ollama_cloud(model["id"]):
        return _msg(session, "Not ZDR (Ollama cloud model)")
    if provider == "ollama":
        return _msg(session, "ZDR: not applicable (local Ollama model)")
    return _msg(session, "ZDR: {policy}", policy=_msg(session, "enabled" if model.get("zdr") else "disabled"))


def _lines(*parts: str) -> str:
    """Join the non-empty parts, one per line."""
    return "\n".join(part for part in parts if part)


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
    default_id = service.default_agent(session.user_id)
    default_name = next(
        (agent["name"] for agent in agents if agent["id"] == default_id),
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
        [_panel_button(panel, message(language, "Chat reasoning effort"), "effort")],
        [_panel_button(panel, message(language, "Close"), "close")],
    ]
    return (
        message(language, "Settings\n\nThinking: {visibility}\nAlerts/errors: {language}\nActive agent: {agent}\nDefault agent: {default_agent}\nDefault LLM: {llm}\nDefault STT: {stt}\n\nUser defaults are shared with the web. Chats with no selected model inherit them.",
                visibility=visible, language=language.upper(), agent=agent_name, default_agent=default_name,
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


def _effort_items(service: ChatService, session: TelegramSession, agent_id: str, chat_id: str) -> list[dict[str, Any]]:
    default = service.get_chat_models(session.user_id, agent_id, chat_id)["reasoning_effort"]["default"]
    default_label = _msg(session, "Default") + (f" ({default})" if default else "")
    return [{"id": None, "name": default_label}, *({"id": level, "name": level} for level in _EFFORT_LEVELS)]


def _effort_view(
    service: ChatService, session: TelegramSession, panel: _Panel,
) -> tuple[str, InlineKeyboardMarkup]:
    effort = service.get_chat_models(session.user_id, panel.agent_id, panel.target_chat)["reasoning_effort"]
    back = [_panel_button(panel, _msg(session, "Back"), "main")]
    if not effort["reasoning"]:
        return _msg(session, "Reasoning is off for this agent."), InlineKeyboardMarkup([back])
    if effort["supported"] is False:
        return _msg(session, "This model does not support reasoning effort."), InlineKeyboardMarkup([back])
    if effort["supported"] is None:
        return (
            _msg(session, "Reasoning effort support could not be verified right now."),
            InlineKeyboardMarkup([back]),
        )
    rows = [
        [_panel_button(
            panel, f"{'✅ ' if item['id'] == effort['override'] else ''}{item['name']}", f"pick:{index}",
        )]
        for index, item in enumerate(panel.items)
    ]
    return (
        _msg(session, "Choose the reasoning effort for this chat. It overrides the agent default only here."),
        InlineKeyboardMarkup([*rows, back]),
    )


_SOURCE_LABELS = {"chat": "Chat override", "agent": "Agent default", "user": "User default"}


def _model_source(chat: dict[str, Any]) -> str:
    """Where a chat's effective model comes from: `chat`, `agent` or `user`."""
    source = chat.get("model_source")
    if source in _SOURCE_LABELS:
        return source
    return "user" if chat.get("model_override") is None else "chat"


def _agent_default_model(service: ChatService, session: TelegramSession, agent_id: str | None = None) -> str | None:
    """The agent's default chat model, or None when it follows the user default (or cannot be read)."""
    try:
        model = service.get_agent(session.user_id, agent_id or session.agent_id)["chat_config"].get("model")
    except (ServiceError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return model if isinstance(model, str) and model else None


def _panel_view(
    service: ChatService, session: TelegramSession, panel: _Panel,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the current view of a settings panel or selector."""
    if panel.view == "main":
        return _settings_main_view(service, session, panel)
    if panel.view == "chat_effort":
        return _effort_view(service, session, panel)
    tail = (
        [_panel_button(panel, _msg(session, "Back"), "main")] if panel.view in _SETTINGS_VIEWS
        else [_panel_button(panel, _msg(session, "Close"), "close")]
    )
    if panel.view in _CREATE_VIEWS:
        tail = [_panel_button(
            panel, _msg(session, "✖ Cancel (keep current chat)" if session.chat_id else "✖ Cancel"), "close",
        )]
    if panel.view in ("agents", *_CREATE_VIEWS):
        rows = [
            [_panel_button(
                panel, f"{'✅ ' if agent['id'] == session.agent_id else ''}{agent['name']} ({agent['id']})",
                f"pick:{index}",
            )]
            for index, agent in enumerate(panel.items)
        ]
        title = {
            "new_chat": "Choose the agent for the new chat:",
            "reroute_agent": "Choose the agent to answer your message in a new chat:",
        }.get(panel.view, "Choose the agent for the new chat:")
        return _msg(session, title), InlineKeyboardMarkup([*rows, tail])
    if panel.view in _CHAT_VIEWS:
        chat = service.get_chat_models(session.user_id, panel.agent_id, panel.target_chat)
        header = _msg(session, _SOURCE_LABELS[_model_source(chat)])
        current = chat["model"]["id"]
    else:
        preferences = service.get_preferences(session.user_id)
        current = preferences["default_llm_model" if panel.view == "llm" else "default_stt_model"]
        header = (
            _msg(session, "Choose the user's default LLM model. This does not set a chat override.")
            if panel.view == "llm" else
            _msg(session, "Choose the user's default STT model. This does not set a chat override.")
        )
        agent_default = _agent_default_model(service, session) if panel.view == "llm" else None
        if agent_default:
            header += "\n" + _msg(
                session, "The active agent has its own default model ({model}); it takes precedence over this one.",
                model=agent_default,
            )
    if panel.view in ("llm", "chat_llm"):
        zdr = service.require_zdr(session.user_id)
        rows, pages = _paged_rows(session, panel, lambda model, index: _llm_label(model, index, current, panel.favorites, zdr))
        name = next((model["name"] for model in panel.items if model["id"] == current), current)
        mixed = any(model.get("provider", "openrouter") != "openrouter" for model in panel.items)
        if zdr:
            template = (
                "Current model: {model}\n\nPage {page}/{pages}. {count} models available.\nChoose another model. Prices are USD per 1 million tokens (input / output). OpenRouter models: ZDR only. 🧪 Claude subscription: experimental, not ZDR. 🏠 Ollama: local:"
                if mixed else "Current model: {model}\n\nPage {page}/{pages}. {count} models available.\nChoose another model. Prices are USD per 1 million tokens (input / output). ZDR enabled:")
        else:
            template = (
                "Current model: {model}\n\nPage {page}/{pages}. {count} models available.\nChoose another model. Prices are USD per 1 million tokens (input / output). 🧪 Claude subscription: experimental. 🏠 Ollama: local:"
                if mixed else "Current model: {model}\n\nPage {page}/{pages}. {count} models available.\nChoose another model. Prices are USD per 1 million tokens (input / output):")
        body = _msg(session, template, model=name, page=panel.page + 1, pages=pages, count=len(panel.items))
    else:
        rows, pages = _paged_rows(session, panel, lambda model, index: _stt_label(model, index, current))
        body = _msg(session, "Current STT model: {model}\n\nPage {page}/{pages}. {count} ZDR models available.\nChoose a speech-to-text model. Prices are USD per 1 million input tokens:"
                    if service.require_zdr(session.user_id, "stt") else "Current STT model: {model}\n\nPage {page}/{pages}. {count} models available.\nChoose a speech-to-text model. Prices are USD per 1 million input tokens:",
                    model=current, page=panel.page + 1, pages=pages, count=len(panel.items))
    if panel.view in _CHAT_VIEWS:
        label = "Use agent default" if chat.get("agent_default_model") else "Use user default"
        rows.insert(0, [_panel_button(panel, _msg(session, label), "inherit")])
    return f"{header}\n\n{body}", InlineKeyboardMarkup([*rows, tail])


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
        models, favorites = _favorite_models(service, session.user_id)
        if not models:
            return _msg(session, _NO_FAVORITES)
        panel.view, panel.items, panel.favorites, panel.page = "llm", models, favorites, 0
    elif kind == "stt" and panel.view == "main":
        models = service.list_transcription_models(session.user_id, "")
        panel.view, panel.items, panel.page = "stt", models, 0
    elif kind == "effort" and panel.view == "main":
        if session.chat_id is None:
            return _msg(session, "Create a chat first with /newchat.")
        panel.view, panel.agent_id, panel.target_chat = "chat_effort", session.agent_id, session.chat_id
        panel.items, panel.page = _effort_items(service, session, session.agent_id, session.chat_id), 0
    elif kind == "page" and argument.isdigit() and panel.view in ("llm", "stt", *_CHAT_VIEWS):
        panel.page = int(argument)
    elif kind == "inherit" and panel.view == "chat_llm":
        service.set_chat_model(session.user_id, panel.agent_id, panel.target_chat, None)
        if _agent_default_model(service, session, panel.agent_id):
            return _msg(session, "This chat now uses the agent's default LLM model.")
        return _msg(session, "This chat now uses the user's default LLM model.")
    elif kind == "pick" and panel.view == "chat_effort" and argument.isdigit() and int(argument) < len(panel.items):
        item = panel.items[int(argument)]
        service.set_chat_reasoning_effort(session.user_id, panel.agent_id, panel.target_chat, item["id"])
        return _msg(session, "Reasoning effort for this chat: {effort}\n\nThis selection applies only to the current chat.",
                    effort=item["name"])
    elif kind == "pick" and argument.isdigit() and int(argument) < len(panel.items):
        selected = panel.items[int(argument)]["id"]
        if panel.view == "agents":
            return _start_agent_chat(service, session, panel.items[int(argument)])
        elif panel.view == "llm":
            service.update_preferences(session.user_id, UserPreferences(default_llm_model=selected))
        elif panel.view == "stt":
            service.update_preferences(session.user_id, UserPreferences(default_stt_model=selected))
        elif panel.view == "chat_llm":
            chosen = service.set_chat_model(session.user_id, panel.agent_id, panel.target_chat, selected)
            return _lines(
                _msg(session, "✅ Model selected\n\n{model_id}\n\nThis selection applies to the current chat.",
                     model_id=_with_provider(panel.items[int(argument)].get("provider") or chosen.get("provider"), selected)),
                _zdr_line(session, chosen),
            )
        panel.view, panel.items, panel.page = "main", [], 0
    else:
        raise InputError("Unknown menu option. Run the command again.")
    return None


async def _edit_panel(
    query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None, *, markdown: bool = False,
) -> None:
    extra = {}
    if markdown:
        text, extra["entities"] = _convert_markdown(text)
    try:
        await query.edit_message_text(text, reply_markup=markup, **extra)
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
            if panel.view in _CREATE_VIEWS:
                await _edit_panel(query, _msg(session, "Cancelled. Your current chat stays as it is."
                                              if panel.view == "new_chat" else "Cancelled. Your message stays in the new chat."), None)
            elif panel.view in _SETTINGS_VIEWS:
                await _edit_panel(query, _msg(session, "Settings closed."), None)
            else:
                with suppress(TelegramError):
                    await query.edit_message_reply_markup(reply_markup=None)
            return
        # picking an agent creates its chat: the confirmation is Markdown (bold agent, italic model)
        markdown = action.startswith("pick:") and panel.view in ("agents", *_CREATE_VIEWS)
        if action.startswith("pick:") and panel.view in _CREATE_VIEWS:
            final = await _pick_create_agent(update, context, session, panel, action)
        else:
            final = _apply_panel_action(state.service, session, panel, action)
        view = None if final is not None else _panel_view(state.service, session, panel)
    except (CatalogUnavailable, FavoritesError, PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Telegram menu action %r failed: %s", action, exc)
        await query.answer(error_message(session.language, exc)[:200], show_alert=True)
        return
    await query.answer()
    if final is not None:
        context.user_data.pop("panel", None)
        await _edit_panel(query, final, None, markdown=markdown)
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
        panel.items, panel.favorites = _favorite_models(state.service, session.user_id)
        if not panel.items:
            await update.effective_message.reply_text(_msg(session, _NO_FAVORITES))
            return
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


def _md_escape(text: str) -> str:
    """Neutralize Markdown specials so names and ids cannot break the surrounding bold/italic markers."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!~>|=])", r"\\\1", text)


async def _reply_markdown(message, text: str) -> None:
    """Reply with Markdown converted to Telegram entities."""
    plain, entities = _convert_markdown(text)
    await message.reply_text(plain, entities=entities)


def _create_session_chat(session: TelegramSession, service: ChatService, agent_id: str | None = None) -> dict:
    """Create a chat and point the session at it; `agent_id` (a user pick) also becomes the saved last used agent.

    Without `agent_id` the chat is for the user's default agent (Settings > General); the last used agent is kept.

    Every path that changes the active agent goes through here, so the session never keeps a chat of the
    previous agent nor an empty chat id that a restart could resume from the old agent.
    """
    if agent_id is None:
        agent_id = service.default_agent(session.user_id)
        if agent_id is None:
            raise InputError("No agents are configured for this user.")
    else:
        service.update_preferences(session.user_id, UserPreferences(agent_id=agent_id))
    chat = service.create_chat(session.user_id, agent_id, origin="telegram")
    session.agent_id, session.chat_id = agent_id, chat["id"]
    session.last_message_at = datetime.now(timezone.utc)
    return chat


def _start_agent_chat(service: ChatService, session: TelegramSession, agent: dict) -> str:
    """Switch to `agent` with a fresh chat; returns the Markdown confirmation."""
    chat = _create_session_chat(session, service, agent["id"])
    return _new_chat_text(service, session, agent["name"], agent["id"], chat["id"])


def _new_chat_text(
    service: ChatService, session: TelegramSession, agent_name: str, agent_id: str, chat_id: str,
) -> str:
    """The Markdown confirmation of a new chat: its agent (bold) and the LLM it will use (italic)."""
    text = _msg(session, "New chat created with **{agent}**.", agent=_md_escape(agent_name))
    try:
        chat = service.get_chat(session.user_id, agent_id, chat_id)
        model = chat["model"]
        text += "\n" + _msg(
            session, "Model: _{model_id}_", model_id=_md_escape(_with_provider(model.get("provider"), model["id"])),
        ) + f" ({_msg(session, _SOURCE_LABELS[_model_source(chat)])})"
    except (ServiceError, ValueError, KeyError, OSError) as exc:
        logger.warning("Could not read the new chat's model: %s", exc)
    return text


async def _create_chat_now(update: Update, state: TelegramBotState, session: TelegramSession) -> None:
    """Create a chat for the default agent and confirm it (the single-agent /newchat)."""
    try:
        chat = _create_session_chat(session, state.service)
    except (PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Could not create Telegram chat: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "Could not create a chat: {error}", error=error_message(session.language, exc)),
        )
        return
    agent_name = next(
        (a["name"] for a in state.service.list_agents(session.user_id) if a["id"] == session.agent_id),
        session.agent_id,
    )
    await _reply_markdown(
        update.effective_message, _new_chat_text(state.service, session, agent_name, session.agent_id, chat["id"]),
    )


async def new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the agent picker for a new chat; with a single agent, create the chat directly."""
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    await _flush_reroute(context, session)
    try:
        agents = state.service.list_agents(session.user_id)
    except (ServiceError, OSError) as exc:
        logger.warning("Could not load Telegram agents: %s", exc)
        await update.effective_message.reply_text(_msg(session, "I could not load your agents right now."))
        return
    if len(agents) <= 1:
        await _create_chat_now(update, state, session)
        return
    panel = _new_panel(update, "new_chat")
    panel.items = agents
    text, markup = _panel_view(state.service, session, panel)
    await _open_panel(update, context, text, markup, panel)


async def _pick_create_agent(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: TelegramSession, panel: _Panel, action: str,
) -> str:
    """Handle a pick in the /newchat or re-route picker; returns the final confirmation text."""
    state: TelegramBotState = context.application.bot_data["state"]
    _, _, argument = action.partition(":")
    if not argument.isdigit() or int(argument) >= len(panel.items):
        raise InputError("Unknown menu option. Run the command again.")
    agent = panel.items[int(argument)]
    if panel.view == "new_chat":
        return _start_agent_chat(state.service, session, agent)
    record = session.pending_reroute
    if record is None or record.token != panel.reroute_token:
        raise InputError("This option expired.")
    return await _reroute_pending(update, context, session, record, agent["id"])


async def _drop_reroute(context: ContextTypes.DEFAULT_TYPE, session: TelegramSession) -> None:
    """Forget the pending re-route (freeing its attachments) and remove its buttons."""
    record, session.pending_reroute = session.pending_reroute, None
    if record is None:
        return
    if record.timer is not None and record.timer is not asyncio.current_task():
        record.timer.cancel()
    record.attachments = []
    if record.message_id:
        with suppress(TelegramError):
            await context.bot.edit_message_reply_markup(
                chat_id=record.chat_id, message_id=record.message_id, reply_markup=None,
            )


async def _start_pending(context: ContextTypes.DEFAULT_TYPE, session: TelegramSession, record: _Reroute) -> None:
    """End the grace period: answer the pending message in the auto-created chat and drop its buttons."""
    if session.pending_reroute is not record:
        return
    session.pending_reroute = None  # before any await, so a tap or the timer cannot repeat it
    if record.timer is not None and record.timer is not asyncio.current_task():
        record.timer.cancel()
    text, attachments, record.attachments = record.text, record.attachments, []
    asyncio.create_task(_send_generation(record.update, context, text, attachments))
    if record.message_id:
        with suppress(TelegramError):
            await context.bot.edit_message_text(
                chat_id=record.chat_id, message_id=record.message_id, reply_markup=None,
                text=_msg(session, "Answering in a new chat with {agent}.", agent=record.agent_name or record.auto_agent_id),
            )


async def _grace_then_generate(
    context: ContextTypes.DEFAULT_TYPE, session: TelegramSession, record: _Reroute, seconds: float,
) -> None:
    await asyncio.sleep(seconds)
    await _start_pending(context, session, record)


def _arm_grace(context: ContextTypes.DEFAULT_TYPE, session: TelegramSession, record: _Reroute, seconds: float) -> None:
    """(Re)start the single timer that answers the pending message after `seconds`."""
    if record.timer is not None:
        record.timer.cancel()
    record.timer = asyncio.create_task(_grace_then_generate(context, session, record, seconds))


def _last_chat(session: TelegramSession, service: ChatService) -> tuple[dict, str] | None:
    """(summary, agent id) of the chat the next message would continue, from persisted data, or None.

    A session that still knows its chat reads that one; after a restart the latest Telegram-origin chat of any agent is used
    (chats used only in the web UI never count).
    """
    try:
        if session.chat_id is not None and session.last_message_at is not None:
            chat, agent_id = service.chat_summary(session.user_id, session.agent_id, session.chat_id), session.agent_id
        else:
            chat = service.latest_chat(session.user_id, origin="telegram")
            agent_id = chat.get("agent_id") if isinstance(chat, dict) else None
    except (ServiceError, OSError, ValueError) as exc:
        logger.warning("Could not look up the last Telegram chat: %s", exc)
        return None
    if not isinstance(chat, dict) or not agent_id or not chat.get("id"):
        return None
    return chat, agent_id


def _last_activity(session: TelegramSession, service: ChatService, chat: dict | None, agent_id: str | None) -> datetime | None:
    """The newest of the in-memory and the persisted activity; a chat with a running reply is active now."""
    moments = [session.last_message_at] if session.chat_id is not None and session.last_message_at else []
    if chat is not None:
        try:
            if service.chat_active(session.user_id, agent_id, chat["id"]) is True:
                return datetime.now(timezone.utc)
        except (ServiceError, OSError, ValueError, TypeError) as exc:
            logger.warning("Could not check whether the Telegram chat is replying: %s", exc)
        try:
            stamp = datetime.fromisoformat(str(chat.get("updated")))
            moments.append(stamp if stamp.tzinfo else stamp.astimezone())
        except ValueError:
            pass
    return max(moments) if moments else None


def _prepare_message_session(session: TelegramSession, service: ChatService) -> None:
    """Pick the chat for an incoming message: continue/resume the active one or start a new idle chat.

    The idle time is measured from the real last activity (the persisted chat, not only memory), so a bot
    restart does not start a new chat. An idle new chat with a previous chat gets a `pending_reroute` record.
    """
    now = datetime.now(timezone.utc)
    session.pending_reroute = None
    found = _last_chat(session, service)
    chat, agent_id = found if found is not None else (None, None)
    last = _last_activity(session, service, chat, agent_id)
    starts_new_chat = last is None or now - last > _CHAT_IDLE_TIMEOUT
    if not starts_new_chat and (session.chat_id is None or session.last_message_at is None):
        # fresh session after a restart: resume the latest chat silently, with its agent
        if agent_id != session.agent_id:
            service.update_preferences(session.user_id, UserPreferences(agent_id=agent_id))
        session.agent_id, session.chat_id = agent_id, chat["id"]
    if starts_new_chat:
        previous = (chat["id"], agent_id, str(chat.get("title") or chat["id"])) if chat and chat.get("turns", 1) else ("", "", "")
        created = _create_session_chat(session, service)
        # without a previous chat the record only lives while the orchestrator decides (see _launch_generation)
        session.pending_reroute = _Reroute(secrets.token_hex(4), *previous, created["id"], session.agent_id)
    session.last_message_at = now


async def _flush_reroute(context: ContextTypes.DEFAULT_TYPE, session: TelegramSession) -> None:
    """Another message or command arrived during the grace period: answer the pending message first."""
    if session.pending_reroute is not None:
        await _start_pending(context, session, session.pending_reroute)


async def _launch_generation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: TelegramSession,
    text: str, attachments: list[dict],
) -> None:
    """Start the reply as a background task, or announce an idle new chat and wait out the grace period."""
    record = session.pending_reroute
    if record is not None:
        state: TelegramBotState = context.application.bot_data["state"]
        record.text, record.attachments, record.update = text, attachments, update
        await _orchestrate(update, context, session, record)
        if session.pending_reroute is not record:  # a later message already started it
            return
        if record.previous_chat_id or record.routed:
            await _offer_reroute(update, state.service, session, record)
            if session.pending_reroute is not record:
                return
            if record.message_id:
                _arm_grace(context, session, record, record.grace or _REROUTE_GRACE_SECONDS)
                return
        session.pending_reroute, record.attachments = None, []  # no buttons to show or could be shown: answer at once
    asyncio.create_task(_send_generation(update, context, text, attachments))


async def _switch_auto_chat(session: TelegramSession, service: ChatService, record: _Reroute, agent_id: str) -> bool:
    """Replace the empty auto-created chat by a new chat of `agent_id` (the saved preference is not changed)."""
    try:
        created = await asyncio.to_thread(service.create_chat, session.user_id, agent_id, origin="telegram")
    except (ServiceError, OSError, ValueError) as exc:
        logger.warning("Could not create the orchestrator's chat: %s", exc)
        return False
    old_agent, old_chat = record.auto_agent_id, record.auto_chat_id
    session.agent_id, session.chat_id = agent_id, created["id"]
    record.auto_agent_id, record.auto_chat_id = agent_id, created["id"]
    try:
        await asyncio.to_thread(service.discard_chat, session.user_id, old_agent, old_chat)
    except (ServiceError, OSError, ValueError) as exc:
        logger.warning("Could not delete the auto-created Telegram chat: %s", exc)
    return True


async def _orchestrate(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: TelegramSession, record: _Reroute,
) -> None:
    """Let the decision model pick the agent of a message that starts a new chat; any failure keeps the default one.

    Confident pick: its chat answers after `grace_seconds`. Uncertain: the core (else default) agent answers after
    `uncertain_grace_seconds` unless the user taps one of the top candidates. Only the text is sent to the router.
    """
    state: TelegramBotState = context.application.bot_data["state"]
    service = state.service
    if service.orchestrator_active(session.user_id) is not True:
        return
    text = record.text.strip()
    if not text:  # media without a caption: nothing to route on, the core agent (if any) takes it
        core = service.orchestrator_core_agent(session.user_id)
        if isinstance(core, str) and core != record.auto_agent_id:
            await _switch_auto_chat(session, service, record, core)
        return
    with suppress(TelegramError):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        decision = await asyncio.wait_for(
            asyncio.to_thread(service.route_message, session.user_id, text), ORCHESTRATOR.timeout_seconds + 0.5,
        )
    except Exception as exc:  # includes the timeout: the router must never block or break the message
        logger.warning("Telegram orchestrator unavailable (%s); using the default agent", type(exc).__name__)
        return
    if not isinstance(decision, RouteDecision) or session.pending_reroute is not record:
        return
    known = {agent["id"] for agent in service.list_agents(session.user_id)}
    ranking = [agent_id for agent_id, _ in decision.ranking if agent_id in known]
    if decision.agent_id not in known:
        return
    if not decision.uncertain:
        target, candidates, grace = decision.agent_id, [], ORCHESTRATOR.grace_seconds
    else:
        # the auto chat was created for the default agent (see _prepare_message_session)
        target = next((a for a in (decision.core_agent_id, record.auto_agent_id) if a in known), decision.agent_id)
        candidates = list(dict.fromkeys(ranking[:3]))
        if target not in candidates:
            candidates.append(target)
        grace = ORCHESTRATOR.uncertain_grace_seconds
    if target != record.auto_agent_id and not await _switch_auto_chat(session, service, record, target):
        return
    record.routed, record.uncertain, record.candidates, record.grace = True, decision.uncertain, candidates, grace
    record.default_agent_id = target
    logger.debug(
        "orchestrator routed to %s (confidence %.2f, uncertain=%s)", target, decision.confidence, decision.uncertain,
    )


async def _reroute_pending(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: TelegramSession,
    record: _Reroute, agent_id: str, chat_id: str | None = None,
) -> str:
    """Replay the pending message in `chat_id` (or a new chat of `agent_id`) and delete the auto-created chat.

    Reusable by any router that picks a target for a message already answered in an auto-created chat.
    Returns the confirmation text. Nothing is changed when the target chat cannot be used.
    """
    state: TelegramBotState = context.application.bot_data["state"]
    service = state.service
    if session.pending_reroute is not record:
        raise InputError("This option expired.")
    title = None
    if chat_id is not None:
        title = service.chat_summary(session.user_id, agent_id, chat_id).get("title") or chat_id
    session.pending_reroute = None  # single use, set before any await so a double tap cannot repeat it
    if record.timer is not None and record.timer is not asyncio.current_task():
        record.timer.cancel()
    if record.task is not None and not record.task.done():
        record.task.cancel()
    try:
        await asyncio.to_thread(service.discard_chat, session.user_id, record.auto_agent_id, record.auto_chat_id)
    except (ServiceError, OSError, ValueError) as exc:
        logger.warning("Could not delete the auto-created Telegram chat: %s", exc)
    created = chat_id is None
    try:
        if created:
            chat_id = service.create_chat(session.user_id, agent_id, origin="telegram")["id"]
        service.update_preferences(session.user_id, UserPreferences(agent_id=agent_id))
    except (PreferencesError, ServiceError, ValueError, OSError):
        record.attachments = []
        raise
    session.agent_id, session.chat_id = agent_id, chat_id
    session.last_message_at = datetime.now(timezone.utc)
    text, attachments, record.attachments = record.text, record.attachments, []
    if record.message_id:
        with suppress(TelegramError):
            await context.bot.edit_message_reply_markup(
                chat_id=record.chat_id, message_id=record.message_id, reply_markup=None,
            )
    asyncio.create_task(_send_generation(update, context, text, attachments))
    if created:
        return _msg(session, "🔀 New chat with **{agent}**: your message was sent there.", agent=_md_escape(agent_id))
    return _msg(session, "↩ Continuing {title}: your message was sent there.", title=_md_escape(str(title)))


async def reroute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buttons under the 'It has been a while' message: continue the previous chat or change the agent."""
    query = update.callback_query
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await query.answer()
        await _unauthorized(update)
        return
    parts = (query.data or "").split(":", 2)
    token, action = (parts + ["", ""])[1:3]
    record = session.pending_reroute
    if (
        record is None or record.token != token or query.message is None
        or query.message.message_id != record.message_id
        or session.chat_id != record.auto_chat_id
    ):
        await query.answer(_msg(session, "This option expired."), show_alert=True)
        with suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)
        if record is not None and record.token == token:
            await _drop_reroute(context, session)
        return
    try:
        if action == "c":
            text = await _reroute_pending(
                update, context, session, record, record.previous_agent_id, record.previous_chat_id,
            )
        elif action[:1] == "s" and action[1:].isdigit() and int(action[1:]) < len(record.candidates):
            agent_id = record.candidates[int(action[1:])]
            if agent_id == record.auto_agent_id:  # the default pick: answer now
                await query.answer()
                await _start_pending(context, session, record)
                return
            text = await _reroute_pending(update, context, session, record, agent_id)
        elif action == "a":
            agents = state.service.list_agents(session.user_id)
            panel = _new_panel(update, "reroute_agent")
            panel.items, panel.reroute_token = agents, record.token
            _arm_grace(context, session, record, _REROUTE_PICKER_SECONDS)
            await query.answer()
            await _open_panel(update, context, *_panel_view(state.service, session, panel), panel)
            return
        else:
            raise InputError("Unknown menu option. Run the command again.")
    except (CatalogUnavailable, PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Telegram re-route %r failed: %s", action, exc)
        await query.answer(error_message(session.language, exc)[:200], show_alert=True)
        return
    await query.answer()
    await _edit_panel(query, text, None, markdown=True)


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
    parts: list[tuple[str, str]], *, show_thinking: bool = True,
) -> tuple[str, list[MessageEntity]]:
    """Render reasoning, text and actions in their original event order."""
    text, entities = "", []
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
            text += plain + ("\n" if kind == "agent" else "")
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
    zdr_line = _zdr_line(session, model)
    await update.effective_message.reply_text(
        _msg(session, "Current chat model:\n{model_id}", model_id=_with_provider(model.get("provider"), model["id"]))
        + ("\n\n" + zdr_line if zdr_line else "")
        + "\n" + _msg(session, _SOURCE_LABELS[_model_source(chat)]),
    )


async def show_stt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    current = state.service.get_preferences(session.user_id)["default_stt_model"]
    await update.effective_message.reply_text(
        _msg(session, "Current STT model:\n{model}\n\nZDR: enabled" if state.service.require_zdr(session.user_id, "stt")
             else "Current STT model:\n{model}", model=current),
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


async def _run_dream_command(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    try:
        reply = await asyncio.to_thread(
            state.service.dream_command, session.user_id, session.agent_id, text, channel="telegram",
        )
    except (ServiceError, ValueError, OSError, RuntimeError) as exc:
        logger.warning("Telegram %s failed: %s", text.split()[0], exc)
        await update.effective_message.reply_text(
            _msg(session, "Generation failed: {error}", error=error_message(session.language, exc)),
        )
        return
    await _reply_markdown(update.effective_message, reply or "")


async def dream_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/dream` shows the dream status; `/dream start|cancel` still work as text (the menu advertises the one-word forms)."""
    await _run_dream_command(update, context, " ".join(["/dream", *(context.args or [])]))


async def dream_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/dreamstart` starts a dream session without waiting for it."""
    await _run_dream_command(update, context, "/dream start")


async def dream_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/dreamcancel` asks the running dream session to stop."""
    await _run_dream_command(update, context, "/dream cancel")


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
        modality = "document" if mime_type == "application/pdf" else mime_type.split("/", 1)[0]
        if modality not in {"image", "video", "audio", "document"}:
            raise InputError("Only image, video, audio and PDF files are supported.")
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


# text that cannot follow inline: indented code, list items, fences, headings, quotes, tables, rules, HTML
_BLOCK_START = re.compile(
    r"(?: {4}|\t|\s*(?:[-*+•]\s|\d+[.)]\s|```|~~~|#{1,6}(?:\s|$)|>|\||<|(?:[-*_][ \t]*){3,}$))",
    re.MULTILINE,
)


def _final_messages(
    parts: list[tuple[str, str]], *, show_thinking: bool = True, agent_name: str | None = None,
) -> list[tuple[str, list[MessageEntity]]]:
    """Split the rendered timeline without discarding text or entity formatting.

    With `agent_name` (final messages only, never drafts) the first text of the answer starts with the
    agent's name in bold, inline (`**Aria:** answer`); before a block element (list, code fence, heading,
    table, quote) the name gets its own line instead.
    """
    if agent_name is not None:
        first = next((index for index, (kind, _) in enumerate(parts) if kind == "text"), None)
        if first is not None:
            label = f"**{_md_escape(agent_name)}:**"
            answer = parts[first][1].lstrip("\n")
            if answer.strip() and not _BLOCK_START.match(answer):
                parts = [*parts[:first], ("text", f"{label} {answer.lstrip()}"), *parts[first + 1:]]
            else:
                parts = [*parts[:first], ("agent", label), *parts[first:]]
    text, entities = _generation_message(parts, show_thinking=show_thinking)
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


async def _offer_reroute(
    update: Update, service: ChatService, session: TelegramSession, record: _Reroute,
) -> None:
    """Send the small message with the re-route buttons just before the reply of a new chat."""
    agents: list[dict] = []
    with suppress(ServiceError, OSError):
        agents = service.list_agents(session.user_id)
    names = {agent["id"]: agent["name"] for agent in agents}
    record.agent_name = names.get(record.auto_agent_id, record.auto_agent_id)
    token = f"{_REROUTE_CALLBACK}:{record.token}"
    rows: list[list[InlineKeyboardButton]] = []
    seconds = int(record.grace or _REROUTE_GRACE_SECONDS)
    entities = None
    if record.routed and record.uncertain:
        for index, agent_id in enumerate(record.candidates):
            star = "⭐ " if agent_id == record.auto_agent_id else ""
            label = f"{star}{names.get(agent_id, agent_id)}"[:60]
            rows.append([InlineKeyboardButton(label, callback_data=f"{token}:s{index}")])
    last_row = []
    if record.previous_chat_id:
        last_row.append(InlineKeyboardButton(_msg(session, "↩ Continue previous chat"), callback_data=f"{token}:c"))
    if len(agents) > 1:
        other = "🔀 More agents" if record.routed and record.uncertain else (
            "🔀 Other agent" if record.routed else "🔀 Change agent"
        )
        last_row.append(InlineKeyboardButton(_msg(session, other), callback_data=f"{token}:a"))
    if last_row:
        rows.append(last_row)
    agent = _md_escape(record.agent_name)
    if record.routed and record.uncertain:
        key = (
            "It has been a while. I am not sure which agent fits this message. Pick one below; otherwise **{agent}** "
            "will answer in {seconds} seconds." if record.previous_chat_id else
            "I am not sure which agent fits this message. Pick one below; otherwise **{agent}** will answer in {seconds} seconds."
        )
    elif record.routed:
        key = (
            "It has been a while. I chose **{agent}** for this message." if record.previous_chat_id
            else "I chose **{agent}** for this message."
        )
    else:
        key = ""
    try:
        if key:
            text, entities = _convert_markdown(_msg(session, key, agent=agent, seconds=seconds))
        else:
            text = _msg(
                session,
                "It has been a while. Starting a new chat with {agent}. Tap below to continue the previous chat or pick "
                "another agent; otherwise I will answer in {seconds} seconds.",
                agent=record.agent_name, seconds=seconds,
            )
        sent = await update.effective_message.reply_text(
            text, entities=entities, reply_markup=InlineKeyboardMarkup(rows),
        ) if entities is not None else await update.effective_message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(rows),
        )
    except TelegramError as exc:
        logger.warning("Could not send the re-route buttons: %s", exc)
        return
    record.chat_id, record.message_id = update.effective_chat.id, sent.message_id


@dataclass
class _ApprovalCard:
    """The Telegram message that asks for one approval."""

    user_id: str
    agent_id: str
    chat_id: str
    telegram_chat_id: int
    message_id: int
    body: str  # Markdown shown above the outcome line


def _approval_text(language: str, data: dict) -> str:
    command = str(data.get("command", ""))
    if len(command) > _APPROVAL_COMMAND_CHARS:
        command = command[:_APPROVAL_COMMAND_CHARS] + "…"
    command = command.replace("```", "``\u200b`")
    risk = message(language, "dangerous") if data.get("risk") == "dangerous" else message(language, "needs your approval")
    lines = [
        message(language, "🔐 *Approval needed* ({risk})", risk=risk),
        "```\n" + command + "\n```",
    ]
    if data.get("backend"):
        lines.append(message(language, "Backend: {backend}", backend=_md_escape(str(data["backend"]))))
    lines.append(message(language, "Working directory: {cwd}", cwd=_md_escape(str(data.get("cwd", "")))))
    if data.get("reason"):
        lines.append(message(language, "Reason: {reason}", reason=_md_escape(str(data["reason"]))))
    lines.append(message(language, "It is denied automatically after {seconds} s.", seconds=int(data.get("timeout_seconds", 0))))
    return "\n".join(lines)


def _approval_outcome(language: str, decision: str, reason: str) -> str:
    if decision == "approved":
        return message(language, "✅ Approved")
    if reason == "timed out":
        return message(language, "⌛ Denied: timed out")
    if reason == "cancelled":
        return message(language, "⏹ Denied: the reply was cancelled")
    if reason == "service shutdown":
        return message(language, "⏹ Denied: the service is shutting down")
    return message(language, "❌ Denied")


async def _send_approval(context: ContextTypes.DEFAULT_TYPE, state: TelegramBotState, session: TelegramSession,
                         telegram_chat_id: int, data: dict, language: str) -> None:
    """Ask for an approval with [Approve][Deny] buttons; the reply stream simply waits."""
    approval_id = str(data["id"])
    body = _approval_text(language, data)
    plain, entities = _convert_markdown(body)
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton(message(language, "✅ Approve"), callback_data=f"{_APPROVAL_CALLBACK}:{approval_id}:y"),
        InlineKeyboardButton(message(language, "❌ Deny"), callback_data=f"{_APPROVAL_CALLBACK}:{approval_id}:n"),
    ]])
    sent = await context.bot.send_message(telegram_chat_id, plain, entities=entities, reply_markup=markup)
    state.approvals[approval_id] = _ApprovalCard(
        session.user_id, session.agent_id, session.chat_id, telegram_chat_id, sent.message_id, body,
    )


async def _close_approval(bot, state: TelegramBotState, approval_id: str, decision: str, reason: str,
                          language: str) -> None:
    """Show the outcome on the approval message and drop its buttons (idempotent)."""
    card = state.approvals.pop(approval_id, None)
    if card is None:
        return
    plain, entities = _convert_markdown(card.body + "\n\n" + _approval_outcome(language, decision, reason))
    with suppress(TelegramError):
        await bot.edit_message_text(
            plain, chat_id=card.telegram_chat_id, message_id=card.message_id, entities=entities, reply_markup=None,
        )


async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[Approve]/[Deny] under an approval message."""
    query = update.callback_query
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await query.answer()
        await _unauthorized(update)
        return
    parts = (query.data or "").split(":")
    approval_id, choice = (parts + ["", ""])[1:3]
    card = state.approvals.get(approval_id)
    if card is None or choice not in ("y", "n") or query.message is None or query.message.message_id != card.message_id:
        await query.answer(_msg(session, "This approval expired."), show_alert=True)
        with suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)
        return
    if card.user_id != session.user_id:
        await query.answer(_msg(session, "Only the chat's owner can answer this."), show_alert=True)
        return
    try:
        outcome = await asyncio.to_thread(
            state.service.resolve_approval, card.user_id, card.agent_id, card.chat_id, approval_id,
            "approve" if choice == "y" else "deny",
        )
    except (UserMessageError, ValueError) as exc:  # expired between the tap and now, or not the owner
        await query.answer(error_message(session.language, exc), show_alert=True)
        if not isinstance(exc, ApprovalForbidden):  # someone else's tap changes nothing: the card stays open
            await _close_approval(context.bot, state, approval_id, "denied", "timed out", session.language)
        return
    await query.answer()
    await _close_approval(context.bot, state, approval_id, outcome["status"], "", session.language)


def _agent_name(service: ChatService, user_id: str, agent_id: str) -> str:
    """The agent's display name, falling back to its id when it cannot be read."""
    try:
        return next((a["name"] for a in service.list_agents(user_id) if a["id"] == agent_id), agent_id)
    except (ServiceError, OSError, ValueError, KeyError):
        return agent_id


async def _send_generation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    attachments: list[dict],
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
        agent_name = _agent_name(state.service, user_id, agent_id)
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
                await stream.finish(_final_messages(content.parts, show_thinking=show_thinking, agent_name=agent_name))
                return
            if event.kind in ("text", "reasoning"):
                content.append(event.kind, event.text)
            elif event.kind == "tool":
                if event.data is None:
                    raise ValueError("Telegram tool event is missing its action data")
                content.add_action(event.data)
            elif event.kind == "notice":
                content.append("notice", event.text)
            elif event.kind == "approval" and event.data:
                await _send_approval(context, state, session, chat.id, event.data, language)
                continue
            elif event.kind == "approval_resolved" and event.data:
                await _close_approval(
                    context.bot, state, str(event.data.get("id")), str(event.data.get("decision")),
                    str(event.data.get("reason", "")), language,
                )
                continue
            else:
                continue
            preview = _final_messages(content.parts, show_thinking=show_thinking)
            if preview:
                await stream.update(*preview[-1])
        if not any(kind == "text" for kind, _ in content.parts):
            content.append("text", message(language, "_(The model returned no text.)_"))
        await stream.finish(_final_messages(content.parts, show_thinking=show_thinking, agent_name=agent_name))
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
    await _flush_reroute(context, session)
    try:
        _prepare_message_session(session, state.service)
    except (PreferencesError, ServiceError, ValueError, OSError) as exc:
        logger.warning("Could not prepare Telegram chat: %s", exc)
        await update.effective_message.reply_text(
            _msg(session, "Could not prepare the chat: {error}", error=error_message(session.language, exc)),
        )
        return
    await _launch_generation(update, context, session, update.effective_message.text or "", [])


async def media_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: TelegramBotState = context.application.bot_data["state"]
    session = _session(update, state)
    if session is None:
        await _unauthorized(update)
        return
    await _flush_reroute(context, session)
    try:
        attachment = await _download_attachment(update.effective_message, context.bot)
        text = update.effective_message.caption or ""
        _prepare_message_session(session, state.service)
        await _launch_generation(update, context, session, text, [attachment])
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


def build_application(
    token: str, service: ChatService | None = None, *, dream_scheduler: bool | None = None,
) -> Application:
    """Build the basic Telegram application with its channel-local state.

    The dream scheduler runs while the bot is up; by default only when the bot builds its own service.
    """
    allowed = load_allowed_users()
    if dream_scheduler is None:
        dream_scheduler = service is None
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
        BotCommand("settings", "Thinking, language, agent, and reasoning effort"),
        BotCommand("newchat", "New chat (choose the agent)"),
        BotCommand("selectagent", "Choose the agent for a new chat"),
        BotCommand("agent", "Show the active agent"),
        BotCommand("selectmodel", "Choose a favorite model"),
        BotCommand("model", "Show the current model"),
        BotCommand("stt", "Show the default STT model"),
        BotCommand("dream", "Dream status: last session, schedule, pending chats"),
        BotCommand("dreamstart", "Start a dream session now"),
        BotCommand("dreamcancel", "Stop the running dream session"),
        BotCommand("help", "Show available commands"),
    ]
    async def set_commands(application: Application) -> None:
        await application.bot.set_my_commands(application.bot_data["commands"])
        if dream_scheduler:
            state.service.start_dream_scheduler()

    application.post_init = set_commands

    async def close_service(_: Application) -> None:
        state.service.close()

    application.post_shutdown = close_service
    application.add_handler(TypeHandler(Update, refresh_language), group=-1)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CallbackQueryHandler(panel_callback, pattern=rf"^{_PANEL_CALLBACK}:"))
    application.add_handler(CallbackQueryHandler(reroute_callback, pattern=rf"^{_REROUTE_CALLBACK}:"))
    application.add_handler(CallbackQueryHandler(approval_callback, pattern=rf"^{_APPROVAL_CALLBACK}:"))
    application.add_handler(CommandHandler(["newchat", "new", "selectagent"], new_chat))
    application.add_handler(CommandHandler("selectmodel", select_model))
    application.add_handler(CommandHandler("model", show_model))
    application.add_handler(CommandHandler("stt", show_stt))
    application.add_handler(CommandHandler("agent", show_agent))
    application.add_handler(CommandHandler("dream", dream_command))
    application.add_handler(CommandHandler("dreamstart", dream_start_command))
    application.add_handler(CommandHandler("dreamcancel", dream_cancel_command))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL, media_message)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.add_error_handler(error_handler)
    logger.info("Telegram bot ready with %d approved users", len(state.sessions))
    return application
