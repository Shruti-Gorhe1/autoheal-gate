"""Tests for the LLM fix path when it is given real file contents.

The deterministic engine only mechanically repairs a narrow pattern
(``return a - b`` style arithmetic). Everything else -- like a hardcoded
argument buried inside a function call -- needs the model to actually see
the file. These tests pin that the file's real content reaches the model,
that only offered files can be edited, and that the diff produced against
the real content is one `git apply` will accept.
"""

from __future__ import annotations

from app.agents import analyzers
from app.agents.base import AgentContext
from app.agents.pipeline import FixAgent

RETRIEVER_LOG = """COMMAND: pytest -q
EXIT CODE: 1

STDOUT:
tests/test_retriever.py F                                                [100%]

================================== FAILURES ===================================
______________________________ test_retrieve_top_k _____________________________

tests/test_retriever.py:8: in test_retrieve_top_k
    results = retrieve_documents("what is autoheal?", k=4)
src/rag/retriever.py:5: in retrieve_documents
    question, k=1
E   AssertionError: expected k=4, actual k=1
=========================== short test summary info ===========================
FAILED tests/test_retriever.py::test_retrieve_top_k - AssertionError: expected k=4, actual k=1
1 failed in 0.03s
"""

ORIGINAL_RETRIEVER = (
    "TOP_K = 4\n\n\n"
    "class _Store:\n"
    "    def similarity_search_with_score(self, question, k):\n"
    "        return [(question, k)] * k\n\n\n"
    "def get_vectorstore():\n"
    "    return _Store()\n\n\n"
    "def retrieve_documents(question, k=TOP_K):\n"
    "    results = get_vectorstore().similarity_search_with_score(\n"
    "        question, k=1\n"
    "    )\n"
    "    return results\n"
)

CORRECTED_RETRIEVER = ORIGINAL_RETRIEVER.replace("question, k=1", "question, k=k")


class StubLLMEngine:
    """Stands in for a real model call, but mirrors exactly what
    ``LLMEngine.propose_fix`` does with the response: turn "files" into a
    real diff via :func:`analyzers.apply_llm_file_edits`. Only the network
    call is faked -- the diff-computation path under test is not."""

    def __init__(self, response):
        self.response = dict(response)
        self.seen_file_contents = None
        self.seen_ctx = None

    def propose_fix(self, ctx, file_contents):
        self.seen_ctx = ctx
        self.seen_file_contents = file_contents
        result = dict(self.response)
        result["patch"] = analyzers.apply_llm_file_edits(
            file_contents, result.get("files", {})
        )
        return result


def make_retriever_repo(tmp_path):
    repo = tmp_path / "rag-service"
    src = repo / "src" / "rag"
    src.mkdir(parents=True)
    (src / "retriever.py").write_text(ORIGINAL_RETRIEVER, encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_retriever.py").write_text(
        "def test_retrieve_top_k():\n    pass\n", encoding="utf-8"
    )
    return repo


def build_ctx(repo_path):
    triage = analyzers.classify(RETRIEVER_LOG)
    root_cause = analyzers.analyze_root_cause(RETRIEVER_LOG, "", triage, str(repo_path))
    ctx = AgentContext(
        run_id="test-retriever",
        repository="acme/rag-service",
        commit_sha="abc123",
        ci_passed=False,
        logs=RETRIEVER_LOG,
        repo_path=str(repo_path),
        policy={"max_changed_files": 3},
    )
    ctx.triage = triage
    ctx.root_cause = root_cause
    return ctx


# ---------------------------------------------------------------------- gathering


def test_the_implicated_files_content_is_actually_read(tmp_path):
    repo = make_retriever_repo(tmp_path)
    contents = analyzers.gather_fix_context(str(repo), ["src/rag/retriever.py"])
    assert contents == {"src/rag/retriever.py": ORIGINAL_RETRIEVER}


def test_test_files_are_never_offered_as_editable_context(tmp_path):
    repo = make_retriever_repo(tmp_path)
    contents = analyzers.gather_fix_context(
        str(repo), ["tests/test_retriever.py", "src/rag/retriever.py"]
    )
    assert "tests/test_retriever.py" not in contents
    assert "src/rag/retriever.py" in contents


def test_a_missing_file_is_silently_skipped(tmp_path):
    repo = make_retriever_repo(tmp_path)
    contents = analyzers.gather_fix_context(str(repo), ["src/rag/does_not_exist.py"])
    assert contents == {}


# ------------------------------------------------------- remote (no checkout)


def test_no_local_checkout_falls_back_to_the_remote_reader():
    """The common real-world case: a repository reached only via webhook,
    with no local clone registered anywhere."""
    calls = []

    def remote_reader(path):
        calls.append(path)
        return ORIGINAL_RETRIEVER if path == "src/rag/retriever.py" else None

    contents = analyzers.gather_fix_context(
        None, ["src/rag/retriever.py"], remote_reader=remote_reader
    )
    assert contents == {"src/rag/retriever.py": ORIGINAL_RETRIEVER}
    assert calls == ["src/rag/retriever.py"]


def test_a_local_hit_never_calls_the_remote_reader(tmp_path):
    """Local disk is always tried first; the remote fallback is only for
    what local genuinely can't supply, not a second, wasted network call."""
    repo = make_retriever_repo(tmp_path)

    def remote_reader(path):
        raise AssertionError(f"remote_reader should not be called for {path}")

    contents = analyzers.gather_fix_context(
        str(repo), ["src/rag/retriever.py"], remote_reader=remote_reader
    )
    assert contents == {"src/rag/retriever.py": ORIGINAL_RETRIEVER}


def test_remote_reader_covers_only_what_local_could_not_supply(tmp_path):
    repo = make_retriever_repo(tmp_path)
    remote_calls = []

    def remote_reader(path):
        remote_calls.append(path)
        return "# fetched from GitHub\n"

    contents = analyzers.gather_fix_context(
        str(repo),
        ["src/rag/retriever.py", "src/rag/other_missing.py"],
        remote_reader=remote_reader,
    )
    assert contents["src/rag/retriever.py"] == ORIGINAL_RETRIEVER
    assert contents["src/rag/other_missing.py"] == "# fetched from GitHub\n"
    assert remote_calls == ["src/rag/other_missing.py"]


def test_without_a_remote_reader_a_missing_local_file_still_yields_nothing(tmp_path):
    repo = make_retriever_repo(tmp_path)
    contents = analyzers.gather_fix_context(str(repo), ["src/rag/other_missing.py"])
    assert contents == {}


def test_deterministic_synthesis_declines_this_shape_of_bug(tmp_path):
    """Confirms the premise: the arithmetic-only deterministic path has
    nothing to offer here, which is exactly why the LLM path must engage."""
    repo = make_retriever_repo(tmp_path)
    triage = analyzers.classify(RETRIEVER_LOG)
    root_cause = analyzers.analyze_root_cause(RETRIEVER_LOG, "", triage, str(repo))
    fix = analyzers.synthesize_fix(RETRIEVER_LOG, root_cause, str(repo))
    assert fix["patch"] == ""


# ---------------------------------------------------------------------- diffing


def test_llm_edits_are_turned_into_a_real_diff():
    original = {"src/rag/retriever.py": ORIGINAL_RETRIEVER}
    proposed = {"src/rag/retriever.py": CORRECTED_RETRIEVER}
    patch = analyzers.apply_llm_file_edits(original, proposed)

    assert "diff --git a/src/rag/retriever.py b/src/rag/retriever.py" in patch
    assert "-        question, k=1" in patch
    assert "+        question, k=k" in patch


def test_an_unoffered_path_is_never_diffed():
    """The model naming a file it was never shown must not be trusted."""
    original = {"src/rag/retriever.py": ORIGINAL_RETRIEVER}
    proposed = {
        "src/rag/retriever.py": CORRECTED_RETRIEVER,
        ".github/workflows/ci.yml": "malicious: true\n",
    }
    patch = analyzers.apply_llm_file_edits(original, proposed)
    assert ".github" not in patch


def test_an_unchanged_file_produces_no_diff():
    original = {"src/rag/retriever.py": ORIGINAL_RETRIEVER}
    proposed = {"src/rag/retriever.py": ORIGINAL_RETRIEVER}
    assert analyzers.apply_llm_file_edits(original, proposed) == ""


# ------------------------------------------------------------------- fix agent


def test_the_fix_agent_grounds_the_llm_in_real_file_content_and_validates_it(tmp_path):
    """End to end: deterministic synthesis declines, the LLM is given the
    actual file, proposes the real fix, and it validates against real tests."""
    repo = make_retriever_repo(tmp_path)
    # A real test that fails against the bug and passes against the fix --
    # no mocking needed since the fixture module defines its own fake store.
    (repo / "tests" / "test_retriever.py").write_text(
        "from src.rag.retriever import retrieve_documents\n"
        "\n\n"
        "def test_retrieve_top_k():\n"
        "    results = retrieve_documents('q', k=4)\n"
        "    assert len(results) == 4\n",
        encoding="utf-8",
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "rag" / "__init__.py").write_text("", encoding="utf-8")

    ctx = build_ctx(repo)
    ctx.test_command = "python -m pytest -q"

    stub = StubLLMEngine(
        {
            "proposal": "k is hardcoded to 1 instead of using the k parameter.",
            "validation_plan": "Run the retriever test suite.",
            "files": {"src/rag/retriever.py": CORRECTED_RETRIEVER},
        }
    )

    FixAgent(llm_engine=stub).execute(ctx)

    assert stub.seen_file_contents == {"src/rag/retriever.py": ORIGINAL_RETRIEVER}
    assert "k=k" in ctx.fix["patch"]
    assert ctx.fix["method"] == "llm-proposal"

    from app.agents.pipeline import ValidationAgent

    ValidationAgent().execute(ctx)
    assert ctx.validation["patch_applied"] is True
    assert ctx.validation["tests_passed"] is True, ctx.validation


def test_the_fix_agent_surfaces_the_llms_explanation_when_it_declines(tmp_path):
    repo = make_retriever_repo(tmp_path)
    ctx = build_ctx(repo)

    stub = StubLLMEngine(
        {
            "proposal": "The vectorstore configuration is external to this file; "
            "no safe change exists within the files provided.",
            "files": {},
        }
    )

    FixAgent(llm_engine=stub).execute(ctx)

    assert ctx.fix["patch"] == ""
    assert "external to this file" in ctx.fix["proposal"]
    assert ctx.fix["method"] == "llm-declined"


def test_no_files_readable_still_asks_the_model_for_an_explanation(tmp_path):
    ctx = build_ctx(tmp_path / "nonexistent-repo")

    stub = StubLLMEngine({"proposal": "No file could be read.", "files": {}})
    FixAgent(llm_engine=stub).execute(ctx)

    assert stub.seen_file_contents == {}
    assert ctx.fix["patch"] == ""


def test_no_local_checkout_still_produces_a_fix_via_github(tmp_path, monkeypatch):
    """The exact scenario this fallback exists for: a repository the gate
    only knows about through a webhook, with no local_path registered and
    no repo-path passed by the caller -- only a GitHub token."""
    ctx = build_ctx(tmp_path / "does-not-exist")
    ctx.repo_path = None
    ctx.github_token = "gho_test_token"

    def fake_fetch(token, repo, path, ref):
        assert token == "gho_test_token"
        assert repo == "acme/rag-service"
        assert path == "src/rag/retriever.py"
        return ORIGINAL_RETRIEVER

    monkeypatch.setattr(
        "app.services.github_client.fetch_file_sync", fake_fetch
    )

    stub = StubLLMEngine(
        {
            "proposal": "k is hardcoded to 1 instead of using the k parameter.",
            "files": {"src/rag/retriever.py": CORRECTED_RETRIEVER},
        }
    )
    FixAgent(llm_engine=stub).execute(ctx)

    assert stub.seen_file_contents == {"src/rag/retriever.py": ORIGINAL_RETRIEVER}
    assert "k=k" in ctx.fix["patch"]

    # Without a local checkout, the fix is real but still unproven -- the
    # policy engine must HOLD this, never fabricate a PASS.
    from app.agents.pipeline import ValidationAgent
    from app.gate import policy as P

    ValidationAgent().execute(ctx)
    assert ctx.validation["attempted"] is False
    decision = P.evaluate(ctx.evidence(), P.merge_policy({"require_human_approval": False}))
    assert decision.verdict == P.HOLD


def test_the_complete_loop_downloads_patches_and_tests_with_no_checkout_anywhere(
    tmp_path, monkeypatch
):
    """The full request: no repo_path, no local_path -- AutoHeal downloads
    the exact commit, applies the LLM's fix there, and runs the real tests.
    A validated fix with approval off should then reach a genuine PASS."""
    repo = make_retriever_repo(tmp_path)
    (repo / "tests" / "test_retriever.py").write_text(
        "from src.rag.retriever import retrieve_documents\n"
        "\n\n"
        "def test_retrieve_top_k():\n"
        "    results = retrieve_documents('q', k=4)\n"
        "    assert len(results) == 4\n",
        encoding="utf-8",
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "rag" / "__init__.py").write_text("", encoding="utf-8")

    ctx = build_ctx(repo)  # RCA reads the real file to seed likely_files
    ctx.repo_path = None  # ...but no checkout is registered for this run
    ctx.github_token = "gho_test_token"
    ctx.test_command = "python -m pytest -q"

    monkeypatch.setattr(
        "app.services.github_client.fetch_file_sync",
        lambda token, repo_, path, ref: ORIGINAL_RETRIEVER,
    )
    # Stand in for the download: hand back the same on-disk fixture repo.
    # validate_patch always copies its input before touching it, so the
    # fixture is never mutated even though it's reused as the "snapshot".
    monkeypatch.setattr(
        "app.services.github_client.download_repository_snapshot",
        lambda token, repo_, ref, dest_dir, timeout=60.0: str(repo),
    )

    stub = StubLLMEngine(
        {
            "proposal": "k is hardcoded to 1 instead of using the k parameter.",
            "files": {"src/rag/retriever.py": CORRECTED_RETRIEVER},
        }
    )

    FixAgent(llm_engine=stub).execute(ctx)
    assert ctx.fix["patch"]

    from app.agents.pipeline import ValidationAgent
    from app.gate import policy as P

    ValidationAgent().execute(ctx)
    assert ctx.validation["attempted"] is True
    assert ctx.validation["patch_applied"] is True
    assert ctx.validation["tests_passed"] is True, ctx.validation
    assert ctx.validation["source"] == "github-snapshot"

    decision = P.evaluate(ctx.evidence(), P.merge_policy({"require_human_approval": False}))
    assert decision.verdict == P.PASS, decision.reason

    # The real fixture repo -- standing in for what a real GitHub download
    # would have produced -- was never mutated by any of this.
    assert (
        repo.joinpath("src", "rag", "retriever.py").read_text() == ORIGINAL_RETRIEVER
    )


def test_without_a_github_token_the_remote_fallback_is_never_attempted(tmp_path):
    """No token means no fallback is even offered to gather_fix_context --
    not a network call that then fails, simply nothing to try."""
    ctx = build_ctx(tmp_path / "does-not-exist")
    ctx.repo_path = None
    ctx.github_token = None

    stub = StubLLMEngine({"proposal": "no files", "files": {}})
    FixAgent(llm_engine=stub).execute(ctx)
    assert stub.seen_file_contents == {}
