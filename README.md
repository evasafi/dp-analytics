# AidBridge

AidBridge lets a person post a specific financial need (rent, a medical bill, a school cost) and lets other people pledge parts of it and send the money straight to the person's bank account. Analysts can study the needs with differential privacy. It is a student prototype (a graduation project). It has only been run on synthetic and demo data, and nothing in it has been independently audited or checked against the law (see `THREAT_MODEL.md`, section 4).

The design, the threat model, the limits and the tested-versus-argued evidence are in `THREAT_MODEL.md`. The GitHub repository is still named `dp-analytics`. The earlier version of the project (a different design, built around a beneficiary dataset) is at the tag `demo-old-schema`.

## How it works

1. A requester posts a need: a purpose, a place, household size, a goal, a private description, a contact, and the account holder's name and IBAN. It starts as pending and is private.
2. A verifier reviews it. Outside the platform the verifier checks that the account holder's name matches the requester's identity and that a supporting document exists. Approval needs both checks ticked and a public text written by the verifier. A rejection carries a reason the requester sees.
3. Approved needs appear on a public board and a province map, with a progress bar of confirmed and promised money. Anyone can browse it without an account.
4. A supporter pledges any amount from 10 lira up to what is still open. After pledging, the supporter sees the account holder and IBAN, sends the money directly, and marks the pledge as sent.
5. The requester says whether it arrived. If it did not, a verifier settles it from both sides' statements. When less than one minimum pledge is left open, the need is fulfilled.
6. A requester can withdraw a need with a reason that donors who pledged will read. The platform refunds nothing.
7. Analysts run differentially private counts over the needs. Every query is charged against a privacy budget and logged.

No money moves through the platform. It cannot see bank transfers, so it records claims, not payments.

## Status

Built, and covered by script tests (counts are in `THREAT_MODEL.md`, section 5):

- Sign-up for requesters and supporters, with sign-in, CSRF tokens, login lockout and a sign-up throttle.
- Posting a need, with purpose limits, euro goals for medical treatment abroad, a checked IBAN, fixed province and district lists, and at most 3 pending needs per requester.
- The verifier queue: approve with two checks and a guarded public text, or reject with a reason. A list of needs withdrawn after money was confirmed.
- The public board and a page per need, with filters, sorting, a province map and a per-address rate limit.
- Partial pledges, a suggested amount, marking a pledge as sent, cancelling, and expiry of unsent pledges.
- Receipts and disputes: the requester confirms or disputes a sent pledge, and a verifier settles disputes.
- Withdrawing a need with a reason that gets the same text guard as the public text.
- Differentially private counts over two datasets (synthetic and live demo needs), with five fixed groupings, a free preview, stored results and CSV download.
- A privacy budget that is spent atomically and cannot be refunded by the app, and an append-only audit log.

Checked in a browser, not by a test: the analyst and admin pages, and the requester's "My needs" page. The board, the pledge pages, the verifier pages and the analytics result pages have script tests but have not been looked at in a browser on this version. The interface has not been restyled.

Not built:

- Notifications, a clock for pledge expiry (it runs when pages load), email or phone verification, password reset, and a page for the event log.
- Field-level encryption, hash-chained audit logs, per-user budget caps, a Turkish translation.
- A check of the legal rules for collecting aid in Turkey. This is the biggest open question.

## Roles

| Role | How it is created | What it can do |
|---|---|---|
| Visitor | no account | Browse the board, a need's page and the map |
| Requester | open sign-up | Post needs, see their status and funding, withdraw a need, confirm or dispute pledges marked sent |
| Supporter | open sign-up | Pledge, see their pledges and the account to pay into, mark as sent, cancel |
| Verifier | `seed_verifier.py` | Review and approve or reject needs, see private details and contacts, settle disputes |
| Analyst | `seed_users.py` | Run private counts |
| Admin | `seed_users.py` | See the audit log and the budgets, never results or needs |

## Setup (Windows PowerShell, Python 3.14, PostgreSQL 18)

1. `python -m venv .venv`, activate it, then `pip install -r requirements.txt`
2. In `psql` as `postgres` (on Windows `psql` is usually not on PATH; use the full path, for example `& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost`), create the app role and database:
   `CREATE ROLE dp_app LOGIN PASSWORD 'choose-a-password';` and `CREATE DATABASE dp_analytics OWNER postgres;`
3. Apply the schema as `postgres`: `psql -U postgres -h localhost -d dp_analytics -f schema.sql`. This one file creates everything.
4. Copy `.env.example` to `.env`. Set `DP_DATABASE_URL` to use your `dp_app` password, and set `DP_SECRET_KEY` to the output of
   `python -c "import secrets; print(secrets.token_hex(32))"`. Copy the `DP_DATABASE_URL` password exactly, then check it without printing it: `python -c "from sqlalchemy.engine import make_url; from app.config import settings; u=make_url(settings.database_url); print(u.username, u.database, len(u.password or ''))"` (compare the length with the real password).
5. Seed the accounts and data:
   - `python seed_users.py` prompts for two passwords (12+ characters each): analyst1 and admin1
   - `python seed_verifier.py` prompts for a verifier username and password
   - `python seed_needs_synth.py` fills the synthetic needs table used by the `needs_synthetic` dataset
   - `python seed_demo.py` (optional) creates demo accounts whose names start with `demo_` (one verifier, twenty requesters named `demo_req01` to `demo_req20`, twenty-five supporters), and demo needs and pledges for the live dataset. It prompts for one password shared by all demo accounts.
6. Optional, for the map outlines: see "Boundary data" below. Without the file the board keeps working and the map area says it is unavailable.
7. Run: `uvicorn app.main:app --reload`, then type `127.0.0.1:8000/board` or `127.0.0.1:8000/login` into the browser. Do not follow a link into the app, because the session cookie is SameSite=Strict.

This version was run from a clean clone on 4 November 2026: steps 1, 3, 4, 5 and 7 above, then the twelve tests, all passing (see `THREAT_MODEL.md`, section 5). Creating the `dp_app` role (step 2) and rebuilding the map outlines were not part of that run.

## Boundary data

`app/provinces.py` and `app/districts.py` hold the 81 provinces and 973 districts, as plain ASCII names and codes. They were generated from the OCHA COD-AB Türkiye dataset on the Humanitarian Data Exchange (`data.humdata.org/dataset/cod-ab-tur`), licence CC BY-IGO, source: General Command of Mapping, Türkiye, via OCHA/HDX.

The map outlines (`app/static/geo/turkey_provinces.min.geojson`) are not committed, because the dataset's methodology says it was shared "for humanitarian use only" and it has not been confirmed that this use is covered. To rebuild them:

1. Download the GeoJSON zip from the dataset page and unzip it. You need `tur_admin1.geojson` (provinces) and, for the district list, `tur_admin2.geojson`.
2. From the project root: `python tools/simplify_geo.py <path to tur_admin1.geojson> app/static/geo/turkey_provinces.min.geojson`

To regenerate the two Python lists, run these from the project root, in this order: `python tools/make_provinces.py` (it reads the outlines written by the step above), then `python tools/make_districts.py <path to tur_admin2.geojson>`. These regeneration steps have not been run from a clean clone.

## Tests

The scripts in `dev/` are run from the project root as modules, for example `python -m dev.test_board`. Each asks for the database owner's password with hidden input, refuses to run on a database without this schema, and creates and removes its own throwaway accounts. They never touch real accounts or the real budgets. Do not run them against a database that holds real data. To run the main ones in turn:

```powershell
foreach ($t in "test_schema","test_roles","test_admin","test_requesters","test_verifiers","test_board","test_supporters","test_receipts","test_suggestion","test_analytics_engine","test_analytics_routes","test_withdrawal_reason") {
  "=== $t"
  python -m dev.$t 2>&1 | Select-String "^FAIL|PASS|Traceback|AssertionError"
}
```

`test_dp_statistical` and `test_engine_dp` check the noise statistically, and `test_ratelimit` and `test_ratelimit_expiry` check the login lockout (run the second at least five minutes after the first). `dev/README.md` lists every script in the folder, and `THREAT_MODEL.md` section 5 says which tests count as evidence.

## Queries (analytics)

Analysts use `/analyst`. They pick a dataset (`needs_synthetic` or `needs_live`) and one of five groupings: need type, province, status, need type by status, or goal band. A preview shows the cost and the typical error before anything is charged. A run costs 0.1 whatever the number of cells, and each dataset has a budget of 10.0. Results are stored, so reopening one or downloading its CSV is free.

Each requester counts for at most their first 3 needs, so one person changes any table by at most 3 and the noise is sized for that: a typical error of about 30 per cell. Cells whose noisy count is below 30 are shown as suppressed, but shown values can still be noise. The synthetic dataset's distributions are assumptions, and `needs_live` holds demo data.

`app/noise.py`, `app/engine.py` and `app/engine_noise.py` are code from the earlier engine. The analytics still use a few of their parts (the noise function, the grouping description and the cell limit).



## Credits

Boundary data: General Command of Mapping, Türkiye, via OCHA and the Humanitarian Data Exchange (CC BY-IGO). The idea of a verified needs map follows İhtiyaç Haritası, which this project is not affiliated with.
