# Employee Attendance & Analytics API

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
- `GET /attendance` — filtered, sorted, paginated, cursor-based (no memory load)
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

## Nothing missing

All endpoints and business rules R1-R10 are implemented.
