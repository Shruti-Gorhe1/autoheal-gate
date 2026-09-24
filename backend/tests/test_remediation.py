"""Tests for isolated remediation: the setup-command step, and validating a
fix against a GitHub snapshot rather than a local checkout."""

from __future__ import annotations

from app.services import remediation

GOOD_PATCH = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def make_broken_repo(tmp_path):
    repo = tmp_path / "calc"
    (repo / "tests").mkdir(parents=True)
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (repo / "tests" / "test_calculator.py").write_text(
        "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    return repo


# ------------------------------------------------------------- setup_command


def test_setup_command_runs_before_the_test_command(tmp_path):
    """The setup step can produce something the tests depend on -- proof
    that it actually runs, in the workspace, before tests are collected."""
    repo = make_broken_repo(tmp_path)
    # The test suite requires a file that only the setup step creates.
    (repo / "tests" / "test_calculator.py").write_text(
        "import pathlib\n"
        "from calculator import add\n\n\n"
        "def test_setup_ran():\n"
        "    assert pathlib.Path('setup_marker.txt').exists()\n\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    result = remediation.validate_patch(
        repo_path=str(repo),
        patch=GOOD_PATCH,
        test_command="python -m pytest -q",
        setup_command="echo marker > setup_marker.txt",
    )
    assert result["patch_applied"] is True
    assert result["tests_passed"] is True, result


def test_a_failing_setup_command_stops_before_tests_run(tmp_path):
    repo = make_broken_repo(tmp_path)

    result = remediation.validate_patch(
        repo_path=str(repo),
        patch=GOOD_PATCH,
        test_command="python -m pytest -q",
        setup_command="exit 1",
    )
    assert result["patch_applied"] is True
    assert result["tests_passed"] is False
    assert "setup command" in result["reason"].lower()
    assert result["setup_exit_code"] == 1
    # The test command itself never ran -- only the setup step's output
    # should appear.
    assert "command" not in result or result.get("command") != "python -m pytest -q"


def test_no_setup_command_behaves_exactly_as_before(tmp_path):
    repo = make_broken_repo(tmp_path)
    result = remediation.validate_patch(
        repo_path=str(repo), patch=GOOD_PATCH, test_command="python -m pytest -q"
    )
    assert result["tests_passed"] is True


# ------------------------------------------------------- snapshot validation


def test_no_token_is_reported_honestly_without_attempting_anything():
    result = remediation.validate_patch_from_snapshot(
        github_token=None,
        repository="acme/calc",
        commit_sha="a" * 40,
        patch=GOOD_PATCH,
    )
    assert result["attempted"] is False
    assert "no github token" in result["reason"].lower() or "no local checkout" in result["reason"].lower()


def test_a_failed_download_is_reported_honestly(monkeypatch):
    monkeypatch.setattr(
        "app.services.github_client.download_repository_snapshot",
        lambda *a, **k: None,
    )
    result = remediation.validate_patch_from_snapshot(
        github_token="gho_test",
        repository="acme/calc",
        commit_sha="a" * 40,
        patch=GOOD_PATCH,
    )
    assert result["attempted"] is False
    assert "could not download" in result["reason"].lower()


def test_a_successful_download_is_validated_exactly_like_a_local_checkout(
    tmp_path, monkeypatch
):
    """This is the complete loop: no local_path anywhere, only a token --
    the snapshot is 'downloaded' (faked here), patched, and tested for real."""
    repo = make_broken_repo(tmp_path)

    def fake_download(token, repository, ref, dest_dir, timeout=60.0):
        assert token == "gho_test"
        assert repository == "acme/calc"
        assert ref == "a" * 40
        return str(repo)

    monkeypatch.setattr(
        "app.services.github_client.download_repository_snapshot", fake_download
    )

    result = remediation.validate_patch_from_snapshot(
        github_token="gho_test",
        repository="acme/calc",
        commit_sha="a" * 40,
        patch=GOOD_PATCH,
        test_command="python -m pytest -q",
    )

    assert result["attempted"] is True
    assert result["patch_applied"] is True
    assert result["tests_passed"] is True, result
    assert result["source"] == "github-snapshot"

    # The fixture repo itself (standing in for the downloaded snapshot) must
    # never be mutated -- validate_patch always works on its own copy.
    assert repo.joinpath("calculator.py").read_text() == "def add(a, b):\n    return a - b\n"


def test_validation_uses_unique_per_job_sandbox_and_cleans_it(tmp_path, monkeypatch):
    repo = make_broken_repo(tmp_path)
    created = []

    from app.services.sandbox import sandbox_manager
    original_create = sandbox_manager.create

    def tracking_create(job_id):
        sandbox = original_create(job_id)
        created.append(sandbox.path)
        return sandbox

    monkeypatch.setattr(sandbox_manager, "create", tracking_create)
    result1 = remediation.validate_patch(
        repo_path=str(repo), patch=GOOD_PATCH, test_command="python -m pytest -q", job_id="job-a"
    )
    result2 = remediation.validate_patch(
        repo_path=str(repo), patch=GOOD_PATCH, test_command="python -m pytest -q", job_id="job-b"
    )

    assert result1["tests_passed"] is True
    assert result2["tests_passed"] is True
    assert created[0] != created[1]
    assert not created[0].exists()
    assert not created[1].exists()
