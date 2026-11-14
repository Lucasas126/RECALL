from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator, Literal, Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.sessions import SessionMiddleware

from src.config.agents import AgentConflict, MaxLevelConflict, UnknownApprovalRule
from src.config.levels import LevelField
from src.config.favorites import FavoriteModelId, FavoritesError
from src.config.models import AgentConfig, DreamConfig, ReasoningEffort
from src.core.sources.agent_files import MemoryTarget
from src.config.preferences import PreferencesError, UserPreferences
from src.core.sources.conversations import ChatMetadataError
from src.providers.catalog import CatalogUnavailable
from src.providers.mcp import McpError, McpNotConnected, UnknownMcpServer
from src.service.chat import (
    AgentBusy,
    ChatAttachment,
    ChatService,
    ServiceError,
    UnknownAgent,
    UnknownChat,
    UnknownTask,
    UnknownAutomation,
    UnknownDreamSession,
    UnknownManagedUser,
    UnknownUser,
    NotAdministrator,
    NoAvatar,
)
from src.utils.avatars import (
    MAX_UPLOAD_BYTES, AvatarStorageError, ImageTooLarge, InvalidImage, UnsupportedImageType, check_content_type,
)
from src.utils.logger import get_logger
from src.terminal.approvals import ApprovalForbidden, ApprovalNotFound
from src.utils.i18n import error_code, error_message, message, messages
from src.utils.version import version_info
from src.config.settings import WEB_SESSION_SECRET_ENV

logger = get_logger("web")

_STATIC = Path(__file__).parent / "static"
_SESSION_KEY = "user_id"


class LoginIn(BaseModel):
    user_id: str
    password: str = Field(min_length=1, max_length=1024)


class MessageIn(BaseModel):
    content: str = ""
    attachments: list[ChatAttachment] = Field(default_factory=list)
    # rewind the chat to this stored turn (a user message) and answer `content` in its place
    edit_index: int | None = Field(default=None, strict=True, ge=0)


class AutomationTriggerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["at", "every", "cron", "process_end"]
    at: str = Field(default="", max_length=40)
    minutes: int = Field(default=0, ge=0, le=60 * 24 * 31)
    cron: str = Field(default="", max_length=120)
    process: str = Field(default="", max_length=20)

    def stored(self) -> dict:
        return {"kind": self.kind, "at": self.at, "minutes": self.minutes, "cron": self.cron, "process": self.process}


class AutomationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=64)
    instruction: str = Field(max_length=4000)
    title: str = Field(default="", max_length=80)
    trigger: AutomationTriggerIn


class AutomationEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: Optional[str] = Field(default=None, max_length=4000)
    title: Optional[str] = Field(default=None, max_length=80)
    trigger: Optional[AutomationTriggerIn] = None


class ModelIn(BaseModel):
    model_id: FavoriteModelId
    zdr: bool | None = Field(default=None, strict=True)  # None follows the user's policy


class ChatModelIn(BaseModel):
    model_id: FavoriteModelId | None
    zdr: bool | None = Field(default=None, strict=True)


class ChatReasoningEffortIn(BaseModel):
    effort: ReasoningEffort | None


class ChatCreateIn(BaseModel):
    disposable: bool = Field(default=False, strict=True)


class ChatDisposableIn(BaseModel):
    disposable: bool = Field(strict=True)


class ChatTitleIn(BaseModel):
    title: str = Field(max_length=1000)


class DreamSettingsIn(BaseModel):
    dream: DreamConfig


class ReindexIn(BaseModel):
    full: bool = Field(default=False, strict=True)  # drop the index and rebuild it from scratch


class TextIn(BaseModel):
    content: str = Field(max_length=50_000)


class ApprovalIn(BaseModel):
    decision: Literal["approve", "deny"]
    scope: Literal["once", "session", "agent"] = "once"  # what an approval is remembered for; ignored for a deny


class MaxLevelIn(BaseModel):
    max_level: LevelField


class TaskSteerIn(BaseModel):
    text: str


async def _read_upload(request: Request) -> bytes:
    """The raw request body, refused as soon as it is over the avatar size limit (never fully buffered)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise ImageTooLarge("The image is larger than {limit} MiB.", limit=MAX_UPLOAD_BYTES // (1024 * 1024))
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise ImageTooLarge("The image is larger than {limit} MiB.", limit=MAX_UPLOAD_BYTES // (1024 * 1024))
        chunks.append(chunk)
    return b"".join(chunks)


def _avatar_response(request: Request, avatar, content: bytes) -> Response:
    """The stored PNG with a revalidating cache: the browser asks every time and gets a 304 while it is unchanged."""
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=0, must-revalidate",
        "ETag": avatar.etag,
    }
    if avatar.etag in (tag.strip() for tag in request.headers.get("if-none-match", "").split(",")):
        return Response(status_code=304, headers=headers)
    return Response(content, media_type="image/png", headers=headers)


def _sse(event: str, data, generation_id: str = "") -> str:
    event_id = f"id: {generation_id}\n" if generation_id else ""
    return f"{event_id}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _event_stream(events, language: str, user_id: str, agent_id: str, chat_id: str) -> Iterator[str]:
    """Format a reply's events as SSE; failures are reported in-band and `done` always closes the stream."""
    generation_id = ""
    try:
        for event in events:
            generation_id = event.generation_id
            if event.kind == "interrupted":
                data = {"content": event.text, **(event.data or {})}
            else:
                data = (
                    event.data if event.kind in ("tool_start", "tool", "memory", "approval", "approval_resolved", "approval_explanation", "task_started")
                    else event.text
                )
            yield _sse(event.kind, data, generation_id)
    except Exception as e:  # the response has started: report the failure in-band
        logger.exception("reply failed for %s/%s/%s", user_id, agent_id, chat_id)
        yield _sse("error", _error_payload(language, e), generation_id)
    yield _sse("done", "", generation_id)


def create_app(service: Optional[ChatService] = None, *, dream_scheduler: Optional[bool] = None) -> FastAPI:
    """Web channel: an HTTP/SSE adapter over `ChatService`.

    The user comes from a signed session cookie set after password authentication. The dream
    scheduler runs while the app is up; by default only when the app builds its own service.
    """
    if dream_scheduler is None:
        dream_scheduler = service is None
    service = service or ChatService(origins={"web"})

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if dream_scheduler:
            service.start_dream_scheduler()
        yield
        service.close()

    app = FastAPI(title="RECALL", lifespan=lifespan)
    session_secret = os.environ.get(WEB_SESSION_SECRET_ENV)
    if not session_secret:
        raise RuntimeError(
            f"Set the {WEB_SESSION_SECRET_ENV} environment variable before starting the web UI"
        )
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="recall_session",
        same_site="strict",
        https_only=False,
        max_age=60 * 60 * 24 * 14,
    )

    def current_user(request: Request) -> str:
        user_id = request.session.get(_SESSION_KEY)
        if not user_id:
            raise HTTPException(401, message(request.headers.get("Accept-Language", "en"), "not logged in"))
        try:
            service.login(user_id)
        except UnknownUser:
            request.session.clear()
            raise HTTPException(401, message(request.headers.get("Accept-Language", "en"), "not logged in")) from None
        return user_id

    def notification_language(request: Request) -> str:
        language = request.headers.get("Accept-Language", "en").split(",", 1)[0]
        user_id = request.session.get(_SESSION_KEY)
        if user_id:
            try:
                return service.language(user_id)
            except (PreferencesError, ServiceError, ValueError, OSError) as exc:
                logger.warning("Could not resolve notification language: %s", exc)
        return language

    def localized_error(request: Request, status: int, exc: Exception) -> JSONResponse:
        language = (
            request.headers.get("Accept-Language", "en").split(",", 1)[0]
            if isinstance(exc, UnknownUser) else notification_language(request)
        )
        return _error(status, exc, language)

    @app.exception_handler(UnknownUser)
    def _unknown_user(request: Request, exc: UnknownUser):
        return localized_error(request, 401, exc)

    @app.exception_handler(UnknownAgent)
    @app.exception_handler(UnknownChat)
    @app.exception_handler(UnknownTask)
    @app.exception_handler(UnknownAutomation)
    @app.exception_handler(ApprovalNotFound)
    @app.exception_handler(UnknownApprovalRule)
    @app.exception_handler(UnknownDreamSession)
    @app.exception_handler(UnknownManagedUser)
    @app.exception_handler(NoAvatar)
    def _not_found(request: Request, exc: ServiceError):
        return localized_error(request, 404, exc)

    @app.exception_handler(ApprovalForbidden)
    @app.exception_handler(NotAdministrator)
    def _approval_forbidden(request: Request, exc: ApprovalForbidden | NotAdministrator):
        return localized_error(request, 403, exc)

    @app.exception_handler(ImageTooLarge)
    def _image_too_large(request: Request, exc: ImageTooLarge):
        return localized_error(request, 413, exc)

    @app.exception_handler(UnsupportedImageType)
    def _unsupported_image(request: Request, exc: UnsupportedImageType):
        return localized_error(request, 415, exc)

    @app.exception_handler(InvalidImage)
    def _invalid_image(request: Request, exc: InvalidImage):
        return localized_error(request, 400, exc)

    @app.exception_handler(AvatarStorageError)
    def _avatar_storage(request: Request, exc: AvatarStorageError):
        logger.error("profile picture operation failed: %s", exc)
        return localized_error(request, 500, exc)

    @app.exception_handler(ValueError)
    def _bad_request(request: Request, exc: ValueError):
        return localized_error(request, 400, exc)

    @app.exception_handler(CatalogUnavailable)
    def _catalog_unavailable(request: Request, exc: CatalogUnavailable):
        logger.warning("model catalog unavailable: %s", exc)
        return localized_error(request, 502, exc)

    @app.exception_handler(FavoritesError)
    @app.exception_handler(PreferencesError)
    @app.exception_handler(ChatMetadataError)
    def _preferences_error(
        request: Request, exc: FavoritesError | PreferencesError | ChatMetadataError,
    ):
        logger.error("preferences operation failed: %s", exc)
        return localized_error(request, 500, exc)

    @app.exception_handler(AgentBusy)
    @app.exception_handler(AgentConflict)
    @app.exception_handler(McpNotConnected)
    def _agent_conflict(request: Request, exc: AgentBusy | AgentConflict | McpNotConnected):
        return localized_error(request, 409, exc)

    @app.exception_handler(MaxLevelConflict)
    def _max_level_conflict(request: Request, exc: MaxLevelConflict):
        response = localized_error(request, 409, exc)
        body = json.loads(response.body)
        body["agents"] = exc.agents  # the agents in the way: id, name, level
        return JSONResponse(body, status_code=409)

    @app.exception_handler(UnknownMcpServer)
    def _unknown_mcp_server(request: Request, exc: UnknownMcpServer):
        return localized_error(request, 404, exc)

    @app.exception_handler(McpError)
    def _mcp_error(request: Request, exc: McpError):
        logger.warning("MCP operation failed: %s", exc)
        return localized_error(request, 502, exc)

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"  # always revalidate the frontend
        return response

    # ------------------------------------------------------------------ session

    @app.post("/api/login")
    def login(body: LoginIn, request: Request):
        try:
            profile = service.authenticate(body.user_id.strip(), body.password)
        except UnknownUser as e:
            return localized_error(request, 401, e)
        request.session.clear()
        request.session[_SESSION_KEY] = profile["id"]
        return profile

    @app.post("/api/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def me(user_id: str = Depends(current_user)):
        return service.login(user_id)

    @app.put("/api/me/avatar")
    async def put_user_avatar(request: Request, user_id: str = Depends(current_user)):
        check_content_type(request.headers.get("content-type"))
        data = await _read_upload(request)
        return await run_in_threadpool(service.set_user_avatar, user_id, data)

    @app.get("/api/me/avatar")
    def get_user_avatar(request: Request, user_id: str = Depends(current_user)):
        avatar, content = service.get_user_avatar(user_id)
        return _avatar_response(request, avatar, content)

    @app.delete("/api/me/avatar", status_code=204)
    def delete_user_avatar(user_id: str = Depends(current_user)):
        service.delete_user_avatar(user_id)

    @app.get("/api/version")
    def version():
        return version_info()  # public: version and build id only

    @app.get("/api/localization")
    def localization(language: Literal["en", "es"] = "en"):
        return messages(language)

    @app.get("/api/preferences")
    def get_preferences(user_id: str = Depends(current_user)):
        return service.get_preferences(user_id)

    @app.patch("/api/preferences")
    def update_preferences(body: UserPreferences, user_id: str = Depends(current_user)):
        return service.update_preferences(user_id, body)

    @app.get("/api/models")
    def list_models(q: str = "", zdr: bool | None = None, user_id: str = Depends(current_user)):
        return service.list_models(user_id, q, zdr=zdr)

    @app.get("/api/models/catalog")
    def model_catalog(
        q: str = "", zdr: bool | None = None, capability: Literal["chat", "stt"] = "chat", provider: str | None = None,
        user_id: str = Depends(current_user),
    ):
        return service.model_catalog(user_id, q, zdr=zdr, capability=capability, provider=provider)

    @app.get("/api/providers/status")
    def provider_status(capability: Literal["chat", "stt"] = "chat", user_id: str = Depends(current_user)):
        return service.provider_status(user_id, capability)

    @app.get("/api/speech-to-text/models")
    def list_speech_to_text_models(
        q: str = "", zdr: bool | None = None, user_id: str = Depends(current_user),
    ):
        return service.list_transcription_models(user_id, q, zdr=zdr)

    @app.get("/api/models/favorites")
    def list_favorites(user_id: str = Depends(current_user)):
        return service.list_favorite_models(user_id)

    @app.post("/api/models/favorites")
    def add_favorite(body: ModelIn, user_id: str = Depends(current_user)):
        return service.add_favorite_model(user_id, body.model_id, zdr=body.zdr)

    @app.delete("/api/models/favorites/{model_id:path}")
    def remove_favorite(model_id: str, user_id: str = Depends(current_user)):
        return service.remove_favorite_model(user_id, model_id)

    # ------------------------------------------------------------------ chats

    @app.get("/api/agents")
    def list_agent_configs(user_id: str = Depends(current_user)):
        return service.list_agent_configs(user_id)

    @app.get("/api/agents/defaults")
    def get_agent_defaults(user_id: str = Depends(current_user)):
        return service.agent_defaults(user_id)

    @app.get("/api/levels")
    def get_levels(user_id: str = Depends(current_user)):
        return service.levels(user_id)

    @app.get("/api/tool-groups")
    def list_tool_groups(user_id: str = Depends(current_user)):
        return service.list_tool_groups(user_id)

    @app.get("/api/mcp")
    def list_mcp_servers(user_id: str = Depends(current_user)):
        return service.list_mcp_servers(user_id)

    @app.get("/api/mcp/{server_id}/tools")
    def list_mcp_tools(server_id: str, user_id: str = Depends(current_user)):
        return service.list_mcp_tools(user_id, server_id)

    @app.post("/api/mcp/{server_id}/connect")
    def connect_mcp(server_id: str, user_id: str = Depends(current_user)):
        return service.connect_mcp(user_id, server_id)

    @app.delete("/api/mcp/{server_id}/connection")
    def disconnect_mcp(server_id: str, user_id: str = Depends(current_user)):
        return service.disconnect_mcp(user_id, server_id)

    @app.post("/api/agents", status_code=201)
    def create_agent(body: AgentConfig, user_id: str = Depends(current_user)):
        return service.create_agent(user_id, body)

    @app.get("/api/agents/{agent_id}")
    def get_agent(agent_id: str, user_id: str = Depends(current_user)):
        return service.get_agent(user_id, agent_id)

    @app.put("/api/agents/{agent_id}")
    def update_agent(agent_id: str, body: AgentConfig, user_id: str = Depends(current_user)):
        return service.update_agent(user_id, agent_id, body)

    @app.delete("/api/agents/{agent_id}", status_code=204)
    def delete_agent(agent_id: str, user_id: str = Depends(current_user)):
        service.delete_agent(user_id, agent_id)

    @app.put("/api/agents/{agent_id}/avatar")
    async def put_agent_avatar(agent_id: str, request: Request, user_id: str = Depends(current_user)):
        service.check_agent_owned(user_id, agent_id)  # another user's agent is a 404 before anything is read
        check_content_type(request.headers.get("content-type"))
        data = await _read_upload(request)
        return await run_in_threadpool(service.set_agent_avatar, user_id, agent_id, data)

    @app.get("/api/agents/{agent_id}/avatar")
    def get_agent_avatar(agent_id: str, request: Request, user_id: str = Depends(current_user)):
        avatar, content = service.get_agent_avatar(user_id, agent_id)
        return _avatar_response(request, avatar, content)

    @app.delete("/api/agents/{agent_id}/avatar", status_code=204)
    def delete_agent_avatar(agent_id: str, user_id: str = Depends(current_user)):
        service.delete_agent_avatar(user_id, agent_id)

    @app.get("/api/chats")
    def list_all_chats(keep: Optional[str] = None, user_id: str = Depends(current_user)):
        return service.list_all_chats(user_id, keep)

    @app.get("/api/agents/{agent_id}/chats")
    def list_chats(agent_id: str, keep: Optional[str] = None, user_id: str = Depends(current_user)):
        return service.list_chats(user_id, agent_id, keep)

    @app.post("/api/agents/{agent_id}/chats", status_code=201)
    def create_chat(
        agent_id: str, body: Optional[ChatCreateIn] = None, disposable: Optional[bool] = None,
        user_id: str = Depends(current_user),
    ):
        # optional JSON body or query flag; no body keeps creating a normal chat
        wanted = disposable if disposable is not None else (body.disposable if body is not None else False)
        return service.create_chat(user_id, agent_id, disposable=wanted)

    @app.post("/api/agents/{agent_id}/chats/{chat_id}/disposable")
    def set_chat_disposable(
        agent_id: str, chat_id: str, body: ChatDisposableIn, user_id: str = Depends(current_user),
    ):
        return service.set_chat_disposable(user_id, agent_id, chat_id, body.disposable)

    @app.get("/api/agents/{agent_id}/chats/{chat_id}")
    def get_chat(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        return service.get_chat(user_id, agent_id, chat_id)

    @app.get("/api/agents/{agent_id}/chats/{chat_id}/models")
    def get_chat_models(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        return service.get_chat_models(user_id, agent_id, chat_id)

    @app.put("/api/agents/{agent_id}/chats/{chat_id}/model")
    def set_chat_model(
        agent_id: str, chat_id: str, body: ChatModelIn, user_id: str = Depends(current_user),
    ):
        return service.set_chat_model(user_id, agent_id, chat_id, body.model_id, zdr=body.zdr)

    @app.put("/api/agents/{agent_id}/chats/{chat_id}/reasoning-effort")
    def set_chat_reasoning_effort(
        agent_id: str, chat_id: str, body: ChatReasoningEffortIn, user_id: str = Depends(current_user),
    ):
        return service.set_chat_reasoning_effort(user_id, agent_id, chat_id, body.effort)

    @app.put("/api/agents/{agent_id}/chats/{chat_id}/title")
    def rename_chat(
        agent_id: str, chat_id: str, body: ChatTitleIn, user_id: str = Depends(current_user),
    ):
        return service.rename_chat(user_id, agent_id, chat_id, body.title)

    @app.delete("/api/agents/{agent_id}/chats/{chat_id}", status_code=204)
    def delete_chat(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        service.delete_chat(user_id, agent_id, chat_id)

    # ------------------------------------------------------------------ dream mode and own files

    @app.get("/api/agents/{agent_id}/dream/settings")
    def get_dream_settings(agent_id: str, user_id: str = Depends(current_user)):
        return service.get_dream_settings(user_id, agent_id)

    @app.put("/api/agents/{agent_id}/dream/settings")
    def update_dream_settings(agent_id: str, body: DreamSettingsIn, user_id: str = Depends(current_user)):
        return service.update_dream_settings(user_id, agent_id, body.dream)

    # ------------------------------------------------------------------ vector index

    @app.get("/api/agents/{agent_id}/index")
    def index_status(agent_id: str, user_id: str = Depends(current_user)):
        return service.index_status(user_id, agent_id)

    @app.post("/api/agents/{agent_id}/index/reindex", status_code=202)
    def reindex(agent_id: str, body: Optional[ReindexIn] = None, user_id: str = Depends(current_user)):
        return service.reindex(user_id, agent_id, full=bool(body and body.full))

    @app.get("/api/agents/{agent_id}/dream/status")
    def dream_status(agent_id: str, user_id: str = Depends(current_user)):
        return service.dream_status(user_id, agent_id)

    @app.post("/api/agents/{agent_id}/dream/start", status_code=202)
    def start_dream(agent_id: str, user_id: str = Depends(current_user)):
        return service.start_dream(user_id, agent_id)

    @app.post("/api/agents/{agent_id}/dream/cancel", status_code=202)
    def cancel_dream(agent_id: str, user_id: str = Depends(current_user)):
        return service.cancel_dream(user_id, agent_id)

    @app.get("/api/agents/{agent_id}/dream/sessions")
    def list_dream_sessions(agent_id: str, user_id: str = Depends(current_user)):
        return service.list_dream_sessions(user_id, agent_id)

    @app.get("/api/agents/{agent_id}/dream/sessions/{session_id}")
    def get_dream_session(agent_id: str, session_id: str, user_id: str = Depends(current_user)):
        return service.get_dream_session(user_id, agent_id, session_id)

    @app.get("/api/agents/{agent_id}/soul")
    def get_soul(agent_id: str, user_id: str = Depends(current_user)):
        return service.get_soul(user_id, agent_id)

    @app.put("/api/agents/{agent_id}/soul")
    def update_soul(agent_id: str, body: TextIn, user_id: str = Depends(current_user)):
        return service.update_soul(user_id, agent_id, body.content)

    @app.get("/api/agents/{agent_id}/soul/history")
    def soul_history(agent_id: str, user_id: str = Depends(current_user)):
        return service.soul_history(user_id, agent_id)

    @app.get("/api/agents/{agent_id}/soul/history/{version}")
    def get_soul_version(agent_id: str, version: str, user_id: str = Depends(current_user)):
        return service.get_soul_version(user_id, agent_id, version)

    @app.post("/api/agents/{agent_id}/soul/history/{version}/restore")
    def restore_soul(agent_id: str, version: str, user_id: str = Depends(current_user)):
        return service.restore_soul(user_id, agent_id, version)

    @app.get("/api/agents/{agent_id}/memories/{target}")
    def get_memory(agent_id: str, target: MemoryTarget, user_id: str = Depends(current_user)):
        return service.get_memory(user_id, agent_id, target)

    @app.put("/api/agents/{agent_id}/memories/{target}")
    def update_memory(agent_id: str, target: MemoryTarget, body: TextIn, user_id: str = Depends(current_user)):
        return service.update_memory(user_id, agent_id, target, body.content)

    @app.post("/api/agents/{agent_id}/chats/{chat_id}/messages")
    def send_message(
        agent_id: str, chat_id: str, body: MessageIn, user_id: str = Depends(current_user),
    ):
        language = service.language(user_id)
        command_reply = (
            None if body.attachments or body.edit_index is not None
            else service.dream_command(user_id, agent_id, body.content)
        )
        if command_reply is None and not body.attachments and body.edit_index is None:
            command_reply = service.compact_command(user_id, agent_id, chat_id, body.content)
        if command_reply is None and not body.attachments and body.edit_index is None:
            command_reply = service.usage_command(user_id, body.content)
        if command_reply is not None:
            # commands are answered by RECALL itself: not sent to the model, not stored in the chat
            generation_id = uuid4().hex
            return StreamingResponse(
                iter([_sse("text", command_reply, generation_id), _sse("done", "", generation_id)]),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        events = service.send(
            user_id, agent_id, chat_id, body.content, stream=True,
            attachments=[attachment.model_dump() for attachment in body.attachments],
            rewind_to=body.edit_index, channel="web",
        )

        return StreamingResponse(
            _event_stream(events, language, user_id, agent_id, chat_id), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/agents/{agent_id}/chats/{chat_id}/stop", status_code=202)
    def stop_chat(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        return service.stop_chat(user_id, agent_id, chat_id)

    @app.post("/api/agents/{agent_id}/chats/{chat_id}/read")
    def mark_chat_read(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        return service.mark_chat_read(user_id, agent_id, chat_id)

    @app.get("/api/claude/usage")
    def claude_usage(user_id: str = Depends(current_user)):
        return service.claude_usage(user_id)

    @app.get("/api/agents/{agent_id}/chats/{chat_id}/stream")
    def follow_reply(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        language = service.language(user_id)
        events = service.follow(user_id, agent_id, chat_id)
        if events is None:
            return Response(status_code=204)  # no reply is running: the saved chat is complete
        return StreamingResponse(
            _event_stream(events, language, user_id, agent_id, chat_id), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/agents/{agent_id}/chats/{chat_id}/processes")
    def list_processes(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        return {"processes": service.list_processes(user_id, agent_id, chat_id)}

    @app.post("/api/agents/{agent_id}/chats/{chat_id}/processes/{process_id}/stop", status_code=202)
    def stop_process(agent_id: str, chat_id: str, process_id: str, user_id: str = Depends(current_user)):
        service.stop_process(user_id, agent_id, chat_id, process_id)
        return {"stopped": process_id}

    @app.get("/api/agents/{agent_id}/chats/{chat_id}/approvals")
    def list_approvals(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        return service.list_approvals(user_id, agent_id, chat_id)

    @app.post("/api/agents/{agent_id}/chats/{chat_id}/approvals/{approval_id}")
    def resolve_approval(
        agent_id: str, chat_id: str, approval_id: str, body: ApprovalIn, user_id: str = Depends(current_user),
    ):
        return service.resolve_approval(user_id, agent_id, chat_id, approval_id, body.decision, body.scope)

    @app.get("/api/agents/{agent_id}/approval-rules")
    def list_approval_rules(agent_id: str, user_id: str = Depends(current_user)):
        return service.list_approval_rules(user_id, agent_id)

    @app.get("/api/agents/{agent_id}/approval-rules/effective")
    def effective_approval_rules(agent_id: str, user_id: str = Depends(current_user)):
        return service.effective_approval_rules(user_id, agent_id)

    @app.delete("/api/agents/{agent_id}/approval-rules")
    def clear_approval_rules(agent_id: str, user_id: str = Depends(current_user)):
        return service.clear_approval_rules(user_id, agent_id)

    @app.post("/api/agents/{agent_id}/approval-rules/reset")
    def reset_approval_rules(agent_id: str, user_id: str = Depends(current_user)):
        return service.clear_approval_rules(user_id, agent_id)

    @app.delete("/api/agents/{agent_id}/approval-rules/{index}")
    def remove_approval_rule(agent_id: str, index: int, user_id: str = Depends(current_user)):
        return service.remove_approval_rule(user_id, agent_id, index)

    # ------------------------------------------------------------------ administration (max_level admin only)

    @app.get("/api/admin/users")
    def admin_list_users(user_id: str = Depends(current_user)):
        return service.list_users(user_id)

    @app.put("/api/admin/users/{target_id}/max-level")
    def admin_set_max_level(target_id: str, body: MaxLevelIn, user_id: str = Depends(current_user)):
        return service.set_user_max_level(user_id, target_id, body.max_level)

    # ------------------------------------------------------------------ subagent tasks

    @app.get("/api/automations")
    def list_automations(user_id: str = Depends(current_user)):
        return service.list_automations(user_id)

    @app.post("/api/automations", status_code=201)
    def create_automation(body: AutomationIn, user_id: str = Depends(current_user)):
        return service.create_automation(user_id, body.agent_id, body.instruction, body.trigger.stored(), body.title)

    @app.patch("/api/automations/{automation_id}")
    def edit_automation(automation_id: str, body: AutomationEditIn, user_id: str = Depends(current_user)):
        trigger = body.trigger.stored() if body.trigger is not None else None
        return service.edit_automation(user_id, automation_id, instruction=body.instruction, title=body.title, trigger=trigger)

    @app.post("/api/automations/{automation_id}/pause")
    def pause_automation(automation_id: str, user_id: str = Depends(current_user)):
        return service.set_automation_enabled(user_id, automation_id, False)

    @app.post("/api/automations/{automation_id}/resume")
    def resume_automation(automation_id: str, user_id: str = Depends(current_user)):
        return service.set_automation_enabled(user_id, automation_id, True)

    @app.delete("/api/automations/{automation_id}", status_code=204)
    def delete_automation(automation_id: str, user_id: str = Depends(current_user)):
        service.delete_automation(user_id, automation_id)

    @app.get("/api/tasks")
    def list_tasks(
        agent: Optional[str] = None, chat: Optional[str] = None, status: str = "",
        limit: int = Query(0, ge=0, le=500), user_id: str = Depends(current_user),
    ):
        return service.list_tasks(user_id, agent, chat, status, limit)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, user_id: str = Depends(current_user)):
        return service.get_task(user_id, task_id)

    @app.post("/api/tasks/{task_id}/steer")
    def steer_task(task_id: str, body: TaskSteerIn, user_id: str = Depends(current_user)):
        return service.steer_task(user_id, task_id, body.text)

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str, user_id: str = Depends(current_user)):
        return service.stop_task(user_id, task_id)

    @app.get("/api/tasks/{task_id}/chat")
    def get_task_chat(task_id: str, user_id: str = Depends(current_user)):
        return service.get_task_chat(user_id, task_id)

    @app.get("/api/tasks/{task_id}/stream")
    def follow_task_reply(task_id: str, user_id: str = Depends(current_user)):
        """Read-only live view of a running task's worker reply: the chat stream's frames, ended when the reply ends.

        Nothing running: only `done`. A task executed by another process: an `unavailable` frame, then `done`.
        """
        language = service.language(user_id)
        events, reason = service.follow_task(user_id, task_id)
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        if events is None:
            frames = [_sse("unavailable", "live view only in the process running the task")] if reason else []
            return StreamingResponse(iter([*frames, _sse("done", "", "")]), media_type="text/event-stream", headers=headers)
        agent_id, chat_id = service.tasks.child_of(user_id, task_id)
        return StreamingResponse(
            _event_stream(events, language, user_id, agent_id, chat_id), media_type="text/event-stream", headers=headers,
        )

    # ------------------------------------------------------------------ frontend

    @app.get("/")
    def index():
        return FileResponse(_STATIC / "index.html")

    # Browsers ask for these two at the site root even when the page does not link them.
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(_STATIC / "icons" / "favicon.ico")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    def apple_touch_icon():
        return FileResponse(_STATIC / "icons" / "apple-touch-icon-180.png")

    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
    return app


def _error_payload(language: str, exc: Exception):
    """SSE `error` data: the message string, or `{message, code}` when the error has a stable code."""
    text = error_message(language, exc)
    code = error_code(exc)
    return {"message": text, "code": code} if code else text


def _error(status: int, exc: Exception, language: str = "en") -> JSONResponse:
    body = {"detail": error_message(language, exc)}
    if code := error_code(exc):
        body["code"] = code
    return JSONResponse(body, status_code=status)
