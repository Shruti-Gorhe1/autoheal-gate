"""Policy engine tests.

These are the most important tests in the project. The policy engine is the
only component that can allow a release, so every rule that blocks one is
pinned here.
"""

from __future__ import annotations

import pytest

from app.gate import policy as P

GOOD_PATCH = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""

TEST_EDIT_PATCH = """diff --git a/tests/test_calculator.py b/tests/test_calculator.py
--- a/tests/test_calculator.py
+++ b/tests/test_calculator.py
@@ -1,2 +1,2 @@
 def test_add():
-    assert add(2, 3) == 5
+    assert add(2, 3) == -1
"""

WORKFLOW_PATCH = """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1,2 +1,2 @@
 jobs:
-  test: {runs-on: ubuntu-latest}
+  test: {runs-on: ubuntu-latest, continue-on-error: true}
"""

SECRET_PATCH = """diff --git a/config.py b/config.py
--- a/config.py
+++ b/config.py
@@ -1,1 +1,2 @@
 import os
+API_KEY = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
"""


def evidence(**overrides):
    base = {
        "ci_passed": False,
        "category": "assertion_failure",
        "confidence": 0.9,
        "patch": GOOD_PATCH,
        "patch_applied": True,
        "tests_passed": True,
        "validation_ran": True,
        "branch": "feature/add",
        "error": None,
    }
    base.update(overrides)
    return base


def permissive(**overrides):
    """A policy that allows an automatic pass when everything is clean."""
    base = {"require_human_approval": False, "max_risk_score": 60}
    base.update(overrides)
    return P.merge_policy(base)


# --------------------------------------------------------------------- pass


def test_green_ci_passes_immediately():
    decision = P.evaluate(evidence(ci_passed=True), P.merge_policy())
    assert decision.verdict == P.PASS
    assert decision.hitl_required is False


def test_validated_fix_passes_when_approval_is_not_required():
    decision = P.evaluate(evidence(), permissive())
    assert decision.verdict == P.PASS, decision.reason


# --------------------------------------------------------------------- hold


def test_approval_requirement_holds_the_release():
    decision = P.evaluate(evidence(), P.merge_policy())
    assert decision.verdict == P.HOLD
    assert decision.hitl_required is True


def test_unvalidated_fix_is_held_not_passed():
    decision = P.evaluate(
        evidence(validation_ran=False, tests_passed=False, patch_applied=False),
        permissive(),
    )
    assert decision.verdict == P.HOLD
    assert any(r.rule == "fix.validated" for r in decision.rules if not r.passed)


def test_low_confidence_is_held():
    decision = P.evaluate(evidence(confidence=0.2), permissive())
    assert decision.verdict == P.HOLD


def test_oversized_change_is_held():
    decision = P.evaluate(evidence(), permissive(max_changed_lines=1))
    assert decision.verdict == P.HOLD


# -------------------------------------------------------------------- block


def test_a_fix_that_edits_tests_is_blocked():
    decision = P.evaluate(evidence(patch=TEST_EDIT_PATCH), permissive())
    assert decision.verdict == P.BLOCK
    assert "test" in decision.reason.lower()


def test_a_fix_touching_ci_configuration_is_blocked():
    decision = P.evaluate(evidence(patch=WORKFLOW_PATCH), permissive())
    assert decision.verdict == P.BLOCK


def test_a_fix_adding_a_credential_is_blocked():
    decision = P.evaluate(evidence(patch=SECRET_PATCH), permissive())
    assert decision.verdict == P.BLOCK


def test_failing_tests_after_the_patch_block_the_release():
    decision = P.evaluate(evidence(tests_passed=False), permissive())
    assert decision.verdict == P.BLOCK


def test_no_candidate_fix_blocks_the_release():
    decision = P.evaluate(
        evidence(patch="", patch_applied=False, tests_passed=False, validation_ran=False),
        permissive(),
    )
    assert decision.verdict == P.BLOCK


def test_security_findings_are_never_auto_healed():
    decision = P.evaluate(evidence(category="secret_detected"), permissive())
    assert decision.verdict == P.BLOCK


def test_gate_failure_blocks_when_fail_closed():
    decision = P.evaluate(evidence(error="upstream timeout"), P.merge_policy())
    assert decision.verdict == P.BLOCK


def test_gate_failure_passes_when_fail_open_is_chosen():
    decision = P.evaluate(
        evidence(error="upstream timeout"), P.merge_policy({"fail_closed": False})
    )
    assert decision.verdict == P.PASS


# ------------------------------------------------------------------ modes


def test_advisory_mode_reports_without_blocking():
    decision = P.evaluate(evidence(patch=TEST_EDIT_PATCH), permissive(mode="advisory"))
    assert decision.verdict == P.PASS
    assert "Advisory mode" in decision.reason


def test_protected_branch_forces_review():
    decision = P.evaluate(evidence(branch="main"), permissive())
    assert decision.verdict == P.HOLD


# ------------------------------------------------------------------ helpers


@pytest.mark.parametrize(
    "path,expected",
    [
        ("tests/test_calculator.py", True),
        ("src/calculator.py", False),
        ("app/foo_test.py", True),
        ("web/Button.test.js", True),
        ("lib/utils.py", False),
    ],
)
def test_test_path_detection(path, expected):
    assert P.is_test_path(path) is expected


def test_changed_files_are_extracted_from_a_diff():
    assert P.changed_files_in_patch(GOOD_PATCH) == ["calculator.py"]


def test_changed_line_count_ignores_headers():
    assert P.changed_line_count(GOOD_PATCH) == 2


def test_risk_rises_for_protected_paths():
    low = P.compute_risk_score(evidence(), P.DEFAULT_POLICY)
    high = P.compute_risk_score(evidence(patch=WORKFLOW_PATCH), P.DEFAULT_POLICY)
    assert high > low


def test_policy_layers_merge_in_order():
    merged = P.merge_policy({"max_risk_score": 10}, {"max_risk_score": 90})
    assert merged["max_risk_score"] == 90
    assert merged["forbid_test_modification"] is True


def test_unknown_policy_keys_are_ignored():
    merged = P.merge_policy({"definitely_not_a_setting": True})
    assert "definitely_not_a_setting" not in merged
