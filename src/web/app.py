from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.service.chat import ChatService, ServiceError, UnknownAgent, UnknownChat, UnknownUser
from src.utils.logger import get_logger

logger = get_logger("web")

_STATIC = Path(__file__).parent / "static"
_COOKIE = "recall_user"


class LoginIn(BaseModel):
    user_id: str


class MessageIn(BaseModel):
    content: str


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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

    @app.delete("/api/agents/{agent_id}/chats/{chat_id}", status_code=204)
    def delete_chat(agent_id: str, chat_id: str, user_id: str = Depends(current_user)):
        service.delete_chat(user_id, agent_id, chat_id)

    @app.post("/api/agents/{agent_id}/chats/{chat_id}/messages")
    def send_message(
        agent_id: str, chat_id: str, body: MessageIn, user_id: str = Depends(current_user)
    ):
        events = service.send(user_id, agent_id, chat_id, body.content, stream=True)

        def stream() -> Iterator[str]:
            try:
                for event in events:
                    yield _sse(event.kind, event.data if event.kind == "tool" else event.text)
            except Exception as e:  # the response has started: report the failure in-band
                logger.exception("reply failed for %s/%s/%s", user_id, agent_id, chat_id)
                yield _sse("error", str(e))
            yield _sse("done", "")

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ------------------------------------------------------------------ frontend

    @app.get("/")
    def index():
        return FileResponse(_STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
    return app


def _error(status: int, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=status)
