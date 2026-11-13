# Poe Web Proxy

A **browser-backed, local-only** adapter exposing Poe web chat through an
OpenAI-compatible Chat Completions endpoint. Independent MIT implementation;
not affiliated with Poe or OpenAI. It uses an actual logged-in browser, not
Poe's official API. It is not a browser-free GraphQL client.

## Run

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and
[agent-browser](https://github.com/vercel-labs/agent-browser) on PATH with its
Chromium installed (`agent-browser install`).

```sh
uv sync
uv run poe-web-proxy login
# Authenticate in the dedicated visible window, then:
export POE_PROXY_API_KEY="$(openssl rand -hex 32)"
uv run poe-web-proxy serve
```

`login` opens a visible window only for authentication. `serve` captures any
live session before switching it to **headless**, so no browser window needs to
stay open. On service shutdown it saves the session and closes its browser;
subsequent starts restore the saved login. An expired login still requires you
to stop the service, run `login` again, and restart `serve`.

Use a session dedicated to this service; do not run login or manipulate that
session while the service runs. To use a different session, pass `--session NAME`
to both commands. If already running, log in manually rather than trying to
replace its startup state.

Optional first-launch import of your own cookie export (array format):

```sh
uv run poe-web-proxy login --cookies "$HOME/Downloads/poe.com_cookies.json"
```

Cookie imports are restricted to poe.com. Temporary import/migration state is
mode 0600 and removed after browser startup. Persistent auth is managed by
agent-browser's `--restore` facility, outside this repository. Those files,
cookie exports, and browser data are sensitive. Headless idle shutdown is
disabled while the service owns the session; stopping the service closes it.

```sh
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer $POE_PROXY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"GPT-6.1-Sol","messages":[{"role":"user","content":"Hello world"}],"stream":true}'
```

Or use `OpenAI(api_key=os.environ["POE_PROXY_API_KEY"],
base_url="http://127.0.0.1:8765/v1")` with `client.chat.completions.create(...)`.
The client key is a **local proxy secret**, not a Poe API key or session cookie.

## Boundaries

- One user text message per request; every request starts a fresh chat.
- Real incremental DOM text is emitted as SSE deltas; not simulated tokenization.
  Poe may deliver short replies in a single delta. Text revisions fail rather
  than silently corrupting the output. Stream errors are SSE error objects,
  without a false success finish or `[DONE]`.
- No tools, system messages, history, attachments, Responses API, token accounting,
  or generation parameters. Unsupported fields are rejected, not silently ignored.
- One in-flight request per browser; others receive 429. Cleanup leaves the chat
  page before the next request; cleanup failures require a service restart.
  Disconnect/timeout cleanup does **not** guarantee Poe cancels server-side work
  or refunds points. Chats are not automatically deleted.
- Default allowlist: `GPT-6.1-Sol`. Configure comma-separated bot handles with
  `POE_PROXY_MODELS`. This list is operator configuration, not provider discovery
  or a guarantee of access. Requires your own account's model permissions/points.
- `--timeout` bounds generation (default 120 seconds). Cleanup can take up to
  another 40 seconds. `--port` changes the default 8765; binding stays loopback.
- Poe DOM selectors are unversioned and can break; headless access can also be
  challenged by the site. Stop the service and run `login` for visible diagnostics
  when errors occur; do not use this as a production or shared public gateway.
- Cookie exports and HARs must stay outside Git. Do not submit credentials to
  third-party proxies. Respect Poe's terms, account restrictions and rate limits.

## Check

```sh
uv run pytest
uv run ruff check .
uv run pyright
```

Tests exercise the HTTP contract with a fake transport and the browser adapter
with scripted states. They do not prove current Poe availability. After login,
run the opt-in live check (uses your Poe points and sends four messages):

```sh
uv run python tests/live_smoke.py
```

It starts and stops a temporary loopback server, uses the real OpenAI SDK to
check exact non-stream output, streaming, overlapping-request rejection, and
recovery after closing an active stream. Set `POE_PROXY_LIVE_SESSION` when using
a non-default browser session. It verifies headless mode and shuts down both its
proxy server and browser. The saved login remains available for the next start.

## License

MIT. No code is copied from GPL Poe wrappers.
