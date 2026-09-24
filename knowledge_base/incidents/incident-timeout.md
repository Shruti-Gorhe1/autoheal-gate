# Incident: API timeout regression

Symptoms: unit and integration tests fail after a recent commit. Error messages mention request timeouts or expected timeout values.

Typical root cause: application timeout configuration changed from 30 seconds to 5 seconds while tests and downstream callers still assume the previous behavior.

Recommended investigation: inspect the current commit diff, search for timeout configuration, compare the last successful pipeline, and confirm whether the shorter timeout is intentional.

Known remediation: restore the intended timeout or update the affected tests and callers only after confirming the new contract. Always rerun the complete test suite before release.
