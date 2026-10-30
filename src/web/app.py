from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config.favorites import FavoriteModelId, FavoritesError
from src.core.sources.conversations import ChatMetadataError
from src.providers.catalog import CatalogUnavailable
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

logger = get_logger("web")

_STATIC = Path(__file__).parent / "static"
_COOKIE = "recall_user"


class LoginIn(BaseModel):
    user_id: str


class MessageIn(BaseModel):
    content: str = ""
    attachments: list[ChatAttachment] = Field(default_factory=list)


class ModelIn(BaseModel):
    model_id: FavoriteModelId
    zdr: bool = Field(default=True, strict=True)


def _sse(event: str, data, generation_id: str = "") -> str:
    event_id = f"id: {generation_id}\n" if generation_id else ""
    return f"{event_id}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(service: Optional[ChatService] = None) -> FastAPI:
    """Web channel: an HTTP/SSE adapter over `ChatService`.

    The user comes from a cookie set at login. This is identification, not authentication:
    anyone who can reach the server can claim any existing user.
    """
    service = service or ChatService()
    app = FastAPI(title="RECALL")

    def current_user(request: Request) -> str:
        user_id = request.cookies.get(_COOKIE)
        if not user_id:
            raise HTTPException(401, "not logged in")
        return user_id

    @app.exception_handler(UnknownUser)
    def _unknown_user(request: Request, exc: UnknownUser):
        return _error(401, exc)

    @app.exception_handler(UnknownAgent)
    @app.exception_handler(UnknownChat)
    def _not_found(request: Request, exc: ServiceError):
        return _error(404, exc)

    @app.exception_handler(ValueError)
    def _bad_request(request: Request, exc: ValueError):
        return _error(400, exc)

    @app.exception_handler(CatalogUnavailable)
    def _catalog_unavailable(request: Request, exc: CatalogUnavailable):
        logger.warning("model catalog unavailable: %s", exc)
        return _error(502, exc)

    @app.exception_handler(FavoritesError)
    @app.exception_handler(ChatMetadataError)
    def _model_preferences_error(request: Request, exc: FavoritesError | ChatMetadataError):
        logger.error("model preferences operation failed: %s", exc)
        return _error(500, exc)

    @app.exception_handler(AgentBusy)
    def _agent_busy(request: Request, exc: AgentBusy):
        return _error(409, exc)

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"  # always revalidate the frontend
        return response

    # ------------------------------------------------------------------ session

    @app.post("/api/login")
    def login(body: LoginIn, response: Response):
        try:
            profile = service.login(body.user_id.strip())
        except UnknownUser as e:
            raise HTTPException(404, str(e)) from e
        response.set_cookie(_COOKIE, profile["id"], httponly=True, samesite="strict")
        return profile

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(_COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(user_id: str = Depends(current_user)):
        return service.login(user_id)

    @app.get("/api/models")
    def list_models(q: str = "", zdr: bool = True, user_id: str = Depends(current_user)):
        return service.list_models(user_id, q, zdr=zdr)

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

    @app.get("/api/agents/{agent_id}/chats")
    def list_chats(agent_id: str, keep: Optional[str] = None, user_id: str = Depends(current_user)):
        return service.list_chats(user_id, agent_id, keep)

    @app.post("/api/agents/{agent_id}/chats", status_code=201)
    def create_chat(agent_id: str, user_id: str = Depends(current_user)):
        return service.create_chat(user_id, agent_id)

    @app.get("/api/agents/{agent_id}/chats/{chat_id}")
    def get_chat(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        return service.get_chat(user_id, agent_id, chat_id)

    @app.put("/api/agents/{agent_id}/chats/{chat_id}/model")
    def set_chat_model(
        agent_id: str, chat_id: str, body: ModelIn, user_id: str = Depends(current_user),
    ):
        return service.set_chat_model(user_id, agent_id, chat_id, body.model_id, zdr=body.zdr)

    @app.delete("/api/agents/{agent_id}/chats/{chat_id}", status_code=204)
    def delete_chat(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        service.delete_chat(user_id, agent_id, chat_id)

    @app.post("/api/agents/{agent_id}/chats/{chat_id}/messages")
    def send_message(
        agent_id: str, chat_id: str, body: MessageIn, user_id: str = Depends(current_user)
    ):
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
                yield _sse("error", str(e), generation_id)
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


def _error(status: int, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=status)
