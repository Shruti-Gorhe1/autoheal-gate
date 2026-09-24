# Incident: Dependency incompatibility

Symptoms: build or test stage fails immediately after a dependency update. Import errors, API changes, resolver conflicts, or runtime-version incompatibilities are common.

Typical root cause: a package version is incompatible with the project's Python/runtime version or with another pinned dependency.

Recommended investigation: compare the dependency diff with the last successful commit, inspect the lock/requirements file, and identify the first package that changed.

Known remediation: pin the dependency to a compatible version, update related packages together when required, and rerun the full test suite in an isolated environment.
