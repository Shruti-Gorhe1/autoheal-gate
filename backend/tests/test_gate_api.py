"""End-to-end gate API tests.

These exercise the real HTTP surface: register a repository, submit a failing
build, watch the gate diagnose and validate a fix, and confirm a human
decision changes the recorded verdict.
"""

from __future__ import annotations


def register(auth_client, full_name: str, local_path: str, policy: dict | None = None):
    response = auth_client.post(
        "/api/gate/repositories",
        json={
            "full_name": full_name,
            "local_path": local_path,
            "test_command": "python -m pytest -q",
            "policy": policy or {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_green_ci_passes_through_the_api(auth_client):
    response = auth_client.post(
        "/api/gate/check",
        json={
            "repository": "acme/calc",
            "commit_sha": "a" * 40,
            "ci_passed": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "PASS"
    assert body["allowed"] is True


def test_a_healable_failure_is_held_for_approval_by_default(
    auth_client, broken_repo, failing_log
):
    register(auth_client, "acme/calc-default", str(broken_repo))

    response = auth_client.post(
        "/api/gate/check",
        json={
            "repository": "acme/calc-default",
            "commit_sha": "b" * 40,
            "branch": "feature/add",
            "ci_passed": False,
            "failure_logs": failing_log,
            "repo_path": str(broken_repo),
        },
    )
    assert response.status_code == 409  # non-PASS verdicts set 409
    body = response.json()

    assert body["verdict"] == "HOLD"
    assert body["fix_proposed"] is True
    assert body["patch_applied"] is True
    assert body["tests_passed"] is True
    assert body["hitl_required"] is True
    assert "add" in (body["root_cause"] or "").lower() or body["patch"]


def test_a_healable_failure_passes_when_approval_is_not_required(
    auth_client, broken_repo, failing_log
):
    register(
        auth_client,
        "acme/calc-auto",
        str(broken_repo),
        policy={"require_human_approval": False},
    )

    response = auth_client.post(
        "/api/gate/check",
        json={
            "repository": "acme/calc-auto",
            "commit_sha": "c" * 40,
            "branch": "feature/add",
            "ci_passed": False,
            "failure_logs": failing_log,
            "repo_path": str(broken_repo),
        },
    )
    assert response.status_code == 200
    assert response.json()["verdict"] == "PASS"


def test_an_unfixable_failure_is_blocked(auth_client, broken_repo):
    register(auth_client, "acme/calc-unknown", str(broken_repo))

    response = auth_client.post(
        "/api/gate/check",
        json={
            "repository": "acme/calc-unknown",
            "commit_sha": "d" * 40,
            "ci_passed": False,
            "failure_logs": "the build agent vanished into the void",
            "repo_path": str(broken_repo),
        },
    )
    assert response.status_code == 409
    body = response.json()
    assert body["verdict"] == "BLOCK"
    assert body["fix_proposed"] is False
    # The silent gap this test exists to prevent: an operator staring at a
    # missing patch with no explanation of why the agent declined to guess.
    assert body["fix_notes"]


def test_a_non_assertion_failure_explains_why_no_fix_was_proposed(
    auth_client, broken_repo
):
    """E.g. a retriever/import/type-error failure: no mechanical repair
    exists, and the caller must be told that rather than left guessing."""
    register(auth_client, "acme/calc-importerror", str(broken_repo))

    response = auth_client.post(
        "/api/gate/check",
        json={
            "repository": "acme/calc-importerror",
            "commit_sha": "9" * 40,
            "ci_passed": False,
            "failure_logs": "ModuleNotFoundError: No module named 'retriever'",
            "repo_path": str(broken_repo),
        },
    )
    body = response.json()
    assert body["failure_category"] == "import_error"
    assert body["fix_proposed"] is False
    assert body["patch"] is None
    assert "no deterministic repair" in body["fix_notes"].lower()


def test_a_held_run_can_be_approved_by_a_human(auth_client, broken_repo, failing_log):
    register(auth_client, "acme/calc-approve", str(broken_repo))
    check = auth_client.post(
        "/api/gate/check",
        json={
            "repository": "acme/calc-approve",
            "commit_sha": "e" * 40,
            "ci_passed": False,
            "failure_logs": failing_log,
            "repo_path": str(broken_repo),
        },
    ).json()
    assert check["verdict"] == "HOLD"

    decision = auth_client.post(
        f"/api/gate/runs/{check['run_id']}/decision", json={"decision": "approve"}
    )
    assert decision.status_code == 200
    assert decision.json()["verdict"] == "PASS"

    detail = auth_client.get(f"/api/gate/runs/{check['run_id']}").json()
    assert detail["verdict"] == "PASS"
    assert len(detail["approvals"]) == 1
    assert detail["approvals"][0]["decision"] == "approve"


def test_a_held_run_can_be_rejected(auth_client, broken_repo, failing_log):
    register(auth_client, "acme/calc-reject", str(broken_repo))
    check = auth_client.post(
        "/api/gate/check",
        json={
            "repository": "acme/calc-reject",
            "commit_sha": "f" * 40,
            "ci_passed": False,
            "failure_logs": failing_log,
            "repo_path": str(broken_repo),
        },
    ).json()

    decision = auth_client.post(
        f"/api/gate/runs/{check['run_id']}/decision",
        json={"decision": "reject", "comment": "not touching this before the freeze"},
    )
    assert decision.json()["verdict"] == "BLOCK"


def test_a_blocked_run_cannot_be_approved(auth_client, broken_repo):
    register(auth_client, "acme/calc-hardblock", str(broken_repo))
    check = auth_client.post(
        "/api/gate/check",
        json={
            "repository": "acme/calc-hardblock",
            "commit_sha": "1" * 40,
            "ci_passed": False,
            "failure_logs": "totally unclassifiable output",
            "repo_path": str(broken_repo),
        },
    ).json()
    assert check["verdict"] == "BLOCK"

    decision = auth_client.post(
        f"/api/gate/runs/{check['run_id']}/decision", json={"decision": "approve"}
    )
    assert decision.status_code == 409


def test_runs_are_listed_and_filterable(auth_client, broken_repo, failing_log):
    register(auth_client, "acme/calc-listing", str(broken_repo))
    auth_client.post(
        "/api/gate/check",
        json={
            "repository": "acme/calc-listing",
            "commit_sha": "2" * 40,
            "ci_passed": False,
            "failure_logs": failing_log,
            "repo_path": str(broken_repo),
        },
    )
    listed = auth_client.get(
        "/api/gate/runs", params={"repository": "acme/calc-listing"}
    ).json()
    assert len(listed) == 1
    assert listed[0]["repository"] == "acme/calc-listing"


def test_gate_stats_summarize_verdicts(auth_client, broken_repo, failing_log):
    register(auth_client, "acme/calc-stats", str(broken_repo))
    auth_client.post(
        "/api/gate/check",
        json={
            "repository": "acme/calc-stats",
            "commit_sha": "3" * 40,
            "ci_passed": True,
        },
    )
    stats = auth_client.get(
        "/api/gate/stats", params={"repository": "acme/calc-stats"}
    ).json()
    assert stats["total"] == 1
    assert stats["by_verdict"]["PASS"] == 1


def test_policy_simulation_needs_no_real_pipeline(auth_client):
    result = auth_client.post(
        "/api/gate/policy/simulate",
        json={
            "evidence": {
                "ci_passed": False,
                "category": "assertion_failure",
                "confidence": 0.9,
                "patch": "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
                "patch_applied": True,
                "tests_passed": True,
                "validation_ran": True,
            },
            "overrides": {"require_human_approval": False},
        },
    )
    assert result.status_code == 200
    assert result.json()["verdict"] == "PASS"


def test_gate_check_requires_authentication(client):
    client.cookies.clear()
    response = client.post(
        "/api/gate/check",
        json={"repository": "acme/x", "commit_sha": "0" * 40, "ci_passed": True},
    )
    assert response.status_code == 401


def test_a_second_registration_of_the_same_repository_is_rejected(auth_client, broken_repo):
    register(auth_client, "acme/calc-dup", str(broken_repo))
    response = auth_client.post(
        "/api/gate/repositories",
        json={"full_name": "acme/calc-dup"},
    )
    assert response.status_code == 409


def test_repository_policy_can_be_updated(auth_client, broken_repo):
    repo = register(auth_client, "acme/calc-patchpolicy", str(broken_repo))
    response = auth_client.patch(
        f"/api/gate/repositories/{repo['id']}",
        json={"policy": {"max_risk_score": 10}},
    )
    assert response.status_code == 200
    assert response.json()["policy"]["max_risk_score"] == 10
