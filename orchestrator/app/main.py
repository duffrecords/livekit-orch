"""HTTP API for starting, inspecting and stopping voice-agent sessions."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from typing import Any, Awaitable, Callable

import aiohttp
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from livekit import api
from pydantic import BaseModel, Field

from .config import Settings
from .logging import setup_logging
from .sessions import SessionNotFound, SessionService, UnknownAgent
from .workers import WorkerDirectory

logger = logging.getLogger("orchestrator")

READY_TIMEOUT_SECONDS = 2.0
PROBE_PATHS = {"/healthz", "/readyz"}


class StartSessionRequest(BaseModel):
    # omitted: the default agent; null: a room with no agent
    agent: str | None = None
    identity: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, Any] | None = None


class DispatchRequest(BaseModel):
    agent: str
    metadata: dict[str, Any] | None = None


class TokenRequest(BaseModel):
    identity: str | None = Field(default=None, min_length=1, max_length=128)


router = APIRouter()


def _sessions(request: Request) -> SessionService:
    return request.app.state.sessions


@router.get("/healthz", tags=["health"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", tags=["health"])
async def readyz(request: Request) -> JSONResponse:
    try:
        await asyncio.wait_for(_sessions(request).ping(), READY_TIMEOUT_SECONDS)
    except Exception as err:
        logger.warning("not ready: LiveKit unreachable", extra={"error": str(err) or repr(err)})
        return JSONResponse({"status": "livekit unreachable"}, status_code=503)
    return JSONResponse({"status": "ok"})


@router.post("/sessions", status_code=201, tags=["sessions"])
async def start_session(request: Request, body: StartSessionRequest | None = None):
    body = body or StartSessionRequest()
    settings: Settings = request.app.state.settings
    agent = body.agent if "agent" in body.model_fields_set else settings.agent_names[0]
    session = await _sessions(request).start(agent, body.identity, body.metadata)
    request.state.session_id = session["id"]
    return session


@router.get("/sessions", tags=["sessions"])
async def list_sessions(request: Request, include: str | None = None):
    return {"sessions": await _sessions(request).list_sessions(include_ended=include == "ended")}


@router.get("/sessions/{session_id}", tags=["sessions"])
async def get_session(request: Request, session_id: str):
    return await _sessions(request).get(session_id)


@router.delete("/sessions/{session_id}", status_code=204, tags=["sessions"])
async def stop_session(request: Request, session_id: str) -> Response:
    await _sessions(request).stop(session_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/dispatches", status_code=201, tags=["sessions"])
async def dispatch_agent(request: Request, session_id: str, body: DispatchRequest):
    return await _sessions(request).dispatch(session_id, body.agent, body.metadata)


@router.post("/sessions/{session_id}/tokens", status_code=201, tags=["sessions"])
async def create_token(request: Request, session_id: str, body: TokenRequest | None = None):
    return await _sessions(request).token(session_id, body.identity if body else None)


@router.get("/workers", tags=["scaling"])
async def list_workers(request: Request):
    return {"workers": await _sessions(request).workers()}


@router.get("/scaling/{agent}", tags=["scaling"])
async def scaling_demand(request: Request, agent: str):
    return await _sessions(request).demand(agent)


@router.post("/webhooks/livekit", status_code=204, tags=["webhooks"])
async def livekit_webhook(request: Request) -> Response:
    body = (await request.body()).decode()
    try:
        event = request.app.state.webhooks.receive(body, request.headers.get("Authorization", ""))
    except Exception:
        raise HTTPException(status_code=401, detail="invalid webhook signature") from None
    _sessions(request).handle_webhook(event)
    return Response(status_code=204)


async def _every(interval: float, name: str, task: Callable[[], Awaitable[Any]]) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await task()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            logger.warning(f"{name} pass skipped: LiveKit unreachable", extra={"error": str(err)})
        except Exception:
            logger.exception(f"{name} pass failed")


def create_app(
    settings: Settings | None = None,
    lk: api.LiveKitAPI | None = None,
    workers: WorkerDirectory | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = lk or api.LiveKitAPI(
            settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
        )
        directory = workers or WorkerDirectory(settings)
        sessions = app.state.sessions = SessionService(client, settings, workers=directory)
        background = [
            asyncio.create_task(_every(settings.reaper_interval_seconds, "reaper", sessions.reap))
        ]
        if settings.redispatch_after_seconds > 0:
            background.append(
                asyncio.create_task(
                    _every(settings.redispatch_interval_seconds, "re-dispatch", sessions.redispatch)
                )
            )
        logger.info(
            "orchestrator started",
            extra={"livekit_url": settings.livekit_url, "agents": list(settings.agent_names)},
        )
        yield
        for task in background:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await directory.aclose()
        if lk is None:
            await client.aclose()

    app = FastAPI(title="livekit-orch orchestrator", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.webhooks = api.WebhookReceiver(
        api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
    )
    app.include_router(router)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        logger.log(
            logging.DEBUG if request.url.path in PROBE_PATHS else logging.INFO,
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "session_id": getattr(request.state, "session_id", None)
                or request.path_params.get("session_id"),
            },
        )
        return response

    @app.exception_handler(SessionNotFound)
    async def session_not_found(_: Request, err: SessionNotFound) -> JSONResponse:
        return JSONResponse({"detail": str(err)}, status_code=404)

    @app.exception_handler(UnknownAgent)
    async def unknown_agent(_: Request, err: UnknownAgent) -> JSONResponse:
        return JSONResponse({"detail": str(err)}, status_code=422)

    @app.exception_handler(api.TwirpError)
    async def livekit_error(_: Request, err: api.TwirpError) -> JSONResponse:
        if err.code == "not_found":
            return JSONResponse({"detail": err.message}, status_code=404)
        logger.error("LiveKit call failed", extra={"code": err.code, "error": err.message})
        return JSONResponse({"detail": f"LiveKit error ({err.code}): {err.message}"}, status_code=502)

    @app.exception_handler(aiohttp.ClientError)
    @app.exception_handler(asyncio.TimeoutError)
    async def livekit_unreachable(_: Request, err: Exception) -> JSONResponse:
        logger.error("LiveKit unreachable", extra={"error": str(err) or repr(err)})
        return JSONResponse({"detail": "LiveKit server unreachable"}, status_code=503)

    return app
