"""Agent pipeline tests.

The deterministic engine is the floor the whole product stands on, so it is
tested directly rather than only through the API.
"""

from __future__ import annotations

from app.agents import analyzers
from app.agents.base import AgentContext
from app.agents.pipeline import run_pipeline


# ------------------------------------------------------------ classification


def test_assertion_failure_is_classified(failing_log):
    result = analyzers.classify(failing_log)
    assert result["category"] == "assertion_failure"
    assert result["confidence"] >= 0.85
    assert "tests/test_calculator.py::test_add" in result["failed_tests"]


def test_missing_module_is_classified():
    result = analyzers.classify("ModuleNotFoundError: No module named 'requests'")
    assert result["category"] == "import_error"


def test_syntax_error_is_classified():
    result = analyzers.classify('File "app.py", line 3\n    SyntaxError: invalid syntax')
    assert result["category"] == "syntax_error"


def test_a_detected_secret_outranks_other_signals():
    logs = "AssertionError: boom\ngitleaks: secret detected in config.py"
    assert analyzers.classify(logs)["category"] == "secret_detected"


def test_unrecognised_output_is_unknown_with_low_confidence():
    result = analyzers.classify("everything is fine here")
    assert result["category"] == "unknown"
    assert result["confidence"] <= 0.3


def test_exit_code_is_read_from_the_log(failing_log):
    assert analyzers.classify(failing_log)["exit_code"] == 1


# --------------------------------------------------------------- root cause


def test_root_cause_names_the_expected_and_actual_values(failing_log):
    triage = analyzers.classify(failing_log)
    rca = analyzers.analyze_root_cause(failing_log, "", triage)
    assert "-1" in rca["summary"] and "5" in rca["summary"]
    assert rca["confidence"] >= 0.85
    assert rca["evidence"]


def test_root_cause_blames_implementation_not_tests(failing_log, broken_repo):
    """The traceback names the test; the analyser must find the real culprit."""
    triage = analyzers.classify(failing_log)
    rca = analyzers.analyze_root_cause(failing_log, "", triage, str(broken_repo))
    from app.gate.policy import is_test_path

    assert rca["failing_symbol"] == "add"
    assert rca["likely_files"][0] == "calculator.py"
    assert not is_test_path(rca["likely_files"][0])


def test_unknown_failures_get_low_confidence():
    triage = analyzers.classify("some unparseable output")
    rca = analyzers.analyze_root_cause("some unparseable output", "", triage)
    assert rca["confidence"] <= 0.3


# ------------------------------------------------------------------- fixes


def test_a_minimal_fix_is_synthesized(failing_log, broken_repo):
    triage = analyzers.classify(failing_log)
    rca = analyzers.analyze_root_cause(failing_log, "", triage, str(broken_repo))
    fix = analyzers.synthesize_fix(failing_log, rca, str(broken_repo))

    assert fix["patch"], fix["proposal"]
    assert "return a + b" in fix["patch"]
    from app.gate.policy import changed_files_in_patch

    assert changed_files_in_patch(fix["patch"]) == ["calculator.py"]


def test_no_fix_is_invented_for_an_unclassified_failure(broken_repo):
    rca = {"category": "unknown", "likely_files": []}
    fix = analyzers.synthesize_fix("mystery failure", rca, str(broken_repo))
    assert fix["patch"] == ""
    assert fix["proposal"]


def test_no_fix_is_invented_without_a_checkout(failing_log):
    triage = analyzers.classify(failing_log)
    rca = analyzers.analyze_root_cause(failing_log, "", triage, None)
    assert analyzers.synthesize_fix(failing_log, rca, None)["patch"] == ""


# ---------------------------------------------------------------- pipeline


def test_the_pipeline_heals_a_real_failing_repository(failing_log, broken_repo):
    """End to end: diagnose, patch a copy, and prove the tests go green."""
    ctx = AgentContext(
        run_id="test-run",
        repository="acme/calc",
        commit_sha="deadbeefcafe",
        branch="feature/add",
        ci_passed=False,
        logs=failing_log,
        repo_path=str(broken_repo),
        test_command="python -m pytest -q",
    )
    ctx = run_pipeline(ctx)

    assert ctx.triage["category"] == "assertion_failure"
    assert ctx.fix["patch"]
    assert ctx.validation["patch_applied"] is True
    assert ctx.validation["tests_passed"] is True, ctx.validation
    assert [entry["agent"] for entry in ctx.timeline] == [
        "triage",
        "retrieval",
        "root_cause",
        "fix",
        "validation",
    ]


def test_the_source_repository_is_never_modified(failing_log, broken_repo):
    before = (broken_repo / "calculator.py").read_text()
    ctx = AgentContext(
        run_id="test-isolation",
        repository="acme/calc",
        commit_sha="abc1234",
        ci_passed=False,
        logs=failing_log,
        repo_path=str(broken_repo),
    )
    run_pipeline(ctx)
    assert (broken_repo / "calculator.py").read_text() == before


def test_a_green_pipeline_short_circuits():
    ctx = AgentContext(
        run_id="test-green",
        repository="acme/calc",
        commit_sha="abc1234",
        ci_passed=True,
    )
    ctx = run_pipeline(ctx)

    assert ctx.triage["category"] == "none"
    assert ctx.fix["patch"] == ""
    assert ctx.validation["attempted"] is False
    assert ctx.evidence()["ci_passed"] is True


def test_validation_reports_honestly_when_it_cannot_run(failing_log):
    ctx = AgentContext(
        run_id="test-nocheckout",
        repository="acme/calc",
        commit_sha="abc1234",
        ci_passed=False,
        logs=failing_log,
        repo_path=None,
    )
    ctx = run_pipeline(ctx)
    assert ctx.validation["tests_passed"] is False
    assert ctx.validation["attempted"] is False
