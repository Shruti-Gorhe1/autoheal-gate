"""Per-job filesystem sandboxes for AutoHeal validation.

The default implementation is intentionally zero-cost and platform-neutral:
each validation attempt gets a unique directory below ``WORKSPACE_ROOT`` and
is removed when the attempt finishes.  It isolates files between concurrent
jobs, but it is *not* an OS/container security boundary.  Docker/container
execution can be layered on later for untrusted code.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class Sandbox:
    job_id: str
    path: Path


class SandboxManager:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or settings.workspace_root).resolve() / "jobs"

    @staticmethod
    def _safe_job_id(job_id: str) -> str:
        value = _SAFE.sub("-", job_id).strip(".-")
        if not value:
            value = "job"
        return value[:120]

    def create(self, job_id: str) -> Sandbox:
        """Create a unique attempt directory for one job."""
        self.root.mkdir(parents=True, exist_ok=True)
        job_root = (self.root / self._safe_job_id(job_id)).resolve()
        if self.root not in job_root.parents:
            raise ValueError("Invalid job id for sandbox path")
        attempt = job_root / f"attempt-{uuid.uuid4().hex}"
        attempt.mkdir(parents=True, exist_ok=False)
        return Sandbox(job_id=job_id, path=attempt)

    @staticmethod
    def _remove_readonly(func, path, _exc_info) -> None:
        """Make a read-only Windows file writable and retry the removal."""
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            raise

    def cleanup(self, sandbox: Sandbox) -> None:
        """Remove only the sandbox created for this attempt.

        Windows can briefly hold files created by Git/Python tooling (or mark
        generated files read-only).  Retry a few times and handle read-only
        files so a successful validation never leaves its workspace behind.
        """
        path = sandbox.path.resolve()
        if self.root not in path.parents:
            raise ValueError("Refusing to clean a path outside the sandbox root")

        last_error: OSError | None = None
        for attempt in range(3):
            if not path.exists():
                break
            try:
                shutil.rmtree(path, onerror=self._remove_readonly)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.1 * (attempt + 1))

        if path.exists() and last_error is not None:
            raise last_error

        job_root = path.parent
        try:
            job_root.rmdir()
        except OSError:
            pass


sandbox_manager = SandboxManager()
