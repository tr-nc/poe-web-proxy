"""Opt-in paid live test: uv run python tests/live_smoke.py.

Requires `poe-web-proxy login` first. Starts and stops its own loopback server;
never prints or persists its randomly generated local client key.
"""

import os
import secrets
import socket
import subprocess
import sys
import time

import httpx
from openai import OpenAI, RateLimitError


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    key = secrets.token_hex(32)
    env = {**os.environ, "POE_PROXY_API_KEY": key, "POE_PROXY_MODELS": "GPT-6.1-Sol"}
    server = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from poe_web_proxy.cli import main; main()",
            "serve",
            "--session",
            os.environ.get("POE_PROXY_LIVE_SESSION", "poe-web-proxy"),
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/v1"
    client = OpenAI(api_key=key, base_url=url, timeout=170, max_retries=0)
    model = "GPT-6.1-Sol"

    def completion(prompt, **kwargs):
        return client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}], **kwargs
        )

    try:
        for _ in range(100):
            if server.poll() is not None:
                raise RuntimeError("Live server exited during startup")
            try:
                r = httpx.get(url + "/models", headers={"Authorization": "Bearer " + key})
                if r.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Live server did not start")
        assert httpx.get(url + "/models").status_code == 401
        marker = "POE_LIVE_" + secrets.token_hex(4)
        r = completion("Reply with exactly this string and nothing else: " + marker)
        assert r.choices[0].message.content.strip() == marker
        assert r.choices[0].finish_reason == "stop"
        print("PASS: SDK non-stream exact response", flush=True)

        parts = []
        finish = None
        with completion(
            "Count from 1 to 20, one number per line. No other text.", stream=True
        ) as stream:
            for chunk in stream:
                if chunk.choices:
                    parts.append(chunk.choices[0].delta.content or "")
                    finish = chunk.choices[0].finish_reason or finish
        assert "".join(parts).strip().splitlines() == [str(i) for i in range(1, 21)]
        assert finish == "stop"
        print(
            "PASS: SDK streaming content and finish; content deltas:",
            len([p for p in parts if p]),
            flush=True,
        )

        with completion(
            "Count from 1 to 100, one number per line. No other text.", stream=True
        ) as stream:
            first = next(iter(stream))
            assert first.choices[0].delta.role == "assistant"
            try:
                completion("This request must be rejected as busy")
            except RateLimitError:
                print("PASS: overlapping request rejected with 429", flush=True)
            else:
                raise AssertionError("Overlapping request was not rejected")
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    break
            # Context manager closes the HTTP stream before completion.
        print("Closed active stream; checking recovery", flush=True)
        marker = "POE_RECOVERED_" + secrets.token_hex(4)
        for _ in range(100):
            try:
                r = completion("Reply with exactly this string and nothing else: " + marker)
                break
            except RateLimitError:
                time.sleep(0.5)
        else:
            raise AssertionError("Browser stayed busy after disconnect")
        assert r.choices[0].message.content.strip() == marker
        print("PASS: disconnected stream cleaned up; fresh SDK request succeeded", flush=True)
    finally:
        client.close()
        server.terminate()
        try:
            server.wait(timeout=50)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


if __name__ == "__main__":
    main()
