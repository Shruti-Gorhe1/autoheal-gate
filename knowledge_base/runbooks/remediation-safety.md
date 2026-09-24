# Runbook: Safe AI remediation

AI-generated patches are proposals, not trusted code. Never modify the developer's working tree directly.

Validation sequence:
- verify the patch is syntactically a unified diff
- run `git apply --check`
- apply it to a temporary copy
- run the project's test command
- capture stdout/stderr and exit code
- only present a release decision after validation

If validation fails, do not deploy automatically. Escalate to human review with the evidence and failed test output.
