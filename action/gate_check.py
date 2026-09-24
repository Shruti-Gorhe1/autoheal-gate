#!/usr/bin/env python3
"""Called by action.yml. Talks to the AutoHeal Gate API and sets step outputs.

Kept dependency-free (stdlib only) so the action needs no pip install step,
which keeps every consuming workflow fast and avoids a supply-chain surface.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    # Multi-line safe, delimiter form, as GitHub Actions requires.
    delimiter = "AUTOHEAL_EOF"
    line = f"{name}<<{delimiter}\n{value}\n{delimiter}\n"
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    else:  # pragma: no cover - local debugging only
        print(f"::set-output name={name}::{value}")


def write_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")


def main() -> int:
    gate_url = env("GATE_URL").rstrip("/")
    api_key = env("GATE_API_KEY")
    if not gate_url or not api_key:
        print("::error::gate-url and api-key are required inputs.", file=sys.stderr)
        return 2

    ci_passed = env("CI_PASSED").strip().lower() == "true"
    failure_log_path = env("FAILURE_LOG_PATH")
    failure_logs = ""
    if failure_log_path and os.path.isfile(failure_log_path):
        with open(failure_log_path, "r", encoding="utf-8", errors="replace") as handle:
            failure_logs = handle.read()

    try:
        overrides = json.loads(env("POLICY_OVERRIDES") or "{}")
    except json.JSONDecodeError:
        print("::warning::policy-overrides was not valid JSON; ignoring it.")
        overrides = {}

    pr_number_raw = env("PR_NUMBER")
    payload = {
        "repository": env("GITHUB_REPOSITORY"),
        "commit_sha": env("GITHUB_SHA"),
        "branch": env("GITHUB_REF_NAME"),
        "workflow_run_id": int(env("WORKFLOW_RUN_ID")) if env("WORKFLOW_RUN_ID") else None,
        "pull_request_number": int(pr_number_raw) if pr_number_raw else None,
        "ci_passed": ci_passed,
        "failure_logs": failure_logs,
        "repo_path": env("REPO_PATH"),
        "test_command": env("TEST_COMMAND") or None,
        "policy_overrides": overrides,
        "metadata": {"event": env("GITHUB_EVENT_NAME"), "source": "github-action"},
        "wait": env("WAIT", "true").strip().lower() == "true",
    }

    request = urllib.request.Request(
        f"{gate_url}/api/gate/check",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        # 409 is the gate's normal way of saying HOLD or BLOCK; parse it.
        if exc.code == 409:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                print(f"::error::AutoHeal Gate returned HTTP 409 with an unreadable body: {raw}")
                return 1
        else:
            print(f"::error::AutoHeal Gate call failed (HTTP {exc.code}): {raw}")
            return 1
    except urllib.error.URLError as exc:
        print(f"::error::Could not reach AutoHeal Gate at {gate_url}: {exc}")
        return 1

    verdict = body.get("verdict", "BLOCK")
    reason = body.get("reason", "")
    run_id = body.get("run_id", "")
    dashboard_url = body.get("dashboard_url", "")
    root_cause = body.get("root_cause") or ""

    write_output("verdict", verdict)
    write_output("run-id", run_id)
    write_output("reason", reason)
    write_output("dashboard-url", dashboard_url)
    write_output("root-cause", root_cause)

    icon = {"PASS": "✅", "BLOCK": "🛑", "HOLD": "⏸️"}.get(verdict, "❔")
    summary = [f"### {icon} AutoHeal Gate: {verdict}", "", reason]
    if body.get("fix_proposed"):
        state = "validated" if body.get("tests_passed") else "proposed but unproven"
        summary += ["", f"A candidate fix was {state}."]
    if dashboard_url:
        summary += ["", f"[Open the full run]({dashboard_url})"]
    write_summary("\n".join(summary))

    print(f"AutoHeal Gate verdict: {verdict}")
    print(reason)
    if dashboard_url:
        print(f"Details: {dashboard_url}")

    fail_on_hold = env("FAIL_ON_HOLD", "true").strip().lower() == "true"

    if verdict == "BLOCK":
        print("::error::AutoHeal Gate BLOCKED this release.", file=sys.stderr)
        return 1
    if verdict == "HOLD" and fail_on_hold:
        print(
            "::error::AutoHeal Gate is HOLDing this release for human approval. "
            f"Approve or reject it at {dashboard_url or '(no dashboard URL returned)'}.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
