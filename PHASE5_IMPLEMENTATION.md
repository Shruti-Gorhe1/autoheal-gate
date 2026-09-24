# Phase 5 — AWS CodePipeline / CodeBuild Integration

## Scope

Phase 5 adds AWS CI/CD support without changing the source-provider abstraction or the LangGraph/agent pipeline.

Implemented:

- `AWSProvider` as a CI/CD-only provider (`aws`)
- AWS CodeBuild build lookup and CloudWatch log retrieval
- AWS CodePipeline execution lookup support
- AWS EventBridge webhook normalization for CodeBuild and CodePipeline state-change events
- `POST /api/integrations/aws/webhook`
- server-side AWS credential configuration with boto3's normal credential chain
- independent source/CI-CD resolution (`GitHub + AWS`, `Azure + AWS`, etc.)
- AWS capability reporting limited to failure-log retrieval
- Phase 5 unit and HTTP webhook tests

## Event model

AWS CodeBuild and CodePipeline emit service events to EventBridge. CodeBuild build-state events include the build ID, project name, and build status. CodePipeline pipeline execution events include the pipeline name, execution ID, and state. AutoHeal accepts those events and, when the AWS event itself does not contain repository/commit information, hydrates the event from the corresponding AWS API before invoking the gate.

## Authentication

AWS authentication is server-side. The adapter accepts explicit access/secret/session settings but also supports boto3's normal credential chain (environment, shared profile, IAM role). Credentials are never sent to the frontend or LLM context.

## Deliberate capability boundary

AWS is registered only as a `CICDProvider` in Phase 5. Source operations remain with the independently resolved source provider. The adapter currently advertises only `get_failure_logs=True`; it does not claim commit diff, file retrieval, PR creation, comments, or status publishing.

## Validation

Run the complete regression suite with:

```text
python -m pip install -r requirements.txt
python -m pytest -q
```

Phase 5 should be considered complete only after the full suite is green.
