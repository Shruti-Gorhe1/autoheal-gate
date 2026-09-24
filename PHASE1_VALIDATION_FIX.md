# Phase 1 Validation Fix

## Problem found
The local Windows test run reported 10 failures. The common root cause for the remediation-related failures was that test commands such as `python -m pytest -q` were executed through `shell=True`. On Windows this can resolve a different Python installation than the interpreter running AutoHeal, causing a valid patched workspace to report a failed test command.

The resulting validation failure caused downstream Gate API tests to return `BLOCK` instead of `HOLD`/`PASS`.

## Change
`backend/app/services/remediation.py` now:
- uses the current AutoHeal interpreter (`sys.executable`) for simple `python ...` commands;
- runs `pytest ...` through `sys.executable -m pytest`;
- keeps `shell=True` for commands that intentionally need shell syntax, such as `echo marker > setup_marker.txt`;
- preserves existing patch application and validation behavior.

No provider architecture, LangGraph workflow, Azure/GCP/AWS support, queue, workers, or scaling work was added.

## Verification in the available environment
Targeted tests corresponding to the reported failures passed:
- `tests/test_remediation.py`: 6 passed
- `tests/test_agents.py`: 22 passed when combined with remediation (`22 passed`)
- the 2 affected `tests/test_llm_fix_context.py` tests: 2 passed
- the 4 affected `tests/test_gate_api.py` tests: 4 passed
- Python syntax compilation passed for `app/services/remediation.py`

The full suite was not claimed as verified in this environment because the full run exceeded the execution timeout.

## Required local verification
Run from the project's backend directory with the project's `.venv` active:

```cmd
pytest -q
```

Expected result: the previously reported 10 failures should no longer occur. Any remaining failure should be investigated separately rather than bypassed.
