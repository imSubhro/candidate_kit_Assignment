# DECISIONS.md

## Q1 – Indexes

Eight indexes. On `employees`: unique `{emp_code}` (uniqueness plus lookups), `{department, emp_code}` (filtered employee list), `{department, joined_on}` and `{joined_on}` (headcount, R9). On `attendance_logs`: unique `{emp_code, date}` (punch-in race guarantee, per-employee monthly), `{date: -1, emp_code: 1}` (list sort), `{status, date: -1, emp_code}` (status filter), `{emp_code, punch_in: -1}` (punch-out finds the latest punch-in at or before the timestamp). Considered and rejected: a standalone `{emp_code}` on logs — it is a redundant prefix of the unique compound index, so it only adds write cost.

## Q2 – Punch-in race

Both requests pass the employee lookup and call `insert_one`. The unique `{emp_code, date}` index lets exactly one insert succeed (201); the other raises `DuplicateKeyError`, which we map to 409. There is no check-then-insert step, so the index is the only gate.

## Q3 – Ties at the cutoff

`$rank` gives tied employees the same rank, and we filter on `rank <= limit`, so every row tying at the boundary is returned even when that exceeds `limit`. Rows sort by `total_late_minutes` desc, then `emp_code` asc.

## Q4 – Headcount

The summary starts from `employees` (`joined_on <= month end`) and left-joins logs, so employees with zero logs still contribute to `headcount`.

## Q5 – Scaling to 100x

Pre-aggregated monthly rollup documents, upserted on every punch-in, punch-out and regularize. Analytics then reads a small `(emp_code, month)` collection instead of scanning 10M+ logs.
