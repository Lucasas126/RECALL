from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from src.config.agents import AgentConflict
from src.config.favorites import FavoriteModelId, FavoritesError
from src.config.models import AgentConfig, ReasoningEffort
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
    UnknownUser,
)
from src.utils.logger import get_logger
from src.utils.i18n import error_message, message, messages
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


class ModelIn(BaseModel):
    model_id: FavoriteModelId
    zdr: bool = Field(default=True, strict=True)


class ChatModelIn(BaseModel):
    model_id: FavoriteModelId | None
    zdr: bool = Field(default=True, strict=True)


class ChatReasoningEffortIn(BaseModel):
    effort: ReasoningEffort | None


def _sse(event: str, data, generation_id: str = "") -> str:
    event_id = f"id: {generation_id}\n" if generation_id else ""
    return f"{event_id}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(service: Optional[ChatService] = None) -> FastAPI:
    """Web channel: an HTTP/SSE adapter over `ChatService`.

    The user comes from a signed session cookie set after password authentication.
    """
    service = service or ChatService()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
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
    def _not_found(request: Request, exc: ServiceError):
        return localized_error(request, 404, exc)

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
    def list_models(q: str = "", zdr: bool = True, user_id: str = Depends(current_user)):
        return service.list_models(user_id, q, zdr=zdr)

    @app.get("/api/speech-to-text/models")
    def list_speech_to_text_models(
        q: str = "", zdr: bool = True, user_id: str = Depends(current_user),
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

    @app.get("/api/chats")
    def list_all_chats(keep: Optional[str] = None, user_id: str = Depends(current_user)):
        return service.list_all_chats(user_id, keep)

    @app.get("/api/agents/{agent_id}/chats")
    def list_chats(agent_id: str, keep: Optional[str] = None, user_id: str = Depends(current_user)):
        return service.list_chats(user_id, agent_id, keep)

    @app.post("/api/agents/{agent_id}/chats", status_code=201)
    def create_chat(agent_id: str, user_id: str = Depends(current_user)):
        return service.create_chat(user_id, agent_id)

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

    @app.delete("/api/agents/{agent_id}/chats/{chat_id}", status_code=204)
    def delete_chat(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        service.delete_chat(user_id, agent_id, chat_id)

    @app.post("/api/agents/{agent_id}/chats/{chat_id}/messages")
    def send_message(
        agent_id: str, chat_id: str, body: MessageIn, user_id: str = Depends(current_user),
    ):
        language = service.language(user_id)
        events = service.send(
            user_id, agent_id, chat_id, body.content, stream=True,
            attachments=[attachment.model_dump() for attachment in body.attachments],
        )

        def stream() -> Iterator[str]:
            generation_id = ""
            try:
                for event in events:
                    generation_id = event.generation_id
                    if event.kind == "interrupted":
                        data = {"content": event.text, **(event.data or {})}
                    else:
                        data = event.data if event.kind in ("tool", "memory") else event.text
                    yield _sse(event.kind, data, generation_id)
            except Exception as e:  # the response has started: report the failure in-band
                logger.exception("reply failed for %s/%s/%s", user_id, agent_id, chat_id)
                yield _sse("error", error_message(language, e), generation_id)
            yield _sse("done", "", generation_id)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------ frontend

    @app.get("/")
    def index():
        return FileResponse(_STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
    return app


def _error(status: int, exc: Exception, language: str = "en") -> JSONResponse:
    return JSONResponse({"detail": error_message(language, exc)}, status_code=status)
