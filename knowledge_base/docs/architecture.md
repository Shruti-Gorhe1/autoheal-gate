# AutoHeal Gate architecture knowledge

AutoHeal Gate is a policy-governed CI/CD release gate, not just a chatbot
wrapper. Evidence and judgment are kept in separate stages so a language
model can never be the thing that unblocks a release.

Six agents run in order: Triage classifies the failure from the raw CI logs
using deterministic pattern matching. Retrieval pulls matching runbooks and
past incidents from this knowledge base. Root Cause explains the failure in
plain language, grounded in the current logs, not in retrieved history. Fix
proposes the smallest unified diff that could resolve it. Validation applies
that diff to a disposable copy of the repository and actually runs the test
suite. Only after all of that does the deterministic policy engine decide
PASS, HOLD, or BLOCK.

An LLM engine (Google ADK over a local Ollama model) can enrich the Root
Cause and Fix stages, but its output is reconciled against the deterministic
baseline rather than trusted outright: a model may not raise confidence above
what the log evidence supports, and a proposed patch is rejected if it edits
a test file, touches a protected path, or fails the isolated test run. When
Root Cause implicates specific source files, Fix is given their actual
current content -- not just the log excerpt -- so it can target a real line.
It reads a local checkout when one exists, and otherwise fetches the file
directly from the GitHub API at the exact commit under evaluation, so a
repository reached only through a webhook still gets a real, targeted fix
proposed rather than a description of the bug's class. It never writes a
diff itself: it returns corrected full file content for files it was shown,
and the diff against the real content is generated deterministically from
that, which also means a hallucinated file path or an untouched file is
simply dropped rather than trusted. Validation then applies that fix to a
local checkout when one is registered, or, given a GitHub token, downloads
the exact commit from GitHub as a disposable snapshot and validates that
instead -- an optional `setup_command` runs once first, since a downloaded
snapshot starts with no dependencies installed, unlike a local checkout. A
verdict of `PASS` therefore always means the fix was actually applied and
actually tested, whether or not the gate had a filesystem copy of the
repository to begin with; only the complete absence of both a checkout and a
token leaves a fix genuinely unprovable, which correctly stays `HOLD`.

OpenTelemetry instruments every agent span so latency, confidence, and
validation outcomes can be correlated with a specific gate run in Phoenix.
