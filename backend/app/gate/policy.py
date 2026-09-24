"""Gate policy engine.

The agents gather evidence. The policy decides. Keeping the two apart means a
language model can never be the thing that unblocks a release: it can only
produce facts that a deterministic rule set then evaluates.

A policy is plain JSON, stored per repository, overridable per request, and
loadable from ``.autoheal/policy.yml`` in the target repository.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

# Verdicts
PASS = "PASS"
BLOCK = "BLOCK"
HOLD = "HOLD"

DEFAULT_POLICY: dict[str, Any] = {
    # Identification
    "name": "default",
    "description": "Balanced policy: auto-heal safe failures, hold everything else.",
    # Core behaviour
    "mode": "enforce",  # enforce | advisory
    "fail_closed": True,
    "allow_auto_remediation": True,
    "require_validated_fix": True,
    "require_human_approval": True,
    # Risk
    "max_risk_score": 40,
    "min_root_cause_confidence": 0.6,
    # Change surface
    "protected_paths": [
        ".github/**",
        "**/Dockerfile",
        "**/*.tf",
        "infra/**",
        "deploy/**",
        "**/secrets*",
        "**/*.pem",
        "**/*.key",
    ],
    "forbid_test_modification": True,
    "max_changed_files": 3,
    "max_changed_lines": 60,
    # Branches
    "protected_branches": ["main", "master", "release/*", "prod"],
    "protected_branch_requires_approval": True,
    # Failure classes the gate is allowed to auto-heal
    "auto_healable_categories": [
        "assertion_failure",
        "import_error",
        "syntax_error",
        "dependency_version",
        "lint_error",
        "flaky_timeout",
    ],
    "never_auto_heal_categories": [
        "security_finding",
        "secret_detected",
        "license_violation",
        "unknown",
    ],
    # Integration side effects
    "publish_check_run": True,
    "open_pull_request": False,
    "comment_on_pull_request": True,
}


@dataclass
class RuleOutcome:
    """One rule's contribution to the verdict."""

    rule: str
    passed: bool
    verdict: str | None
    message: str
    weight: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "passed": self.passed,
            "verdict": self.verdict,
            "message": self.message,
            "weight": self.weight,
        }


@dataclass
class PolicyDecision:
    verdict: str
    reason: str
    risk_score: int
    hitl_required: bool
    rules: list[RuleOutcome] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "risk_score": self.risk_score,
            "hitl_required": self.hitl_required,
            "rules": [r.as_dict() for r in self.rules],
            "policy_name": self.policy.get("name", "default"),
            "mode": self.policy.get("mode", "enforce"),
        }


def merge_policy(*layers: dict[str, Any] | None) -> dict[str, Any]:
    """Shallow-merge policy layers, last one wins. ``None`` layers are skipped."""
    merged = dict(DEFAULT_POLICY)
    for layer in layers:
        if not layer:
            continue
        for key, value in layer.items():
            if key in DEFAULT_POLICY or key.startswith("x_"):
                merged[key] = value
    return merged


def load_policy_yaml(text: str) -> dict[str, Any]:
    """Parse a ``.autoheal/policy.yml`` document into a policy dict."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - pyyaml is a listed dependency
        return {}
    try:
        data = yaml.safe_load(text) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data.get("policy", data)


# ---------------------------------------------------------------- utilities


def normalize_path(path: str) -> str:
    """Normalise a diff path without eating a leading dot.

    ``lstrip("./")`` would turn ``.github/workflows/ci.yml`` into
    ``github/workflows/ci.yml`` and silently defeat the protected-path rule,
    so only an explicit ``./`` prefix is removed.
    """
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _matches_any(path: str, patterns: list[str]) -> bool:
    normalized = normalize_path(path)
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern):
            return True
        # Allow "infra/**" to also match "infra/main.tf" style paths.
        if pattern.endswith("/**") and normalized.startswith(pattern[:-3] + "/"):
            return True
    return False


def is_test_path(path: str) -> bool:
    normalized = normalize_path(path).lower()
    filename = normalized.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{normalized}"
        or "/test/" in f"/{normalized}"
        or filename.startswith("test_")
        or filename.endswith("_test.py")
        or filename.endswith(".test.js")
        or filename.endswith(".spec.ts")
    )


def changed_files_in_patch(patch: str) -> list[str]:
    files: list[str] = []
    for line in (patch or "").splitlines():
        if not line.startswith("+++ ") and not line.startswith("--- "):
            continue
        raw = line[4:].strip().split("\t", 1)[0]
        if raw in ("/dev/null", "dev/null"):
            continue
        raw = raw.replace("\\", "/")
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        raw = normalize_path(raw)
        if raw and raw not in files:
            files.append(raw)
    return files


def changed_line_count(patch: str) -> int:
    count = 0
    for line in (patch or "").splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            count += 1
    return count


def is_valid_unified_diff(patch: Any) -> bool:
    if not isinstance(patch, str):
        return False
    text = patch.strip()
    if not text:
        return False
    has_headers = "\n--- " in f"\n{text}" and "\n+++ " in text
    return has_headers and "@@" in text


SECRET_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{12,}"),
]


def patch_contains_secret(patch: str) -> bool:
    added = "\n".join(
        line for line in (patch or "").splitlines() if line.startswith("+")
    )
    return any(pattern.search(added) for pattern in SECRET_PATTERNS)


# ------------------------------------------------------------- risk scoring


def compute_risk_score(evidence: dict[str, Any], policy: dict[str, Any]) -> int:
    """Score 0-100. Higher means more dangerous to let through automatically."""
    score = 0
    patch = evidence.get("patch") or ""
    files = changed_files_in_patch(patch)
    lines = changed_line_count(patch)

    # Size of the change
    score += min(20, len(files) * 5)
    score += min(20, lines // 5)

    # Sensitive surface
    if any(_matches_any(f, policy.get("protected_paths", [])) for f in files):
        score += 30
    if any(is_test_path(f) for f in files):
        score += 25
    if patch_contains_secret(patch):
        score += 40

    # Confidence in the diagnosis
    confidence = float(evidence.get("confidence") or 0.0)
    score += int((1.0 - min(max(confidence, 0.0), 1.0)) * 25)

    # Failure class
    category = evidence.get("category", "unknown")
    if category in policy.get("never_auto_heal_categories", []):
        score += 35
    elif category not in policy.get("auto_healable_categories", []):
        score += 15

    # Validation actually run and green
    if evidence.get("tests_passed"):
        score -= 25
    elif evidence.get("patch_applied"):
        score -= 5

    # Branch sensitivity
    branch = evidence.get("branch") or ""
    if _matches_any(branch, policy.get("protected_branches", [])):
        score += 10

    return max(0, min(100, score))


# --------------------------------------------------------------- evaluation


def evaluate(evidence: dict[str, Any], policy: dict[str, Any]) -> PolicyDecision:
    """Turn agent evidence into a verdict using only deterministic rules.

    ``evidence`` keys:
        ci_passed, category, confidence, patch, patch_applied, tests_passed,
        validation_ran, branch, error
    """
    rules: list[RuleOutcome] = []
    risk = compute_risk_score(evidence, policy)
    patch = evidence.get("patch") or ""
    files = changed_files_in_patch(patch)

    # --- Rule 0: the gate itself failed -----------------------------------
    if evidence.get("error"):
        verdict = BLOCK if policy.get("fail_closed", True) else PASS
        rules.append(
            RuleOutcome(
                "gate.availability",
                passed=False,
                verdict=verdict,
                message=f"The gate could not complete its analysis: {evidence['error']}",
            )
        )
        return PolicyDecision(
            verdict=verdict,
            reason=(
                "The gate failed and fail_closed is on, so the pipeline is blocked."
                if verdict == BLOCK
                else "The gate failed but fail_closed is off, so the pipeline continues."
            ),
            risk_score=risk,
            hitl_required=verdict != PASS,
            rules=rules,
            policy=policy,
        )

    # --- Rule 1: CI was already green -------------------------------------
    if evidence.get("ci_passed"):
        rules.append(
            RuleOutcome(
                "ci.result",
                passed=True,
                verdict=PASS,
                message="The upstream CI run succeeded. No remediation was needed.",
            )
        )
        return PolicyDecision(
            verdict=PASS,
            reason="CI passed. The gate allows the pipeline to continue.",
            risk_score=risk,
            hitl_required=False,
            rules=rules,
            policy=policy,
        )

    rules.append(
        RuleOutcome(
            "ci.result",
            passed=False,
            verdict=None,
            message="The upstream CI run failed. Evaluating remediation.",
        )
    )

    blocking: list[RuleOutcome] = []
    holding: list[RuleOutcome] = []

    # --- Rule 2: remediation permitted at all -----------------------------
    if not policy.get("allow_auto_remediation", True):
        outcome = RuleOutcome(
            "remediation.enabled",
            passed=False,
            verdict=BLOCK,
            message="Auto-remediation is disabled for this repository.",
        )
        rules.append(outcome)
        blocking.append(outcome)

    # --- Rule 3: failure category -----------------------------------------
    category = evidence.get("category", "unknown")
    if category in policy.get("never_auto_heal_categories", []):
        outcome = RuleOutcome(
            "failure.category",
            passed=False,
            verdict=BLOCK,
            message=(
                f"Failures classified as '{category}' are never auto-healed. "
                "A human must resolve this."
            ),
            weight=35,
        )
        rules.append(outcome)
        blocking.append(outcome)
    else:
        rules.append(
            RuleOutcome(
                "failure.category",
                passed=True,
                verdict=None,
                message=f"Failure classified as '{category}'.",
            )
        )

    # --- Rule 4: a fix exists ---------------------------------------------
    if not patch.strip():
        outcome = RuleOutcome(
            "fix.proposed",
            passed=False,
            verdict=BLOCK,
            message="No candidate fix was produced for this failure.",
        )
        rules.append(outcome)
        blocking.append(outcome)
    else:
        rules.append(
            RuleOutcome(
                "fix.proposed",
                passed=True,
                verdict=None,
                message=f"A candidate fix touching {len(files)} file(s) was produced.",
            )
        )

        # --- Rule 5: shape of the diff ------------------------------------
        if not is_valid_unified_diff(patch):
            outcome = RuleOutcome(
                "fix.format",
                passed=False,
                verdict=BLOCK,
                message="The candidate fix is not a well-formed unified diff.",
            )
            rules.append(outcome)
            blocking.append(outcome)

        # --- Rule 6: tests must not be edited into passing -----------------
        test_files = [f for f in files if is_test_path(f)]
        if policy.get("forbid_test_modification", True) and test_files:
            outcome = RuleOutcome(
                "fix.no_test_edits",
                passed=False,
                verdict=BLOCK,
                message=(
                    "The fix modifies test files "
                    f"({', '.join(test_files)}), which would hide the failure."
                ),
                weight=25,
            )
            rules.append(outcome)
            blocking.append(outcome)

        # --- Rule 7: protected paths --------------------------------------
        protected_hits = [
            f for f in files if _matches_any(f, policy.get("protected_paths", []))
        ]
        if protected_hits:
            outcome = RuleOutcome(
                "fix.protected_paths",
                passed=False,
                verdict=BLOCK,
                message=(
                    "The fix touches protected paths "
                    f"({', '.join(protected_hits)}). Automated changes are not "
                    "allowed there."
                ),
                weight=30,
            )
            rules.append(outcome)
            blocking.append(outcome)

        # --- Rule 8: blast radius -----------------------------------------
        max_files = int(policy.get("max_changed_files", 3))
        if len(files) > max_files:
            outcome = RuleOutcome(
                "fix.blast_radius",
                passed=False,
                verdict=HOLD,
                message=(
                    f"The fix changes {len(files)} files, above the limit of "
                    f"{max_files}."
                ),
            )
            rules.append(outcome)
            holding.append(outcome)

        max_lines = int(policy.get("max_changed_lines", 60))
        lines = changed_line_count(patch)
        if lines > max_lines:
            outcome = RuleOutcome(
                "fix.size",
                passed=False,
                verdict=HOLD,
                message=f"The fix changes {lines} lines, above the limit of {max_lines}.",
            )
            rules.append(outcome)
            holding.append(outcome)

        # --- Rule 9: no secrets introduced --------------------------------
        if patch_contains_secret(patch):
            outcome = RuleOutcome(
                "fix.no_secrets",
                passed=False,
                verdict=BLOCK,
                message="The fix appears to add a credential or private key.",
                weight=40,
            )
            rules.append(outcome)
            blocking.append(outcome)

    # --- Rule 10: diagnosis confidence ------------------------------------
    confidence = float(evidence.get("confidence") or 0.0)
    min_confidence = float(policy.get("min_root_cause_confidence", 0.6))
    if confidence < min_confidence:
        outcome = RuleOutcome(
            "rca.confidence",
            passed=False,
            verdict=HOLD,
            message=(
                f"Root-cause confidence {confidence:.2f} is below the required "
                f"{min_confidence:.2f}."
            ),
        )
        rules.append(outcome)
        holding.append(outcome)
    else:
        rules.append(
            RuleOutcome(
                "rca.confidence",
                passed=True,
                verdict=None,
                message=f"Root-cause confidence {confidence:.2f}.",
            )
        )

    # --- Rule 11: the fix must be proven ----------------------------------
    if policy.get("require_validated_fix", True):
        if not evidence.get("validation_ran"):
            outcome = RuleOutcome(
                "fix.validated",
                passed=False,
                verdict=HOLD,
                message=(
                    "The fix was never executed against the test suite, so it "
                    "is unproven."
                ),
            )
            rules.append(outcome)
            holding.append(outcome)
        elif not evidence.get("tests_passed"):
            outcome = RuleOutcome(
                "fix.validated",
                passed=False,
                verdict=BLOCK,
                message="The fix was applied but the test suite still fails.",
            )
            rules.append(outcome)
            blocking.append(outcome)
        else:
            rules.append(
                RuleOutcome(
                    "fix.validated",
                    passed=True,
                    verdict=None,
                    message="The fix was applied in an isolated copy and the tests passed.",
                )
            )

    # --- Rule 12: risk budget ---------------------------------------------
    max_risk = int(policy.get("max_risk_score", 40))
    if risk > max_risk:
        outcome = RuleOutcome(
            "risk.budget",
            passed=False,
            verdict=HOLD,
            message=f"Risk score {risk} exceeds the budget of {max_risk}.",
        )
        rules.append(outcome)
        holding.append(outcome)
    else:
        rules.append(
            RuleOutcome(
                "risk.budget",
                passed=True,
                verdict=None,
                message=f"Risk score {risk} is within the budget of {max_risk}.",
            )
        )

    # --- Rule 13: human approval ------------------------------------------
    branch = evidence.get("branch") or ""
    protected_branch = _matches_any(branch, policy.get("protected_branches", []))

    needs_human = bool(policy.get("require_human_approval", True)) or (
        protected_branch and policy.get("protected_branch_requires_approval", True)
    )
    if needs_human:
        outcome = RuleOutcome(
            "approval.required",
            passed=False,
            verdict=HOLD,
            message=(
                f"Branch '{branch}' is protected, so a human must approve this fix."
                if protected_branch
                else "Policy requires a human to approve every automated fix."
            ),
        )
        rules.append(outcome)
        holding.append(outcome)

    # --- Final verdict -----------------------------------------------------
    if blocking:
        verdict = BLOCK
        reason = blocking[0].message
    elif holding:
        verdict = HOLD
        reason = holding[0].message
    else:
        verdict = PASS
        reason = (
            "The failure was diagnosed, the fix was validated against the test "
            "suite, and every policy rule passed."
        )

    # Advisory mode reports the verdict but never blocks the pipeline.
    if policy.get("mode") == "advisory" and verdict != PASS:
        rules.append(
            RuleOutcome(
                "policy.mode",
                passed=True,
                verdict=PASS,
                message=f"Advisory mode: reporting '{verdict}' without blocking.",
            )
        )
        reason = f"Advisory mode. Would have returned {verdict}: {reason}"
        verdict = PASS

    return PolicyDecision(
        verdict=verdict,
        reason=reason,
        risk_score=risk,
        hitl_required=verdict == HOLD,
        rules=rules,
        policy=policy,
    )
