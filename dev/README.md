# dev/ scripts

Run every script from the project root, as a module, with the venv active:

    python -m dev.test_roles

Evaluation outputs (`eval_accuracy.csv`, `eval_accuracy.png`) stay in the project root.

## Need only the app role (`dp_app`, from `.env`)
attack_sim.py (no database), test_dp_statistical.py (no database, takes a few minutes), check_audit.py, check_failures.py, check_rows.py, check_truth.py,
eval_accuracy.py, run_queries.py

## Prompt for the app accounts' passwords (visible input; dev accounts only)
test_roles.py, test_field.py, test_admin.py

## Need the database owner (`postgres`) password
test_budget.py (also needs the ADMIN_URL environment variable), test_refusal.py, test_collusion.py.
These create throwaway datasets and accounts and reset budget state. Never run them against real data.

## Not in the repo (gitignored, owner credentials)
dev_reset.py, set_budget.py, cleanup_final.py

## Field cap tests
test_field_cap.py prompts for field1's password and adds 5 records to the real dataset; run it only before a cleanup. test_field_cap_concurrent.py needs the owner password and uses a throwaway dataset and account.
