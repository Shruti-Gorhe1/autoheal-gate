"""Isolated remediation.

A candidate patch is applied to a throwaway copy of the repository and the
test command is run there. The working tree the user cares about is never
touched, and a patch that cannot be proven green never reaches a PASS verdict.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
import uuid
import shlex
import re
from pathlib import Path
from typing import Any

from app.config import settings
from app.security.guards import safe_error, validate_execution_command
from app.gate.policy import changed_files_in_patch
from app.services.sandbox import sandbox_manager

logger = logging.getLogger("autoheal.remediation")

_EXCLUDED = shutil.ignore_patterns(
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
    ".pytest_cache", "dist", "build", "*.egg-info",
)


def _run(command: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )


def _apply_with_git(workspace: Path, patch_file: Path) -> tuple[bool, str]:
    """Apply a unified diff using Git without relying on the Unix patch utility."""
    if not (workspace / ".git").exists():
        init_result = _run(["git", "init", "-q"], workspace, timeout=30)
        if init_result.returncode != 0:
            return False, f"git init failed: {init_result.stderr.strip()[:300]}"

        add_result = _run(["git", "add", "-A"], workspace, timeout=60)
        if add_result.returncode != 0:
            return False, f"git add failed: {add_result.stderr.strip()[:300]}"

    errors = []

    for strip in ("-p1", "-p0"):
        result = _run(
            [
                "git",
                "apply",
                "--whitespace=nowarn",
                strip,
                str(patch_file),
            ],
            workspace,
            timeout=60,
        )

        if result.returncode == 0:
            return True, f"Applied with git apply {strip}."

        errors.append(
            f"git apply {strip}: {result.stderr.strip()[:300]}"
        )

    return False, " | ".join(errors)


def _run_step(
    command: str, workspace: Path, timeout: int
) -> tuple[bool, int, str, str, str | None]:
    """Run one shell step (setup or test) and normalize its outcome.

    Returns ``(succeeded, exit_code, stdout, stderr, timeout_reason)``; the
    last element is set only when the step itself timed out, so the caller
    can tell a timeout apart from an ordinary non-zero exit.
    """
    allowed, reason = validate_execution_command(command)
    if not allowed:
        return False, -1, "", reason or "Command blocked by security policy.", None
    try:
        # Test commands are commonly written as ``python -m pytest`` or
        # ``pytest``.  On Windows, invoking those through ``shell=True`` can
        # resolve a different Python installation than the interpreter that
        # is running AutoHeal (for example the system Python instead of the
        # project .venv).  That makes a correctly patched workspace appear
        # to fail validation with an otherwise misleading non-zero exit code.
        # Use the current interpreter for simple Python/pytest commands while
        # retaining shell execution for commands that intentionally use shell
        # syntax such as ``echo marker > setup_marker.txt``.
        simple = command.strip()
        python_match = re.match(r"^(?:python(?:\.exe)?|py)\s+(.+)$", simple, re.IGNORECASE)
        pytest_match = re.match(r"^pytest(?:\.exe)?(?:\s+(.*))?$", simple, re.IGNORECASE)

        if python_match:
            args = shlex.split(python_match.group(1), posix=False)
            executable_command = [sys.executable, *args]
            result = subprocess.run(
                executable_command,
                cwd=str(workspace),
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        elif pytest_match:
            args = shlex.split(pytest_match.group(1), posix=False) if pytest_match.group(1) else []
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *args],
                cwd=str(workspace),
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        else:
            result = subprocess.run(
                command,
                cwd=str(workspace),
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

        return result.returncode == 0, result.returncode, result.stdout, result.stderr, None
    except subprocess.TimeoutExpired as exc:
        return False, -1, exc.stdout or "", exc.stderr or "", f"exceeded {timeout} seconds"


def validate_patch(
    repo_path: str,
    patch: str,
    test_command: str | None = None,
    setup_command: str | None = None,
    timeout: int | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Copy, patch, (optionally) set up, test. Returns a structured record.

    ``setup_command`` runs once in the patched workspace before the test
    command -- typically a dependency install (``pip install -r
    requirements.txt``). It matters most for a workspace that came from
    :func:`validate_patch_from_snapshot`, which starts from a bare source
    tree with nothing installed; a local checkout usually already has its
    environment set up, but passing one there is equally supported.
    """
    command = test_command or settings.default_test_command
    limit = timeout or settings.test_timeout_seconds
    command_ok, command_reason = validate_execution_command(command)
    if not command_ok:
        return {"attempted": False, "patch_applied": False, "tests_passed": False, "reason": command_reason}
    if setup_command:
        setup_ok, setup_reason = validate_execution_command(setup_command)
        if not setup_ok:
            return {"attempted": False, "patch_applied": False, "tests_passed": False, "reason": setup_reason}
    source = Path(repo_path).resolve()

    if not source.is_dir():
        return {
            "attempted": False,
            "patch_applied": False,
            "tests_passed": False,
            "reason": f"The repository path does not exist: {source}",
        }

    sandbox = sandbox_manager.create(job_id or f"adhoc-{uuid.uuid4().hex}")
    temp_dir = sandbox.path
    workspace = temp_dir / source.name

    try:
        shutil.copytree(source, workspace, ignore=_EXCLUDED)

        patch_file = temp_dir / "candidate.patch"
        text = patch if patch.endswith("\n") else patch + "\n"
        patch_file.write_text(text, encoding="utf-8")

        applied, detail = _apply_with_git(workspace, patch_file)
        if not applied:
            return {
                "attempted": True,
                "patch_applied": False,
                "tests_passed": False,
                "reason": "The candidate patch did not apply cleanly.",
                "detail": detail,
                "changed_files": changed_files_in_patch(patch),
            }

        if setup_command:
            setup_ok, setup_exit, setup_out, setup_err, setup_timeout = _run_step(
                setup_command, workspace, limit
            )
            if not setup_ok:
                reason = (
                    f"The setup command {setup_timeout}."
                    if setup_timeout
                    else "The setup command failed before tests could run."
                )
                return {
                    "attempted": True,
                    "patch_applied": True,
                    "tests_passed": False,
                    "reason": reason,
                    "detail": detail,
                    "setup_command": setup_command,
                    "setup_exit_code": setup_exit,
                    "stdout": setup_out[-8000:],
                    "stderr": setup_err[-4000:],
                    "changed_files": changed_files_in_patch(patch),
                }

        passed, exit_code, stdout, stderr, test_timeout = _run_step(command, workspace, limit)
        reason = (
            f"The test command {test_timeout}."
            if test_timeout
            else "The patched workspace passed its test suite."
            if passed
            else "The patched workspace still fails its test suite."
        )

        return {
            "attempted": True,
            "patch_applied": True,
            "tests_passed": passed,
            "reason": reason,
            "detail": detail,
            "command": command,
            "exit_code": exit_code,
            "stdout": (stdout or "")[-8000:],
            "stderr": (stderr or "")[-4000:],
            "changed_files": changed_files_in_patch(patch),
        }

    except Exception as exc:
        logger.exception("Validation failed")
        return {
            "attempted": True,
            "patch_applied": False,
            "tests_passed": False,
            "reason": f"Validation could not complete: {safe_error(exc)}",
        }
    finally:
        sandbox_manager.cleanup(sandbox)


def validate_patch_from_snapshot(
    github_token: str | None,
    repository: str,
    commit_sha: str,
    patch: str,
    test_command: str | None = None,
    setup_command: str | None = None,
    timeout: int | None = None,
    provider: Any = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Download the exact commit, then validate exactly as
    :func:`validate_patch` would for a local checkout.

    This is what makes AutoHeal's loop complete for a repository the gate
    only knows about through a webhook: with no local clone registered
    anywhere, a candidate fix could previously only ever be proposed, never
    proven, which meant it could never reach a real, validated ``PASS`` --
    only an indefinite ``HOLD``. The downloaded snapshot is deleted when this
    returns, success or failure; nothing from it outlives this one call.

    ``provider``, when given, is used instead of the GitHub-specific
    download helper -- this is what lets validation stay provider-agnostic
    once an Azure or GCP provider exists. It is optional and additive:
    every existing caller that passes only ``github_token`` keeps working
    exactly as before, downloading straight from GitHub.
    """
    if provider is None and not github_token:
        return {
            "attempted": False,
            "patch_applied": False,
            "tests_passed": False,
            "reason": (
                "No local checkout is available and no GitHub token was "
                "provided, so the fix could not be executed."
            ),
        }

    sandbox = sandbox_manager.create(job_id or f"adhoc-{uuid.uuid4().hex}")
    download_dir = sandbox.path

    try:
        if provider is not None:
            snapshot_path = provider.download_snapshot_sync(
                repository, commit_sha, str(download_dir)
            )
        else:
            from app.services.github_client import download_repository_snapshot

            snapshot_path = download_repository_snapshot(
                github_token, repository, commit_sha, str(download_dir)
            )
        if snapshot_path is None:
            return {
                "attempted": False,
                "patch_applied": False,
                "tests_passed": False,
                "reason": (
                    f"Could not download {repository}@{commit_sha[:8]} from "
                    "GitHub to validate the candidate fix."
                ),
            }

        result = validate_patch(
            repo_path=snapshot_path,
            patch=patch,
            test_command=test_command,
            setup_command=setup_command,
            timeout=timeout,
            job_id=job_id,
        )
        result["source"] = "github-snapshot"
        return result

    finally:
        sandbox_manager.cleanup(sandbox)
