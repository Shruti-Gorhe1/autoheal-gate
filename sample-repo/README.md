# Sample repository

A deliberately tiny, deliberately broken repository used to demonstrate
AutoHeal Gate end to end without needing a real project on hand.

`calculator.py` defines `add(a, b)` — but subtracts instead. Its test,
`tests/test_calculator.py`, asserts the correct behavior and fails:

```
assert add(2, 3) == 5
E   assert -1 == 5
```

This is exactly the shape of bug the deterministic fix agent can repair on
its own: a failing numeric assertion whose expected and actual values pin
down the fix precisely, in a file the traceback lets AutoHeal locate.

Run it directly:

```bash
cd sample-repo
python -m pytest -q        # fails: 1 failed
```

Then either use the dashboard's **Try the gate** page, or call the API
directly:

```bash
curl -X POST http://localhost:8000/api/gate/check \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $YOUR_KEY" \
  -d '{
    "repository": "autoheal/sample-repo",
    "commit_sha": "0000000000000000000000000000000000demo",
    "ci_passed": false,
    "repo_path": "'"$(pwd)"'",
    "test_command": "python -m pytest -q"
  }'
```

`.autoheal/policy.yml` in this folder loosens the default policy slightly so
the whole loop is visible in one call; see the top-level
[`examples/ci-cd-integration.md`](../examples/ci-cd-integration.md) for how a
real project would configure its own policy.

To reset the bug after AutoHeal fixes it (or after you fix it yourself while
experimenting), restore the line above from this README, or `git checkout`
the file if you're working from a clone.
