# Privacy-preserving analytics for humanitarian data

Web app with differential privacy on every query and a global privacy budget. Synthetic data only.
Design, threat model, evaluation, and the tested-versus-argued table are in `THREAT_MODEL.md`.

## Setup (Windows PowerShell, Python 3.14, PostgreSQL 18)

1. `python -m venv .venv`, activate it, then `pip install -r requirements.txt`
2. In `psql` as `postgres`, create the app role and database:
   `CREATE ROLE dp_app LOGIN PASSWORD 'choose-a-password';` and `CREATE DATABASE dp_analytics OWNER postgres;`
3. Apply the schema as `postgres`: `psql -U postgres -h localhost -d dp_analytics -f schema.sql`
4. Copy `.env.example` to `.env`. Set `DP_DATABASE_URL` to use your `dp_app` password, and set `DP_SECRET_KEY` to the output of
   `python -c "import secrets; print(secrets.token_hex(32))"`
5. Seed synthetic data and accounts: `python seed.py`, then `python seed_users.py` (it prompts for three passwords, 12+ characters each, one per account: field1, analyst1, admin1)
6. Run: `uvicorn app.main:app --reload`, then type `127.0.0.1:8000/login` into the browser
   (do not follow a link into the app: the session cookie is SameSite=Strict)

Dev and test scripts are in `dev/` (see `dev/README.md`).

Tested from a fresh clone: steps 1, 3, 4, 5 and 6 (schema, .env, seeding, sign-in and a charged query). Step 2 (creating the `dp_app` role) was not tested, because the role already existed on the test machine.

## Queries

Analysts build a query on /analyst: count or mean of monthly transfer, up to two group-bys (region, household size, transfer band) and optional filters. A preview shows cost and expected error before anything is charged. A count costs 0.1 and a mean costs 0.5, however many cells; the total budget is 10.0. Results are stored, so reopening one or downloading its CSV is free. Cells with a noisy count below 30 are shown as suppressed, but shown values can still be noise.


oise.py and queries.py are the older fixed-query code. The web app no longer uses them, but earlier tests still do.
