# AidBridge

AidBridge connects people who need help with people who can give it, and lets analysts study the resulting data with differential privacy. It is a student prototype. It has only been run on synthetic and demo data, and nothing in it has been independently audited.

The design, threat model, evaluation and the tested-versus-argued table are in `THREAT_MODEL.md`. The GitHub repository is still named `dp-analytics`.

## How it works

1. A requester posts a need: category, province, district, a description and a contact method. It starts as pending and is private.
2. A verifier reviews it, writes the public text, and approves or rejects. A rejection carries a note the requester sees.
3. Approved needs appear on a public board and a province map. The board shows only the category, province, district, month and the verifier's text. Anyone can browse it without an account.
4. A supporter can pledge to an approved need (at most 3 at a time) and give a contact detail that only verifiers see. The verifier arranges the handover outside AidBridge, then marks the need fulfilled or releases it back to the board.
5. Analysts run differentially private queries over a separate synthetic dataset. Every query is charged against a shared privacy budget and logged.

No money moves through the platform. It connects people, and any handover happens outside it.

## Status

Built. Everything below has script tests except the map drawing, the form dropdowns and the pledge and handover page layouts, which were checked by eye or not yet checked (see section 5 of `THREAT_MODEL.md`):

- Sign-up for requesters and supporters, with sign-in, CSRF tokens, login lockout and a sign-up throttle.
- Posting a need, with fixed province and district lists (validated as a pair) and a contact method with format checks. At most 3 pending needs per requester.
- The verifier queue: approve with a guarded public text, or reject with a note.
- The public board and province map, with filters and a per-address rate limit.
- The analytics engine over the synthetic dataset: count or mean, up to two group-bys, filters, a preview of cost and error, stored results and CSV download.
- Needs analytics over a synthetic needs table: counts only, four fixed groupings, each requester's first 3 needs counted, noise at sensitivity 3. Tested, and the form, the four previews and one result page (count by category) were checked in a browser.
- A shared privacy budget that is spent atomically and cannot be refunded by the app, and an append-only audit log.
- Pledging: a supporter pledges to a published need (at most 3 active pledges) with a private contact detail, and can cancel until the handover is arranged. The verifier sees both contacts, marks the handover arranged, marks the need fulfilled, or releases it back to the board with a reason. A database trigger enforces the status order, and one active pledge per need.

Not built:

- Notifications to either side, and expiry of pledges nobody follows up. An abandoned pledge holds a need until a verifier releases it.
- Analytics over the real needs table. Needs analytics runs only on a synthetic needs table (the `needs_synthetic` dataset); the real table (the `needs_live` dataset) is not wired.
- Email or phone verification, field-level encryption, hash-chained audit logs, and a per-person budget split.

## Roles

| Role | How it is created | What it can do |
|---|---|---|
| Visitor | no account | Browse the board and map |
| Requester | open sign-up | Post needs, see the status of its own needs |
| Supporter | open sign-up | Browse published needs, pledge to up to 3, cancel until the handover is arranged |
| Verifier | `seed_verifier.py` | Review needs, see private descriptions and contact details, arrange handovers, mark needs fulfilled, release pledges |
| Analyst | `seed_users.py` | Run private queries |
| Admin | `seed_users.py` | See the audit log and the budget, never results or needs |
| Field agent | `seed_users.py` | Add records to the synthetic analytics dataset (original design, write-only) |

## Setup (Windows PowerShell, Python 3.14, PostgreSQL 18)

1. `python -m venv .venv`, activate it, then `pip install -r requirements.txt`
2. In `psql` as `postgres` (on Windows `psql` is usually not on PATH; use the full path, for example `& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost`), create the app role and database:
   `CREATE ROLE dp_app LOGIN PASSWORD 'choose-a-password';` and `CREATE DATABASE dp_analytics OWNER postgres;`
3. Apply the schema as `postgres`: `psql -U postgres -h localhost -d dp_analytics -f schema.sql`. It already contains everything in the `add_*.sql` files; those are history, so do not run them on top.
4. Copy `.env.example` to `.env`. Set `DP_DATABASE_URL` to use your `dp_app` password, and set `DP_SECRET_KEY` to the output of
   `python -c "import secrets; print(secrets.token_hex(32))"`. Copy the `DP_DATABASE_URL` password exactly, then check it without printing it: `python -c "from sqlalchemy.engine import make_url; from app.config import settings; u=make_url(settings.database_url); print(u.username, u.database, len(u.password or ''))"` (compare the length with the real password).
5. Seed the synthetic data and the accounts:
   - `python seed.py`
   - `python seed_users.py` prompts for three passwords (12+ characters each): field1, analyst1, admin1
   - `python seed_verifier.py` prompts for a verifier username and password
   - `python seed_needs_synth.py` fills the synthetic needs table used by the needs analytics
6. Optional, for the map outlines: see "Boundary data" below. Without the file the board keeps working and the map area says it is unavailable (checked in a browser).
7. Run: `uvicorn app.main:app --reload`, then type `127.0.0.1:8000/board` or `127.0.0.1:8000/login` into the browser. Do not follow a link into the app, because the session cookie is SameSite=Strict.

The last full run from a fresh clone was at commit `7dd295a`. It covered the schema, `pip install -r requirements.txt`, `.env.example`, all four seed scripts (including the verifier and `seed_needs_synth.py`, which reproduced 20194 needs from 12000 people) and all 18 test scripts in `dev/`. On that database `needs_synthetic` had id 2, not 7, and the needs tests passed. Two stale tests were found and fixed (`test_engine_run` did not delete `results` rows; `test_provinces` used an out-of-date pattern). The outline file is gitignored, so the clone had none; the board tests passed without it, but rebuilding the file from `tools/` was not tried. Step 2 (creating the `dp_app` role, which already existed on the test machine) and the browser checks have not been tested from a clean clone. Exit codes were the evidence for most of the test runs.

## Boundary data

`app/provinces.py` and `app/districts.py` hold the 81 provinces and 973 districts, as plain ASCII names and codes. They were generated from the OCHA COD-AB Türkiye dataset on the Humanitarian Data Exchange (`data.humdata.org/dataset/cod-ab-tur`), licence CC BY-IGO, source: General Command of Mapping, Türkiye, via OCHA/HDX.

The map outlines (`app/static/geo/turkey_provinces.min.geojson`) are not committed, because the dataset's methodology says it was shared "for humanitarian use only" and it has not been confirmed that this use is covered. To rebuild them:

1. Download the GeoJSON zip from the dataset page and unzip it. You need `tur_admin1.geojson` (provinces) and, for the district list, `tur_admin2.geojson`.
2. From the project root: `python tools/simplify_geo.py <path to tur_admin1.geojson> app/static/geo/turkey_provinces.min.geojson`

To regenerate the two Python lists, run these from the project root, in this order: `python tools/make_provinces.py` (it reads the outlines written by the step above), then `python tools/make_districts.py <path to tur_admin2.geojson>`. These regeneration steps have not been run from a clean clone.

## Tests

Resetting the demo: `python tools\remove_demo_data.py` removes one named requester (default Tugba), one named supporter (default Diane) and everything attached to them (their needs, pledges and events). It is a rehearsal by default: the real DELETE statements run in a transaction that is rolled back. `--apply` plus typing `DELETE` commits. It refuses to run if anything unexpected is attached to those users or the counts differ from `--expect`, and it never touches the audit log, stored results or budgets. The defaults are this project's demo accounts, so on other data pass `--requester`, `--supporter` and `--expect`.

The scripts in `dev/` are run from the project root as modules, for example `python -m dev.test_board`. Most need the database owner's password, which they prompt for with hidden input, and they create and remove throwaway accounts. They never touch the real accounts. Do not run them against a database holding real data. `dev/README.md` lists the older scripts; the newer ones are the other `test_*.py` files in the same folder.

## Queries (analytics)

Analysts build a query on `/analyst`: count or mean of monthly transfer, up to two group-bys (region, household size, transfer band) and optional filters. A preview shows cost and expected error before anything is charged. A count costs 0.1 and a mean costs 0.5, however many cells; the total budget is 10.0. Results are stored, so reopening one or downloading its CSV is free. Cells with a noisy count below 30 are shown as suppressed, but shown values can still be noise.

Needs analytics has its own form on `/analyst`: counts of needs by category, province, status, or category and status, at 0.1 each from a separate budget of 10.0. Each requester counts for at most their first 3 needs, so noise is 3 times larger than for people counts (typical error about 30 per cell). The data is synthetic and its distributions are assumptions.

`noise.py` and `queries.py` are the older fixed-query code. The web app no longer uses them, but earlier tests still do.

## Credits

Boundary data: General Command of Mapping, Türkiye, via OCHA and the Humanitarian Data Exchange (CC BY-IGO). The idea of a verified needs map follows İhtiyaç Haritası, which this project is not affiliated with.