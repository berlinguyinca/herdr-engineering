"""A pinned inference.model the gateway no longer serves is reported at startup (best effort, never raises)."""
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.inference_models import ModelResolver


@pytest.fixture
def gateway():
    seen = {"auth": None}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"] = self.headers.get("Authorization")
            body = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1", seen
    srv.shutdown()


def test_missing_model_warns_with_model_and_available_ids_but_no_key(gateway, caplog):
    url, _ = gateway
    caplog.set_level(logging.WARNING)
    assert ModelResolver(url, "sk-secret-key", []).check_configured("old-name") is False
    text = caplog.text
    assert "old-name" in text and "model-a" in text
    assert "sk-secret-key" not in text


def test_served_model_and_auto_are_silent(gateway, caplog):
    url, _ = gateway
    caplog.set_level(logging.WARNING)
    r = ModelResolver(url, "", [])
    assert r.check_configured("model-a") is True
    assert r.check_configured("auto") is True
    assert r.check_configured("") is True
    assert caplog.text == ""


def test_unreachable_gateway_never_raises_or_warns(caplog):
    caplog.set_level(logging.WARNING)
    assert ModelResolver("http://127.0.0.1:9/v1", "", []).check_configured("x") is True
    assert caplog.text == ""


def test_service_startup_runs_the_check_in_the_background(gateway, caplog):
    url, _ = gateway
    caplog.set_level(logging.WARNING)
    cfg = assistant_config({"personal_assistant": {"enabled": True, "inference": {"endpoint": url, "model": "renamed-away"}}})
    AssistantService(config=cfg)
    deadline = time.time() + 5
    while "renamed-away" not in caplog.text and time.time() < deadline:
        time.sleep(0.05)
    assert "renamed-away" in caplog.text
