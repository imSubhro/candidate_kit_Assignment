# Employee Attendance & Analytics API

A FastAPI + MongoDB REST service for employee attendance tracking and workforce analytics. It implements the full contract in `prd.md`: strict epoch-ms validation, overnight shifts, a 10-minute grace period, overtime thresholds, `Decimal` half-up rounding, HR regularization with an immutable audit trail, and analytics served entirely by MongoDB aggregation pipelines.

**Highlights**

- 12 endpoints matching `openapi.yaml` exactly (paths, params, status codes, field names)
- Business rules R1-R10 verified against the spec's test vectors (16182 s → 4.50 not a half-day, `09:40:01` → 10 min late, 23/13 working days for July 2026)
- Concurrency-safe writes: unique indexes + compare-and-set → exactly one 201, the rest 409
- `/admin/explain` proves every query is **IXSCAN-only, no COLLSCAN** on a 100k-record dataset
- Ships with test suites: 83-check E2E + 100k explain benchmark

## How to run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set MONGO_URI and MONGO_DB
python sample_seed.py         # load sample data (6 employees, 9 attendance logs)
uvicorn app.main:app --port 8000 --reload
```

Requires **MongoDB 6.0 or newer** (tested on 7.0 locally via `docker run -p 27017:27017 mongo:7`).  
Real environment variables always override `.env` values.

## What is implemented

All endpoints from `openapi.yaml`:

- `GET /health` — 200/503 based on MongoDB ping
- `POST /employees`, `GET /employees` — create and list with pagination
- `POST /attendance/punch-in`, `POST /attendance/punch-out` — with full R1-R5 business rules
- `GET /attendance` — filtered, sorted, paginated in the database (no memory load)
- `PATCH /attendance/{emp_code}/{date}` — regularize with compare-and-set and audit trail
- `GET /analytics/employees/{emp_code}/monthly` — MongoDB aggregation
- `GET /analytics/departments/summary` — MongoDB aggregation, zero-log employees included
- `GET /analytics/leaderboard/late` — `$setWindowFields`/`$rank` for competition ranking
- `GET /analytics/departments/{department}/trend` — `$densify` gap fill, 7-day moving average
- `GET /admin/explain/{endpoint}` — `executionStats` explain for all five endpoints

## Notes

- All instants are epoch **milliseconds** (integers), never floats or strings.
- Rounding uses `decimal.Decimal` with `ROUND_HALF_UP` throughout.
- Overnight shifts (shift_end ≤ shift_start) are fully supported in R1, R2, R3.
- Concurrency: punch-in and employee create rely on unique indexes; punch-out uses atomic `update_one` with `punch_out: None` filter; PATCH uses compare-and-set with retry.
- `moving_avg_7d` is the mean of the rounded (4 dp) `attendance_rate` values in the 7-day window, then rounded again to 4 dp.
- Logs dated before `joined_on` are not filtered out of log metrics (spec says to follow the literal contract).

## Tests

Requires MongoDB running on `localhost:27017` (exits with a clear message if not). Each script returns exit code 0 on pass, 1 on failure.

```bash
python tests/e2e_test.py       # 83 checks: every endpoint, R1-R10 rules, boundary vectors,
                               # regularize history, 422 validation matrix, races, _id leaks
                               # (drops and reseeds the database first)
python tests/explain_100k.py   # builds 100k records: all 5 explains show IXSCAN, no COLLSCAN,
                               # plus endpoint latency spot checks
python sample_seed.py          # restore clean sample data after explain_100k
```

## Nothing missing

All endpoints and business rules R1-R10 are implemented.
