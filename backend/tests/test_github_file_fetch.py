"""Tests for the synchronous GitHub file fetch used as the Fix agent's
fallback when no local checkout is available."""

from __future__ import annotations

import base64

import httpx
import pytest

from app.services.github_client import fetch_file_sync


def _response(status_code, json_body):
    return httpx.Response(
        status_code, json=json_body, request=httpx.Request("GET", "https://api.github.com")
    )


def test_a_missing_token_never_makes_a_network_call(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("httpx.get should not be called without a token")

    monkeypatch.setattr(httpx, "get", unexpected)
    assert fetch_file_sync(None, "acme/rag-service", "src/rag/retriever.py", "abc123") is None

def test_a_base64_encoded_file_is_decoded(monkeypatch):
    content = "TOP_K = 4\n"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

    def fake_get(url, params=None, headers=None, timeout=None):
        assert "src/rag/retriever.py" in url
        assert params == {"ref": "abc123"}
        assert headers["Authorization"] == "Bearer gho_test"
        return _response(200, {"encoding": "base64", "content": encoded})

    monkeypatch.setattr(httpx, "get", fake_get)
    result = fetch_file_sync("gho_test", "acme/rag-service", "src/rag/retriever.py", "abc123")
    assert result == content


def test_a_404_returns_none_not_an_exception(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(404, {"message": "Not Found"}))
    assert fetch_file_sync("gho_test", "acme/x", "missing.py", "abc123") is None


def test_a_network_failure_returns_none(monkeypatch):
    def raise_error(*_args, **_kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "get", raise_error)
    assert fetch_file_sync("gho_test", "acme/x", "f.py", "abc123") is None
