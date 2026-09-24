"""Tests for the GitHub tarball snapshot download used when there is no
local checkout to validate a fix against."""

from __future__ import annotations

import io
import tarfile

import httpx
import pytest

from app.services.github_client import download_repository_snapshot


def _make_tarball(top_level_name: str, files: dict[str, str]) -> bytes:
    """Build an in-memory .tar.gz matching the shape GitHub's tarball
    endpoint actually returns: one top-level directory containing the repo."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for relative, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=f"{top_level_name}/{relative}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _response(status_code, content=b""):
    return httpx.Response(
        status_code, content=content, request=httpx.Request("GET", "https://api.github.com")
    )


def test_a_missing_token_never_makes_a_network_call(monkeypatch, tmp_path):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("httpx.get should not be called without a token")

    monkeypatch.setattr(httpx, "get", unexpected)
    assert download_repository_snapshot(None, "acme/x", "abc123", str(tmp_path)) is None


def test_a_valid_tarball_is_extracted_to_its_one_top_level_directory(monkeypatch, tmp_path):
    tarball = _make_tarball(
        "acme-rag-service-abcdef1",
        {"src/rag/retriever.py": "TOP_K = 4\n", "README.md": "hello\n"},
    )

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        assert url == "https://api.github.com/repos/acme/rag-service/tarball/abc123"
        assert headers["Authorization"] == "Bearer gho_test"
        assert follow_redirects is True
        return _response(200, tarball)

    monkeypatch.setattr(httpx, "get", fake_get)

    result = download_repository_snapshot("gho_test", "acme/rag-service", "abc123", str(tmp_path))

    assert result is not None
    extracted = list_files = list((tmp_path / "acme-rag-service-abcdef1").rglob("*.py"))
    assert result.endswith("acme-rag-service-abcdef1")
    assert (tmp_path / "acme-rag-service-abcdef1" / "src" / "rag" / "retriever.py").read_text() == (
        "TOP_K = 4\n"
    )


def test_a_404_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(404))
    assert download_repository_snapshot("gho_test", "acme/x", "deadbeef", str(tmp_path)) is None


def test_a_corrupt_archive_returns_none_not_an_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(200, b"not a real tarball"))
    assert download_repository_snapshot("gho_test", "acme/x", "deadbeef", str(tmp_path)) is None


def test_a_network_failure_returns_none(monkeypatch, tmp_path):
    def raise_error(*_args, **_kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "get", raise_error)
    assert download_repository_snapshot("gho_test", "acme/x", "deadbeef", str(tmp_path)) is None


def test_a_tarball_with_no_single_top_level_directory_is_rejected(monkeypatch, tmp_path):
    """GitHub's tarballs always have exactly one top-level directory; a
    result with zero or several is not one this code should trust."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = b"loose file, no containing directory\n"
        info = tarfile.TarInfo(name="loose.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _response(200, buffer.getvalue()))
    assert download_repository_snapshot("gho_test", "acme/x", "deadbeef", str(tmp_path)) is None
