# AidBridge: working rules for Claude Code

A needs-matching platform with differentially private analytics (a graduation project). FastAPI, Jinja2, raw SQL through SQLAlchemy, PostgreSQL 18, Python 3.14, Windows PowerShell.

## Safety rules (read first)
- Work only on the branch `financial-pivot`. Never commit to or push `main`. Commit only after the tests pass, and show the diff first.
- The database `dp_analytics` holds the old schema for the demo on `main`. Never run anything against it. Use `dp_analytics_v2` only. Before any command that touches the database, run `. .\tools\use_v2.ps1` in that terminal; it points the terminal at `dp_analytics_v2` and prints the name. If it prints anything else, stop.
- Never print, log, commit or ask for `.env` contents or any password. The tests under dev/ ask for the `postgres` password with a hidden prompt: do not run them yourself, ask the user to run them in their own terminal and paste the output.
- Privacy budgets (the `datasets` table) are spend-only and must never be reset. Tests use throwaway datasets.
- No real person's data anywhere: demo and synthetic data only.

## Design decisions (made by the author, in conversation)
- Financial needs, not goods. A requester posts a need with a goal, purpose, household size and an IBAN. A verifier checks the payee name and a supporting document outside the platform, then approves with a guarded public text. No money passes through the platform: donors send money straight to the requester's account.
- Partial pledges. Floor 10 lira. Total of active pledges never exceeds the goal (database trigger, row lock). A pledge goes pledged, sent (donor marks), confirmed (requester confirms receipt). Unsent pledges expire after 7 days. A need is fulfilled when confirmed receipts leave less than one minimum pledge open.
- Requesters can withdraw a need with a required reason. No refunds by the platform. The donor accepts that notice when pledging.
- A donor gives a contact when pledging (required, shown to verifiers only, for disputes). The account holder and IBAN are shown only to that donor, only while the pledge is pledged, sent or disputed. Donors do not see a withdrawal reason.
- Euro goals only for medical treatment abroad. The lira-per-euro rate is a placeholder in `settings`. Per-purpose goal ceilings are in `purpose_limits`. Institutions (schools, mosques) are out of scope. No second verifier.
- The "suggested amount" button reads `suggestion_stats` and is hidden until a band has enough finished needs (`settings.suggestion_min_needs`).
- Analytics: counts only, each requester's first 3 needs counted (person-level protection), Laplace noise at sensitivity 3, suppression below 30, fixed groupings, budget 10.0 per dataset. Datasets `needs_synthetic` and `needs_live`; the engine reads the views `synth_analytics` and `needs_analytics`.

## State of the migration
Done and tested: schema.sql (dev/test_schema), seeds, roles, requester flow, verifier flow, public board, supporter pledge flow (/support; the old /handovers is removed). Run `git log --oneline` for the latest step.
Not migrated yet: requester confirmation of receipt and disputes, the suggested-amount button, analytics routes (app/needs_engine.py is old), docs, diagrams.
Plan: step 6b requester confirmation and disputes, then the suggested-amount button as its own small step, step 7 analytics on the new views, step 8 README, THREAT_MODEL, diagrams, a fresh-clone run, merge.

## Code layout
- app/main.py: setup, login, register, admin, result pages, and old code not yet replaced. app/routes/*.py: one router per area. app/web.py: render, CSRF, templates, client_ip. app/reference.py and app/validation.py: lists and input checks.
- schema.sql is the source of truth. The database enforces the money and privacy rules with triggers, constraints and column-level grants, so a route is never the only guard. Do not widen the grants of the app role `dp_app` without asking.
- Included routers do not appear in `app.routes`; use `app.openapi()['paths']` to list routes.

## Tests
dev/test_schema, test_requesters, test_verifiers, test_board, test_supporters, test_roles, test_admin, run as `python -m dev.test_x`. Each refuses to run on a database without the new schema. Run all of them before a commit. Older tests (test_contact, test_form_errors, test_needs_*, test_budget, test_collusion) target the old schema and fail until migrated.

## Working style
- Small steps. Explain what changes and why in plain language. Say what was tested and what was only argued. No emojis. Do not overstate results.
- Read code before editing it. Make minimal edits and show diffs. Public pages set no cookies and never show private columns (description, contact, payee details, household size).
