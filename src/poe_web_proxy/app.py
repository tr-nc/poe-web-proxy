"""Minimal OpenAI chat contract. Unsupported semantics are rejected, not simulated."""

import asyncio
import hmac
import json
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager, suppress
from typing import Literal, Protocol

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import Receive, Scope, Send

from .browser import BrowserError


class _OwnedStream(StreamingResponse):
    """Own cleanup even if the client disconnects before the generator starts."""

    def __init__(self, source: AsyncGenerator[str, None], cleanup: Callable[[], Awaitable[None]]):
        super().__init__(
            source, media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )
        self.source = source
        self.cleanup = cleanup

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.source.aclose()
            finally:
                await self.cleanup()


class Transport(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    def generate(self, model: str, prompt: str) -> AsyncGenerator[str, None]: ...
    async def reset(self) -> None: ...


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user"]
    content: str = Field(min_length=1, max_length=20000)


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    messages: list[Message] = Field(min_length=1, max_length=1)
    stream: bool = False


def error(message: str, status: int, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": code, "code": code}}, status_code=status
    )


def create_app(transport: Transport, api_key: str, models: tuple[str, ...]) -> FastAPI:
    if not api_key or not models:
        raise ValueError("A local API key and at least one model are required")
    busy = False
    healthy = True
    cleanup_tasks: set[asyncio.Task] = set()

    async def release():
        nonlocal busy, healthy
        try:
            await asyncio.wait_for(transport.reset(), timeout=40)
        except Exception:  # noqa: BLE001 — every cleanup failure must quarantine this session.
            # Do not let a potentially still-running browser contaminate another request.
            healthy = False
        finally:
            busy = False

    @asynccontextmanager
    async def lifespan(app):
        try:
            await transport.start()
            yield
        finally:
            if cleanup_tasks:
                await asyncio.gather(*cleanup_tasks)
            try:
                await release()
            finally:
                await transport.close()

    app = FastAPI(title="Poe Web Proxy", lifespan=lifespan, docs_url=None, redoc_url=None)

    async def auth(authorization: str = Header(default="")):
        return hmac.compare_digest(authorization.encode(), ("Bearer " + api_key).encode())

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return error(
            "Only one user text message, model, and stream are supported; "
            "tools, history, generation parameters, and extra fields are not supported",
            400,
            "invalid_request_error",
        )

    @app.get("/v1/models")
    async def list_models(authorized: bool = Depends(auth)):
        if not authorized:
            return error("Invalid local API key", 401, "authentication_error")
        return {
            "object": "list",
            "data": [{"id": model, "object": "model", "owned_by": "poe-web"} for model in models],
        }

    @app.post("/v1/chat/completions")
    async def complete(body: Completion, request: Request, authorized: bool = Depends(auth)):
        nonlocal busy
        if not authorized:
            return error("Invalid local API key", 401, "authentication_error")
        if body.model not in models:
            return error("Model is not configured", 404, "not_found_error")
        if not healthy:
            return error("Browser cleanup failed; restart the service", 503, "service_unavailable")
        # No await between inspection and reservation: one owner on the event loop.
        if busy:
            return error(
                "Browser is busy; retry after the current request", 429, "rate_limit_error"
            )
        busy = True
        identifier = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())

        def chunk(delta: dict, finish=None):
            return {
                "id": identifier,
                "object": "chat.completion.chunk",
                "created": created,
                "model": body.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        cleanup_task: asyncio.Task | None = None

        async def cleanup():
            nonlocal cleanup_task
            # A request owns exactly one cleanup, including early disconnects.
            if cleanup_task is None:
                cleanup_task = asyncio.create_task(release())
                cleanup_tasks.add(cleanup_task)
                cleanup_task.add_done_callback(cleanup_tasks.discard)
            with suppress(asyncio.CancelledError):
                await asyncio.shield(cleanup_task)

        def deltas():
            return transport.generate(body.model, body.messages[0].content)

        if body.stream:

            async def events():
                try:
                    yield "data: " + json.dumps(chunk({"role": "assistant"})) + "\n\n"
                    async with aclosing(deltas()) as source:
                        async for delta in source:
                            yield "data: " + json.dumps(chunk({"content": delta})) + "\n\n"
                    await cleanup()
                    yield "data: " + json.dumps(chunk({}, "stop")) + "\n\n"
                    yield "data: [DONE]\n\n"
                except (BrowserError, TimeoutError):
                    await cleanup()
                    yield (
                        'data: {"error":{"message":"Poe generation failed; check browser/login",'
                        '"type":"upstream_error","code":"upstream_error"}}\n\n'
                    )

            return _OwnedStream(events(), cleanup)
        try:
            pieces = []
            async with aclosing(deltas()) as source:
                async for delta in source:
                    if await request.is_disconnected():
                        raise BrowserError("Client disconnected")
                    pieces.append(delta)
            return {
                "id": identifier,
                "object": "chat.completion",
                "created": created,
                "model": body.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "".join(pieces)},
                    }
                ],
            }
        except TimeoutError:
            return error("Poe generation timed out", 504, "timeout_error")
        except BrowserError as exc:
            return error(str(exc), 502, "upstream_error")
        finally:
            await cleanup()

    return app
