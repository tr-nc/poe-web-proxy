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

## macOS login-time startup

After login and `uv sync`, install the LaunchAgent once:

```sh
uv run poe-web-proxy service install
~/.local/bin/poe-proxy status
```

Installation starts the service immediately and at subsequent **user logins**
(not before login at boot). It installs `~/.local/bin/poe-proxy`; if that directory
is on PATH, `poe-proxy` with no arguments starts the service. It does not require
an interactive shell, zshrc, or a network package sync on startup.

```sh
poe-proxy start      # idempotent, preserves active requests
poe-proxy stop       # stays stopped until manual start or the next login
poe-proxy restart   # interrupts active requests
poe-proxy status    # API readiness, logs and key path; never prints the key
poe-proxy uninstall # remove autostart; retain credentials and logs
```

The persistent local client key is stored in
`~/Library/Application Support/poe-web-proxy/api-key` (0600); it is not embedded
in the plist. Clients must use this key rather than a newly generated one.
Logs are under `~/Library/Logs/poe-web-proxy/`. Reinstalling preserves the key.
The LaunchAgent captures the installed Python and agent-browser paths; rerun
`service install` after moving the checkout or replacing the Node installation.
A loaded/ready service does not prove Poe login or model access remains valid.
For reauthentication, stop the service, run `uv run poe-web-proxy login`, then
`poe-proxy start`. Do not run a manual `serve` alongside the managed service.

## Experimental agent/Pi endpoint

`/v1/agent/chat/completions` is separate from the plain chat endpoint. It accepts
text system/developer/user/assistant/tool history and JSON-schema function tools.
It serializes the transcript into a fresh web prompt: **roles and tool calls are
prompt-based emulation, not native Poe/OpenAI tool support**. Replies are buffered
until a complete JSON envelope is validated. Only a declared tool with valid
arguments is forwarded, at most one per turn; the proxy never executes tools.
The client (e.g. Pi) executes them under its own permissions. Treat this adapter
as experimental, particularly for destructive or security-sensitive operations.

Agent streaming emits the completed, validated answer/call as one SSE chunk, not
incremental model tokens. It reports neither native reasoning nor token usage or
billing. Numeric output-token caps and sampling parameters are rejected because
the web UI cannot enforce them. The serialized prompt is limited to 200000
characters; compact long transcripts. Image input is not supported.

For Pi, add a provider to `~/.pi/agent/models.json` (merge with existing entries):

```json
{
  "providers": {
    "poe-web": {
      "baseUrl": "http://127.0.0.1:8765/v1/agent",
      "api": "openai-completions",
      "apiKey": "!/bin/cat \"$HOME/Library/Application Support/poe-web-proxy/api-key\"",
      "compat": {
        "supportsStore": false,
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": false,
        "supportsUsageInStreaming": false,
        "supportsStrictMode": false,
        "promptCacheKeyMode": "disabled"
      },
      "models": [{
        "id": "Claude-Opus-5.5",
        "name": "Opus 5.5 via Poe (experimental tool emulation)",
        "reasoning": false,
        "input": ["text"],
        "contextWindow": 32000,
        "maxTokens": 4096,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "samplingParams": {"max_completion_tokens": null, "max_tokens": null}
      }]
    }
  }
}
```

The context metadata is a conservative adapter setting, not a claim about the
underlying model. Zero pricing means unreported cost, **not free Poe usage**.
The null sampling fields disable Pi's otherwise-sent output token cap. Add
`poe-web/Claude-Opus-5.5` to `enabledModels` in Pi's settings to include it in
Scoped Models, then `/reload` and select it with `/model`. Do not change your
normal default model just to configure this provider.

```sh
pi --provider poe-web --model Claude-Opus-5.5
uv run python tests/live_pi_smoke.py # opt-in paid read-tool round trip
```

## Plain chat boundaries

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
- Default allowlist: `GPT-6.1-Sol,Claude-Opus-5.5`. Configure comma-separated bot handles with
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
