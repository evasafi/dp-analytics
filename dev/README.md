# Scripts in dev/

Run them from the project root as modules, for example `python -m dev.test_board`.

Every test below that touches the database asks for the database owner's password (hidden input), creates and removes its own throwaway accounts, and refuses to run against a database without this schema. They do not touch real accounts or the real budgets. Do not run them against a database that holds real data.

## Application tests

| Script | What it covers |
|---|---|
| `test_schema.py` | The database itself: limits, grants, triggers, views and the pledge race |
| `test_roles.py`, `test_admin.py` | Role separation, CSRF tokens, the admin page |
| `test_requesters.py` | Sign-up limits, posting a need, my needs, withdrawing a need |
| `test_verifiers.py` | The review queue, the two checks, the guarded public text, decisions that race |
| `test_board.py` | The public board, a need's page, the map data, the rate limit |
| `test_supporters.py` | Pledging, account visibility, marking as sent, cancelling |
| `test_receipts.py` | Confirming and disputing a pledge, settling disputes |
| `test_suggestion.py` | The suggested amount and when it hides |
| `test_withdrawal_reason.py` | The text guard on a withdrawal reason |
| `test_analytics_engine.py`, `test_analytics_routes.py` | The private counts and the analyst pages |

To run them in turn:

```powershell
foreach ($t in "test_schema","test_roles","test_admin","test_requesters","test_verifiers","test_board","test_supporters","test_receipts","test_suggestion","test_analytics_engine","test_analytics_routes","test_withdrawal_reason") {
  "=== $t"
  python -m dev.$t 2>&1 | Select-String "^FAIL|PASS|Traceback|AssertionError"
}
```

## Other scripts

| Script | What it is |
|---|---|
| `test_dp_statistical.py`, `test_engine_dp.py` | Statistical checks that the noise matches its epsilon, with deliberately wrong controls. They take a few minutes |
| `test_ratelimit.py`, `test_ratelimit_expiry.py` | The login lockout. Run the second at least five minutes after the first. Both print their expected values; they do not assert them |
| `attack_sim.py` | A simulation of an analyst averaging away noise by repeating a query. It uses no database |
| `check_failures.py` | A helper that lists the failed logins used by the lockout tests |

Scripts of the earlier design (the beneficiary engine, the query builder, the old forms and the old handover flow) were removed. They are kept at the tag `demo-old-schema`.
