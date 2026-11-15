"""Pi's outer retry layer sees only the status/message, not SDK response headers."""

import pytest
from fastapi.testclient import TestClient
from openai import APIStatusError, OpenAI
from test_active_recovery import MODEL, LivePage
from test_active_recovery import fast_poll as fast_poll  # noqa: PLC0414 — pytest fixture export

from poe_web_proxy.app import create_app
from poe_web_proxy.sessions import SessionStore


def test_invalid_output_is_nontransient_for_sdk_and_outer_client(tmp_path, fast_poll):
    page = LivePage()
    page.finish_after_reads = 1
    page.answer = '{"type":"tool_calls","calls":[]}'
    with TestClient(create_app(page, "test-key", (MODEL,), SessionStore(tmp_path / "db"))) as c:
        sdk = OpenAI(api_key="test-key", base_url="http://testserver/v1/agent", http_client=c)
        with pytest.raises(APIStatusError) as caught:
            sdk.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": "test"}],
                extra_headers={"x-session-affinity": "retry-status"},
            )
        assert caught.value.status_code == 422
        assert caught.value.response.headers["x-should-retry"] == "false"
        assert page.sends == 1
