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
5. Seed synthetic data and accounts: `python seed.py`, then `python seed_users.py`
6. Run: `uvicorn app.main:app --reload`, then type `127.0.0.1:8000/login` into the browser
   (do not follow a link into the app: the session cookie is SameSite=Strict)

Dev and test scripts are in `dev/` (see `dev/README.md`).