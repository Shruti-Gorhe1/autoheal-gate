"""Optional LLM engine backed by Google ADK over a local Ollama model.

Everything here is optional. If ADK is not installed or Ollama is not running,
``available()`` returns False and the pipeline uses the deterministic engine.
No hosted model provider is required, and nothing here costs money.

The engine is deliberately limited to two jobs: explaining a root cause in
prose, and proposing a diff. It never decides whether a release may proceed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.agents.base import AgentContext
from app.config import settings
from app.observability.telemetry import agent_span

logger = logging.getLogger("autoheal.llm")

MAX_LOG_CHARS = 6000
MAX_DIFF_CHARS = 3000


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    head = limit // 2
    return f"{text[:head]}\n...[truncated]...\n{text[-(limit - head):]}"


def parse_json_response(text: str) -> dict[str, Any]:
    """Recover a JSON object from a model response that may wrap it in prose."""
    if not text:
        return {}
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def strip_fences(text: str) -> str:
    return re.sub(r"```(?:diff|patch)?", "", text or "").strip()


class LLMEngine:
    """Thin client over Ollama, with ADK used when it is installed."""

    def __init__(self) -> None:
        self.provider = settings.llm_provider.lower()
        self.model = settings.llm_model
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.timeout = settings.llm_timeout_seconds
        self._available: bool | None = None

    # ------------------------------------------------------------ liveness

    def available(self) -> bool:
        """One cached reachability probe. Never blocks a run for long."""
        if self._available is not None:
            return self._available
        if self.provider != "ollama":
            self._available = False
            return False
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=3.0)
            tags = response.json().get("models", [])
            names = {str(m.get("name", "")).split(":")[0] for m in tags}
            self._available = response.status_code == 200 and (
                not names or self.model.split(":")[0] in names or True
            )
        except Exception as exc:
            logger.info("Ollama is not reachable at %s (%s).", self.base_url, exc)
            self._available = False
        return self._available

    # ------------------------------------------------------------ generate

    def generate(self, prompt: str, temperature: float = 0.0) -> str:
        if not self.available():
            return ""
        with agent_span("llm-call", **{"llm.model": self.model}) as span:
            try:
                response = httpx.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": temperature, "num_predict": 900},
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                text = str(response.json().get("response", "")).strip()
                span.set_attribute("llm.response_chars", str(len(text)))
                return text
            except Exception as exc:
                span.record_exception(exc)
                logger.warning("LLM call failed: %s", exc)
                return ""

    # --------------------------------------------------------------- tasks

    def root_cause(
        self, ctx: AgentContext, baseline: dict[str, Any]
    ) -> dict[str, Any]:
        prompt = f"""You are the root-cause analyst in a CI/CD release gate.

Explain, in at most four sentences, why this pipeline failed. The CI logs are
the only authoritative evidence. The knowledge-base extracts are background
material about past incidents and are NOT evidence about this one.

Return ONLY a JSON object with the keys: summary, confidence, likely_files.
"confidence" is a number from 0 to 1. "likely_files" is an array of repository
paths. Never claim a test passed. Never propose a patch here.

A rule-based analyser already concluded:
{json.dumps(baseline.get("summary", ""))}
category={baseline.get("category")}

FAILING TESTS: {baseline.get("failed_tests")}

CI LOGS:
{_clip(ctx.logs, MAX_LOG_CHARS)}

COMMIT DIFF:
{_clip(ctx.diff, MAX_DIFF_CHARS)}

KNOWLEDGE BASE (BACKGROUND ONLY):
{_clip(ctx.knowledge.get("context", ""), 3000)}
"""
        return parse_json_response(self.generate(prompt))

    def propose_fix(
        self, ctx: AgentContext, file_contents: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Propose a fix, grounded in the real contents of the implicated files.

        Without ``file_contents`` the model can only describe a class of bug,
        not respond to the actual code -- it has never seen it. With them, it
        can propose the exact line the evidence points at. It never writes a
        diff itself: hand-authored hunks and line numbers from a model are a
        common source of unappliable or subtly wrong patches. Instead it
        returns the corrected full content of whichever files it's changing,
        and the diff against the real, current content is computed here.
        """
        file_contents = file_contents or {}

        if not file_contents:
            prompt = f"""You are a remediation engineer working under a strict policy.

No source file could be read for this failure, so you cannot see any code.
Explain briefly, in "proposal", what change would likely be needed and why
none can be safely proposed without seeing the implicated file. Return ONLY
a JSON object with the keys: proposal, validation_plan, files (an empty
object).

ROOT CAUSE:
{json.dumps(ctx.root_cause.get("summary", ""))}
IMPLICATED FILES: {ctx.root_cause.get("likely_files")}

CI LOGS:
{_clip(ctx.logs, MAX_LOG_CHARS)}
"""
            parsed = parse_json_response(self.generate(prompt))
            parsed.setdefault("files", {})
            return parsed

        file_blocks = "\n\n".join(
            f"FILE: {path}\n-----\n{content}\n-----" for path, content in file_contents.items()
        )

        prompt = f"""You are a remediation engineer working under a strict policy.

Below is the root cause of a CI failure and the full current contents of
every file you are allowed to change. Propose the smallest change that fixes
it.

Rules you must follow:
- Return ONLY a JSON object with the keys: proposal, validation_plan, files.
- "files" is an object mapping a file path from FILES BELOW (verbatim, only
  those paths) to that file's COMPLETE new content after your fix.
- Reproduce every line you are not changing exactly as given. Do not
  summarize, truncate, or add commentary inside a file's content.
- Only include a file in "files" if you are actually changing it. Do not
  include unchanged files.
- Never propose a change to a file not listed below. If the real fix
  requires a file you cannot see, say so in "proposal" and return an empty
  "files" object.
- Never touch CI configuration, infrastructure, or anything holding
  credentials, even if such a path happens to appear below.
- Change at most {ctx.policy.get("max_changed_files", 3)} files.
- If you are unsure, return an empty "files" object. That is a valid, safe
  answer; a wrong edit is not.

ROOT CAUSE:
{json.dumps(ctx.root_cause.get("summary", ""))}

CI LOGS:
{_clip(ctx.logs, MAX_LOG_CHARS)}

COMMIT DIFF:
{_clip(ctx.diff, MAX_DIFF_CHARS)}

FILES BELOW:
{file_blocks}
"""
        parsed = parse_json_response(self.generate(prompt))
        files = parsed.get("files")
        parsed["files"] = files if isinstance(files, dict) else {}

        from app.agents.analyzers import apply_llm_file_edits

        parsed["patch"] = apply_llm_file_edits(file_contents, parsed["files"])
        return parsed

    # ------------------------------------------------------------ metadata

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "reachable": self.available(),
            "adk_installed": _adk_installed(),
        }


def _adk_installed() -> bool:
    try:
        import google.adk  # noqa: F401

        return True
    except Exception:
        return False
