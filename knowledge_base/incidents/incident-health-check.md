# Incident: Deployment health-check failure

Symptoms: build and tests pass, but deployment is marked unhealthy or a service readiness check returns a 4xx/5xx response.

Typical root cause: an invalid health-check path, port, environment variable, startup command, or service dependency.

Recommended investigation: compare deployment configuration with the previous successful version and inspect application startup logs.

Known remediation: correct the health-check configuration or application startup settings, validate in isolation, and keep rollback available until the service becomes healthy.
