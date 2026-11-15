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

from .agent import AgentCompletion, ImagePart, Text
from .browser import BrowserError
from .images import checked_attachments
from .sessions import AgentTurn, SessionConflict, SessionStore, session_id
from .turn import BrowserTurn, ReasoningEffort


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
    def generate(
        self, model: str, prompt: str, turn: BrowserTurn | None = None
    ) -> AsyncGenerator[str, None]: ...
    async def reset(self) -> None: ...


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user"]
    content: str | list[Text | ImagePart] = Field(min_length=1, max_length=200000)


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    messages: list[Message] = Field(min_length=1, max_length=1)
    stream: bool = False
    reasoning_effort: ReasoningEffort | None = None


def error(message: str, status: int, code: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": code, "code": code}}, status_code=status
    )


def create_app(
    transport: Transport,
    api_key: str,
    models: tuple[str, ...],
    sessions: SessionStore | None = None,
) -> FastAPI:
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
        if request.url.path.startswith("/v1/agent/"):
            return error(
                "Invalid agent request: text/inline images and JSON-schema function tools only; "
                "reasoning_effort must be low/medium/high/xhigh/max; token limits must be null",
                400,
                "invalid_request_error",
            )
        return error(
            "Only one user message with text/inline images, model, and stream are supported; "
            "reasoning_effort may be low/medium/high/xhigh/max; "
            "tools, history, other generation parameters, and extra fields are not supported",
            400,
            "invalid_request_error",
        )

    @app.get("/v1/models")
    @app.get("/v1/agent/models")
    async def list_models(authorized: bool = Depends(auth)):
        if not authorized:
            return error("Invalid local API key", 401, "authentication_error")
        return {
            "object": "list",
            "data": [{"id": model, "object": "model", "owned_by": "poe-web"} for model in models],
        }

    @app.post("/v1/chat/completions")
    async def complete(body: Completion, request: Request, authorized: bool = Depends(auth)):
        if not authorized:
            return error("Invalid local API key", 401, "authentication_error")
        content = body.messages[0].content
        turn = None
        if isinstance(content, list):
            try:
                images = checked_attachments(
                    [part.image_url.attachment() for part in content if isinstance(part, ImagePart)]
                )
            except ValueError as exc:
                return error(str(exc), 400, "invalid_request_error")
            prompt = "\n".join(part.text for part in content if isinstance(part, Text))
            if len(prompt) > 200000:
                return error("Text exceeds 200000 characters", 400, "invalid_request_error")
            body = Completion(
                model=body.model,
                stream=body.stream,
                reasoning_effort=body.reasoning_effort,
                messages=[Message(role="user", content=prompt or "Describe the attached images.")],
            )
            turn = BrowserTurn(images=images) if images else None
        if body.reasoning_effort is not None:
            turn = turn or BrowserTurn()
            turn.reasoning_effort = body.reasoning_effort
        return await respond(body, request, authorized, turn)

    async def respond(
        body: Completion,
        request: Request,
        authorized: bool,
        turn: BrowserTurn | None = None,
        reserve: Callable[[], None] | None = None,
    ):
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
        try:
            if reserve:
                reserve()
        except SessionConflict as exc:
            busy = False
            return error(str(exc), 409, "session_conflict")
        except BaseException:
            busy = False
            raise
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
            prompt = body.messages[0].content
            assert isinstance(prompt, str)
            return (
                transport.generate(body.model, prompt, turn=turn)
                if turn is not None
                else transport.generate(body.model, prompt)
            )

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

    @app.post("/v1/agent/chat/completions")
    async def agent_complete(
        body: AgentCompletion, request: Request, authorized: bool = Depends(auth)
    ):
        if not authorized:
            return error("Invalid local API key", 401, "authentication_error")
        plan = None
        try:
            plan = AgentTurn(body, session_id(request.headers), sessions)
            # Reservation occurs only after the shared browser has been acquired.
            result = await respond(
                Completion(
                    model=body.model,
                    messages=[Message(role="user", content=plan.prompt)],
                    stream=False,
                ),
                request,
                authorized,
                plan.browser,
                plan.reserve,
            )
            if not isinstance(result, dict):
                return result
            message, finish = body.reply(
                result["choices"][0]["message"]["content"], plain_text=plan.browser.plain_text
            )
            plan.commit(message)
        except ValueError as exc:
            return error(str(exc), 400, "invalid_request_error")
        except SessionConflict as exc:
            return error(str(exc), 409, "session_conflict")
        except BrowserError as exc:
            return error(str(exc), 502, "upstream_error")
        finally:
            if plan:
                plan.abort()
        result["choices"][0].update(message=message, finish_reason=finish)
        if not body.stream:
            return result

        async def events():
            delta: dict[str, object] = {"role": "assistant"}
            if message.get("content") is not None:
                delta["content"] = message["content"]
            if "tool_calls" in message:
                delta["tool_calls"] = [
                    {"index": i, **call} for i, call in enumerate(message["tool_calls"])
                ]
            for payload, reason in ((delta, None), ({}, finish)):
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": result["id"],
                            "object": "chat.completion.chunk",
                            "created": result["created"],
                            "model": body.model,
                            "choices": [{"index": 0, "delta": payload, "finish_reason": reason}],
                        }
                    )
                    + "\n\n"
                )
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return app
