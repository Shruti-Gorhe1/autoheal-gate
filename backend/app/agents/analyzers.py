"""Deterministic CI log analysis.

Everything here is pure text processing: no model, no network, no cost. It is
the reason the gate produces a defensible verdict even with no LLM available,
and it is also the ground truth the LLM engine is checked against.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from app.gate.policy import is_test_path

# --------------------------------------------------------------- patterns

_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("secret_detected", re.compile(r"(?i)(secret detected|gitleaks|trufflehog found)"), 0.95),
    ("security_finding", re.compile(r"(?i)(CVE-\d{4}-\d+|high severity vulnerability|snyk test failed)"), 0.9),
    ("license_violation", re.compile(r"(?i)(license (violation|not allowed)|disallowed licen[cs]e)"), 0.9),
    ("syntax_error", re.compile(r"(SyntaxError|IndentationError|Unexpected token|Parse error)"), 0.95),
    ("import_error", re.compile(r"(ModuleNotFoundError|ImportError|Cannot find module|cannot import name)"), 0.93),
    ("dependency_version", re.compile(r"(?i)(no matching distribution|version conflict|ERESOLVE|incompatible|could not find a version)"), 0.85),
    ("type_error", re.compile(r"\bTypeError\b"), 0.85),
    ("lint_error", re.compile(r"(?i)(ruff|flake8|eslint|black --check|would reformat|lint failed)"), 0.8),
    ("flaky_timeout", re.compile(r"(?i)(timed? ?out|ETIMEDOUT|connection reset|ECONNREFUSED|rate limit exceeded)"), 0.75),
    ("build_error", re.compile(r"(?i)(build failed|compilation (error|failed)|linker error)"), 0.8),
    ("infrastructure", re.compile(r"(?i)(runner .* lost communication|no space left on device|out of memory|OOMKilled)"), 0.85),
    ("assertion_failure", re.compile(r"(AssertionError|^E\s+assert |expect\(.*\)\.to|FAILED .*::)", re.MULTILINE), 0.9),
]

_FAILED_TEST = re.compile(r"FAILED\s+([^\s]+::[^\s\-]+)")
_EXIT_CODE = re.compile(r"EXIT CODE:\s*(-?\d+)")
_PYTEST_SUMMARY = re.compile(r"(\d+) failed[,.]?(?:\s*(\d+) passed)?")
_ASSERT_CMP = re.compile(r"^E\s+assert\s+(.+?)\s*==\s*(.+?)\s*$", re.MULTILINE)
_MISSING_MODULE = re.compile(r"ModuleNotFoundError: No module named ['\"]([\w.\-]+)['\"]")
# pytest prints "+  where -1 = add(2, 3)", which names the function at fault.
_WHERE_CALL = re.compile(r"where\s+\S+\s*=\s*([\w.]+)\s*\(")
_DIRECT_CALL = re.compile(r"^E\s+assert\s+([\w.]+)\s*\(", re.MULTILINE)
_FILE_LINE = re.compile(r"([\w./\\\-]+\.(?:py|js|ts|tsx|jsx|go|java|rb)):(\d+)")


def classify(logs: str) -> dict[str, Any]:
    """Assign a failure category with a confidence and the matched signal."""
    text = logs or ""
    matches: list[dict[str, Any]] = []

    for category, pattern, confidence in _PATTERNS:
        found = pattern.search(text)
        if found:
            matches.append(
                {
                    "category": category,
                    "confidence": confidence,
                    "signal": found.group(0).strip()[:200],
                }
            )

    if not matches:
        return {
            "category": "unknown",
            "confidence": 0.2,
            "signal": None,
            "all_matches": [],
            "failed_tests": [],
            "exit_code": _exit_code(text),
        }

    # Security-class signals win over ordinary test noise regardless of order.
    priority = {"secret_detected": 3, "security_finding": 3, "license_violation": 3}
    matches.sort(
        key=lambda m: (priority.get(m["category"], 0), m["confidence"]), reverse=True
    )
    best = matches[0]

    return {
        "category": best["category"],
        "confidence": best["confidence"],
        "signal": best["signal"],
        "all_matches": [m["category"] for m in matches],
        "failed_tests": failed_tests(text),
        "exit_code": _exit_code(text),
    }


def _exit_code(logs: str) -> int | None:
    match = _EXIT_CODE.search(logs or "")
    return int(match.group(1)) if match else None


def failed_tests(logs: str) -> list[str]:
    seen: list[str] = []
    for name in _FAILED_TEST.findall(logs or ""):
        cleaned = name.replace("\\", "/")
        if cleaned not in seen:
            seen.append(cleaned)
    return seen


def referenced_files(logs: str, limit: int = 12) -> list[str]:
    """Source files the traceback points at, test files last."""
    found: list[str] = []
    for path, _line in _FILE_LINE.findall(logs or ""):
        normalized = path.replace("\\", "/").lstrip("./")
        if normalized.startswith(("/usr/", "/opt/")) or "site-packages" in normalized:
            continue
        if normalized not in found:
            found.append(normalized)

    found.sort(key=is_test_path)
    return found[:limit]


def failing_symbol(logs: str) -> str | None:
    """The function the failing assertion actually called, if pytest named it."""
    match = _WHERE_CALL.search(logs or "") or _DIRECT_CALL.search(logs or "")
    if not match:
        return None
    return match.group(1).split(".")[-1]


def locate_definition(repo_path: str | None, symbol: str) -> str | None:
    """Find the non-test source file that defines ``symbol``.

    The traceback usually names only the test file, so the implementation has
    to be located before a repair can target it.
    """
    if not repo_path or not symbol:
        return None

    root = Path(repo_path)
    if not root.is_dir():
        return None

    patterns = [
        re.compile(rf"^\s*def\s+{re.escape(symbol)}\s*\("),
        re.compile(rf"^\s*(?:async\s+)?function\s+{re.escape(symbol)}\s*\("),
        re.compile(rf"^\s*(?:export\s+)?const\s+{re.escape(symbol)}\s*="),
    ]
    suffixes = {".py", ".js", ".ts", ".tsx", ".jsx"}
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}

    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file() or candidate.suffix not in suffixes:
            continue
        if any(part in skip for part in candidate.parts):
            continue

        relative = candidate.relative_to(root).as_posix()
        if is_test_path(relative):
            continue

        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if any(pattern.search(text) for pattern in patterns):
            return relative

    return None


def extract_evidence(logs: str, limit: int = 8) -> list[str]:
    """Pull the lines a reviewer would actually read."""
    lines: list[str] = []
    for raw in (logs or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if (
            line.startswith("E ")
            or line.startswith("E\t")
            or line.lstrip().startswith("E   ")
            or "FAILED" in line
            or "Error:" in line
            or "error:" in line
            or "AssertionError" in line
            or "Traceback (most recent call last)" in line
        ):
            cleaned = line.strip()[:300]
            if cleaned not in lines:
                lines.append(cleaned)
        if len(lines) >= limit:
            break
    return lines


def analyze_root_cause(
    logs: str,
    diff: str,
    triage: dict[str, Any],
    repo_path: str | None = None,
) -> dict[str, Any]:
    """Build a root-cause statement from the logs alone."""
    category = triage.get("category", "unknown")
    tests = triage.get("failed_tests") or failed_tests(logs)
    files = referenced_files(logs)
    evidence = extract_evidence(logs)

    implicated = [f for f in files if not is_test_path(f)]

    # The traceback usually names only the test. Resolve the function it
    # called back to the file that defines it, so the repair targets the
    # implementation rather than the test.
    symbol = failing_symbol(logs)
    definition = locate_definition(repo_path, symbol) if symbol else None
    if definition and definition not in implicated:
        implicated.insert(0, definition)

    summary: str
    confidence = float(triage.get("confidence", 0.3))

    if category == "assertion_failure":
        cmp_match = _ASSERT_CMP.search(logs or "")
        if cmp_match:
            actual, expected = cmp_match.group(1).strip(), cmp_match.group(2).strip()
            target = (
                implicated[0]
                if implicated
                else (f"{symbol}()" if symbol else "the implementation under test")
            )
            summary = (
                f"A test assertion failed: the code produced {actual} where "
                f"{expected} was expected. The defect is in {target}, not in the test."
            )
            confidence = max(confidence, 0.9)
        else:
            summary = (
                f"{len(tests) or 1} test(s) failed on an assertion. "
                "The implementation does not match the expected behaviour."
            )
    elif category == "import_error":
        missing = _MISSING_MODULE.search(logs or "")
        if missing:
            summary = (
                f"The module '{missing.group(1)}' is not installed or not importable "
                "in the CI environment. The dependency declaration and the "
                "installed set have diverged."
            )
            confidence = max(confidence, 0.9)
        else:
            summary = "An import failed, so a module path or dependency is wrong."
    elif category == "syntax_error":
        summary = "The source does not parse. A syntax error was introduced in this change."
        confidence = max(confidence, 0.95)
    elif category == "dependency_version":
        summary = (
            "Dependency resolution failed. A requested version does not exist or "
            "conflicts with another pin."
        )
    elif category == "lint_error":
        summary = "A linter or formatter rejected the change. Style rules were not met."
    elif category == "flaky_timeout":
        summary = (
            "A step timed out or a network call failed. This often reflects the "
            "environment rather than the change itself."
        )
        confidence = min(confidence, 0.6)
    elif category == "secret_detected":
        summary = "A scanner reported a credential in the change. This must be handled by a human."
        confidence = 0.95
    elif category == "security_finding":
        summary = "A security scanner reported a finding that blocks the release."
        confidence = 0.9
    elif category == "infrastructure":
        summary = (
            "The runner or environment failed rather than the code. Re-running the "
            "job is usually the correct response."
        )
    else:
        summary = (
            "The failure could not be classified from the available logs. "
            "A human should inspect the run."
        )
        confidence = min(confidence, 0.3)

    return {
        "summary": summary,
        "category": category,
        "failing_symbol": symbol,
        "confidence": round(confidence, 2),
        "evidence": evidence,
        "failed_tests": tests,
        "likely_files": implicated or files,
        "method": "deterministic-log-analysis",
    }


# ------------------------------------------------------------ fix synthesis


def _read_repo_file(repo_path: str | None, relative: str) -> str | None:
    if not repo_path:
        return None
    candidate = Path(repo_path) / relative
    if not candidate.is_file():
        # The traceback path may be repo-relative in a different way.
        matches = list(Path(repo_path).rglob(Path(relative).name))
        if len(matches) != 1:
            return None
        candidate = matches[0]
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _unified_diff(relative: str, before: str, after: str) -> str:
    """Create a git-compatible unified diff with repository-relative paths."""
    import difflib

    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)

    diff = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{relative}",
        tofile=f"b/{relative}",
        lineterm="\n",
    )

    return "".join(diff)


# Arithmetic slips that a failing equality assertion can prove.
_OPERATOR_FIXES = [
    (re.compile(r"\breturn\s+(\w+)\s*-\s*(\w+)\s*$"), "return {a} + {b}", "+"),
    (re.compile(r"\breturn\s+(\w+)\s*\+\s*(\w+)\s*$"), "return {a} - {b}", "-"),
    (re.compile(r"\breturn\s+(\w+)\s*/\s*(\w+)\s*$"), "return {a} * {b}", "*"),
    (re.compile(r"\breturn\s+(\w+)\s*\*\s*(\w+)\s*$"), "return {a} / {b}", "/"),
]


def build_unified_diff(relative: str, before: str, after: str) -> str:
    """Public entry point to :func:`_unified_diff`, for callers outside this module."""
    return _unified_diff(relative, before, after)


# Per-file and total character budgets when handing source to the LLM engine.
# Generous enough for a real module, small enough to keep the prompt cheap
# and to stop a single huge file from crowding out every other file's context.
MAX_FIX_CONTEXT_FILES = 3
MAX_FIX_CONTEXT_CHARS_PER_FILE = 6000
MAX_FIX_CONTEXT_CHARS_TOTAL = 14000


def gather_fix_context(
    repo_path: str | None,
    likely_files: list[str],
    max_files: int = MAX_FIX_CONTEXT_FILES,
    remote_reader: Callable[[str], str | None] | None = None,
) -> dict[str, str]:
    """Read the actual contents of the files the root-cause agent implicated.

    Without this, the fix agent is asked to edit code it has never seen --
    it can describe the bug but cannot respond to it with anything more
    specific than what the log already spelled out. This is what lets a
    fix like changing a hardcoded ``k=1`` back to the ``k`` parameter
    actually get proposed, rather than only a class-of-bug description.

    Test files are excluded here, not just blocked later by policy: an LLM
    given a test file as "context to fix" is an LLM invited to edit it.

    ``remote_reader``, when given, is tried for any implicated file a local
    checkout could not supply -- either because no ``repo_path`` is
    registered at all, or the file isn't present there. This is what lets
    Fix see real code for a repository the gate only knows about through
    GitHub (a webhook-driven run with no local clone), rather than only
    working when the gate happens to share a filesystem with the checkout.
    """
    if not likely_files:
        return {}

    contents: dict[str, str] = {}
    budget = MAX_FIX_CONTEXT_CHARS_TOTAL

    for relative in likely_files:
        if len(contents) >= max_files or budget <= 0:
            break
        if is_test_path(relative):
            continue

        text = _read_repo_file(repo_path, relative) if repo_path else None
        if text is None and remote_reader is not None:
            text = remote_reader(relative)
        if text is None:
            continue

        allowance = min(MAX_FIX_CONTEXT_CHARS_PER_FILE, budget)
        if len(text) > allowance:
            head = allowance // 2
            text = f"{text[:head]}\n...[truncated]...\n{text[-(allowance - head):]}"

        contents[relative] = text
        budget -= len(text)

    return contents


def apply_llm_file_edits(
    original_files: dict[str, str], proposed_files: dict[str, object]
) -> str:
    """Turn the LLM's proposed full-file contents into a verified diff.

    Only paths that were actually offered as context are honored -- a
    path the model invents is not a file this run has read or can attest
    to, so it is silently dropped rather than trusted. Files the model
    returned unchanged produce no diff for that file.
    """
    diffs: list[str] = []

    for relative, original in original_files.items():
        new_content = proposed_files.get(relative)

        if not isinstance(new_content, str) or new_content == original:
            continue

        diff = build_unified_diff(
            relative,
            original,
            new_content,
        )

        if diff:
            diff = f"diff --git a/{relative} b/{relative}\n" + diff

            if not diff.endswith("\n"):
                diff += "\n"

            diffs.append(diff)

    return "".join(diffs)


def synthesize_fix(
    ctx_logs: str,
    root_cause: dict[str, Any],
    repo_path: str | None,
) -> dict[str, Any]:
    """Produce a minimal unified diff when the evidence proves what to change.

    This deliberately refuses to guess. Returning no patch is a correct
    outcome; the policy engine then holds or blocks the release.
    """
    category = root_cause.get("category", "unknown")
    empty = {
        "patch": "",
        "proposal": "",
        "confidence": 0.0,
        "validation_plan": "",
        "method": "deterministic-synthesis",
    }

    if category != "assertion_failure":
        empty["proposal"] = (
            f"No deterministic repair exists for a '{category}' failure. "
            "A human or the LLM engine must propose the change."
        )
        return empty

    cmp_match = _ASSERT_CMP.search(ctx_logs or "")
    if not cmp_match:
        empty["proposal"] = "The assertion could not be reduced to an expected/actual pair."
        return empty

    actual_raw, expected_raw = cmp_match.group(1).strip(), cmp_match.group(2).strip()

    try:
        actual = float(actual_raw)
        expected = float(expected_raw)
    except ValueError:
        empty["proposal"] = (
            "The failing assertion is not numeric, so no safe mechanical repair "
            "can be derived."
        )
        return empty

    for relative in root_cause.get("likely_files", []):
        source = _read_repo_file(repo_path, relative)
        if source is None:
            continue


        for pattern, template, _op in _OPERATOR_FIXES:
            new_lines: list[str] = []
            changed = False
            for line in source.splitlines(keepends=True):
                stripped = line.rstrip("\r\n")
                match = pattern.search(stripped)
                if match and not changed:
                    indent = stripped[: len(stripped) - len(stripped.lstrip())]
                    replacement = template.format(a=match.group(1), b=match.group(2))
                    newline = line[len(stripped) :]
                    new_lines.append(f"{indent}{replacement}{newline}")
                    changed = True
                else:
                    new_lines.append(line)

            if not changed:
                continue

            patch = _unified_diff(relative, source, "".join(new_lines))
            if not patch:
                continue

            return {
                "patch": patch,
                "proposal": (
                    f"The failing assertion shows {actual_raw} where {expected_raw} "
                    f"was expected. The operator in {relative} is the smallest "
                    "change consistent with that evidence."
                ),
                "confidence": 0.85,
                "validation_plan": (
                    "Copy the repository to a throwaway workspace, apply the diff, "
                    "and run the configured test command. Never modify tests."
                ),
                "method": "deterministic-synthesis",
                "expected": expected,
                "actual": actual,
            }

    empty["proposal"] = (
        "The failing assertion was understood, but no single-line repair in the "
        "implicated source files could be derived from the evidence."
    )
    return empty
