# Phase 12 — Security, Secrets, and Provider Capabilities

Phase 12 hardens the existing AutoHeal architecture without changing the
provider/event, queue, worker, Context Builder, LangGraph, or frontend contracts.

## Security controls

- Credentials are redacted before audit/trace attributes are persisted.
- Common GitHub/AWS/Bearer credentials are scrubbed from free-form strings.
- Gate internal errors are sanitized before they are returned or audited.
- Webhook authentication remains permissive for local development only; a
  production-like `APP_ENV` requires the configured webhook secret/auth.
- Validation/setup commands reject obvious network download, repository push,
  credential-control, and destructive filesystem commands.
- Provider capability flags are enforced before optional reporting or PR
  creation operations.
- Existing per-job filesystem sandbox protection remains unchanged. It is not
  a container/OS security boundary; container isolation remains a separate
  hardening step.

## Provider capability model

Concrete providers advertise only implemented operations through
`ProviderCapabilities`. Provider adapters with no explicit capability object
remain backward-compatible with test doubles/custom adapters.

## Validation

Phase 12 adds security regression tests covering:

- secret redaction
- sanitized errors
- safe/blocked validation commands
- concrete provider capabilities
- production webhook-auth requirement

Run the complete regression suite with:

```text
python -m pytest -q
```

The Phase 11 verified baseline was 214 tests; Phase 12 adds 6 security tests,
so the expected total is 220 tests.
