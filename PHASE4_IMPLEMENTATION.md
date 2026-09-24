# Phase 4 — Google Cloud Build Integration

## Scope

Phase 4 adds **Google Cloud Build as a CI/CD provider** without changing the
LangGraph/agent architecture, queueing model, workers, sandbox, frontend, or
persistent job-state model.

The source and CI/CD dimensions remain independent. The primary cross-provider
case is:

```text
GitHub repository
      |
      v
Google Cloud Build
      |
      v
   PipelineEvent
      |
      +--> source_provider = github
      +--> cicd_provider   = gcp
      |
      v
  ProviderResolver
      |
      v
   Gate Service
```

## Implemented

- `GoogleCloudBuildProvider` as a **CICDProvider only**.
- `GoogleCloudBuildClient` using Cloud Build REST APIs.
- Cloud Build Pub/Sub push payload decoding.
- Terminal build status normalization into `PipelineEvent`.
- Cloud Build build-log retrieval from `logsBucket` when available.
- Explicit project/location handling.
- Distinct GCP credentials through `provider_tokens["gcp"]`.
- `POST /api/integrations/gcp/webhook`.
- Optional `X-AutoHeal-GCP-Secret` shared-secret protection for the webhook.
- Provider capabilities expose only `get_failure_logs`; source-control writes
  and reads remain owned by the independently resolved source provider.
- Gate evidence collection now uses normalized `PipelineEvent.run_id`, so
  string build IDs such as Cloud Build UUIDs work without changing the legacy
  integer `workflow_run_id` database field.

## Authentication

The GCP client accepts an explicit server-side access token or uses Google
Application Default Credentials through `google-auth`. No credential is sent
to the browser or LLM context.

Environment variables:

```text
GOOGLE_CLOUD_PROJECT=
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=
GOOGLE_CLOUD_ACCESS_TOKEN=
GCP_WEBHOOK_SECRET=
```

## Cloud Build event model

Cloud Build can publish build state changes through Pub/Sub. The push payload
contains `message.data` as base64-encoded JSON for the Build resource and
`message.attributes` containing the build ID/status. The implementation
accepts that documented envelope and also accepts a direct Build JSON payload
for local unit testing.

## Source identity

Cloud Build can build code from several repository connection mechanisms.
This phase does **not** create a new GCP source-control adapter. For the
existing cross-provider flow, the source provider can be explicitly indicated
with `_SOURCE_PROVIDER` in Cloud Build substitutions. GitHub is the default
source interpretation when a repository is represented as `owner/name`.

## Tests

Phase 4 adds provider, resolver, credential-routing, normalization, log
retrieval, and webhook tests. The complete regression suite must be run from
the Phase 4 virtual environment before the phase is marked verified.
