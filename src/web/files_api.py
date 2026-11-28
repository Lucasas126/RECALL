"""`/api/files/*`: the read-only file panel routes (logic in `src.service.files`)."""
from __future__ import annotations

import posixpath
import urllib.parse
import uuid
from typing import Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from src.service.files import FileError, FileGrants, FilePanel
from src.utils.file_kinds import file_kind

SID_KEY = "files_sid"


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str


def session_id(request: Request) -> str:
    sid = request.session.get(SID_KEY)
    if not sid:
        sid = request.session[SID_KEY] = uuid.uuid4().hex
    return sid


def load_panel(service, agent: str, owner: str, grants: FileGrants, sid: str) -> FilePanel:
    """The agent's roots: its readable sources and, with the terminal group, its backends' sessions."""
    from src.agent.tools import chat_folder_options
    from src.agent.toolsets.terminal import build_terminal_tools
    from src.config.ceiling import restrict_config
    from src.config.loader import load_agent
    from src.core.sources.registry import SourceRegistry

    service._owned_agent(owner, agent)
    config = load_agent(owner, agent)
    sources = SourceRegistry.for_agent(config.user_id, config.id, **chat_folder_options(config))
    sessions = None
    if config.tools and "terminal" in set(config.tool_groups or ()):
        tools = build_terminal_tools(restrict_config(config))
        sessions = getattr(tools[0], "sessions", None) if tools else None
    return FilePanel(owner, agent, sources, sessions, grants, sid)


def build_router(service, current_user: Callable, grants: FileGrants) -> APIRouter:
    router = APIRouter()

    def panel(request: Request, agent: str, owner: str) -> FilePanel:
        return load_panel(service, agent, owner, grants, session_id(request))

    def respond(call: Callable[[], Response | dict]):
        try:
            return call()
        except FileError as exc:
            return JSONResponse(exc.body, status_code=exc.status)

    def agent_param(agent: str = Query(..., min_length=1)) -> str:
        return agent

    def owner_of(agent: str = Depends(agent_param), user_id: str = Depends(current_user)) -> str:
        return service.agent_owner(user_id, agent)

    @router.get("/api/files/roots")
    async def roots(request: Request, agent: str = Depends(agent_param), owner: str = Depends(owner_of)):
        return await run_in_threadpool(lambda: respond(lambda: {"roots": panel(request, agent, owner).roots()}))

    @router.get("/api/files/list")
    async def list_files(request: Request, root: str = "", path: str = "", agent: str = Depends(agent_param),
                         owner: str = Depends(owner_of)):
        return await run_in_threadpool(lambda: respond(lambda: panel(request, agent, owner).list(root, path)))

    @router.get("/api/files/info")
    async def info(request: Request, root: str = "", path: str = "", agent: str = Depends(agent_param),
                   owner: str = Depends(owner_of)):
        return await run_in_threadpool(lambda: respond(lambda: panel(request, agent, owner).info(root, path)))

    @router.get("/api/files/content")
    async def content(request: Request, root: str = "", path: str = "", download: int = 0,
                      agent: str = Depends(agent_param), owner: str = Depends(owner_of)):
        def run():
            name, data = panel(request, agent, owner).content(root, path)
            kind, mime = file_kind(name)
            headers = {"X-Content-Type-Options": "nosniff"}
            if kind == "html" or name.lower().endswith(".svg"):
                headers["Content-Security-Policy"] = "sandbox"
            quoted = urllib.parse.quote(posixpath.basename(name))
            headers["Content-Disposition"] = f"{'attachment' if download else 'inline'}; filename*=UTF-8''{quoted}"
            return Response(data, media_type=mime, headers=headers)
        return await run_in_threadpool(lambda: respond(run))

    @router.get("/api/files/pdf-page")
    async def pdf_page(request: Request, root: str = "", path: str = "", page: int = 1, width: int = 800,
                       agent: str = Depends(agent_param), owner: str = Depends(owner_of)):
        def run():
            png = panel(request, agent, owner).pdf_page(root, path, page, width)
            return Response(png, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})
        return await run_in_threadpool(lambda: respond(run))

    @router.post("/api/files/grant")
    async def grant(request: Request, body: GrantIn, agent: str = Depends(agent_param), owner: str = Depends(owner_of)):
        def run():
            panel(request, agent, owner).grant(body.backend)
            return {"ok": True}
        return await run_in_threadpool(lambda: respond(run))

    return router
