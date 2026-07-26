import json
import urllib.error

import pytest

from core import util


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, maximum):
        return self.payload[:maximum]


def _install_response(monkeypatch, payload):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return _Response(payload)

    monkeypatch.setattr(util.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_update_check_accepts_v_prefixed_tag_and_ignores_malformed_entries(monkeypatch):
    payload = json.dumps(
        [
            {"name": "not-a-version", "html_url": "https://example.invalid/release"},
            {
                "tag_name": "v5.1.0",
                "html_url": "https://github.com/AiWithYou/dupeguru_neo/releases/tag/v5.1.0",
            },
        ]
    ).encode("utf-8")
    calls = _install_response(monkeypatch, payload)

    result = util.check_for_update("5.0.0")

    assert str(result["version"]) == "5.1.0"
    assert result["url"].endswith("/tag/v5.1.0")
    assert calls == [(util.UPDATE_API_URL, util.UPDATE_REQUEST_TIMEOUT_SECONDS)]


def test_update_check_rejects_oversized_response(monkeypatch):
    _install_response(monkeypatch, b"[" + b" " * util.MAX_UPDATE_RESPONSE_BYTES + b"]")

    assert util.check_for_update("5.0.0") is None


@pytest.mark.parametrize(
    "payload",
    (
        b"{}",
        b"not-json",
        json.dumps(
            [
                {
                    "tag_name": "v5.1.0",
                    "html_url": "https://attacker.invalid/v5.1.0",
                }
            ]
        ).encode("utf-8"),
    ),
)
def test_update_check_fails_closed_for_untrusted_payloads(monkeypatch, payload):
    _install_response(monkeypatch, payload)

    assert util.check_for_update("5.0.0") is None


def test_update_check_handles_network_failure(monkeypatch):
    def fail(_request, timeout):
        assert timeout == util.UPDATE_REQUEST_TIMEOUT_SECONDS
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(util.urllib.request, "urlopen", fail)

    assert util.check_for_update("5.0.0") is None


def test_update_response_read_is_bounded():
    response = _Response(b"[]")
    assert response.read(util.MAX_UPDATE_RESPONSE_BYTES + 1) == b"[]"
