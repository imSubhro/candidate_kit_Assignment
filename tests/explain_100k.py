import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pymongo import MongoClient
from fastapi.testclient import TestClient

import app.main as m

random.seed(42)

# ---- build ~100k attendance logs + 500 employees ----
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
try:
    client.admin.command("ping")
except Exception:
    sys.exit("MongoDB not reachable on localhost:27017 — start it first:  docker start mongo7")
db = client[os.getenv("MONGO_DB", "attendance_db")]

db.attendance_logs.drop()
emp_docs = []
for i in range(500):
    emp_docs.append({
        "emp_code": f"EMP{i:04d}",
        "name": f"Employee {i}",
        "email": f"emp{i}@corp.com",
        "department": ["Engineering", "Sales", "HR", "Finance"][i % 4],
        "joined_on": f"2026-0{random.randint(1, 6)}-{random.randint(10, 28):02d}",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    })
db.employees.drop()
db.employees.insert_many(emp_docs)

statuses = ["PRESENT", "PRESENT", "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
docs = []
base = datetime(2026, 5, 1)
for i in range(100_000):
    e = emp_docs[i % 500]
    d = base + timedelta(days=(i // 500) % 200)  # unique (emp_code, date)
    st = random.choice(statuses)
    has_punch = st in ("PRESENT", "WFH", "ON_DUTY")
    pin = datetime(d.year, d.month, d.day, random.randint(9, 11), random.randint(0, 59), tzinfo=timezone.utc) if has_punch else None
    pout = (pin + timedelta(hours=random.randint(7, 10))) if has_punch else None
    wh = round((pout - pin).total_seconds() / 3600, 2) if pout else None
    docs.append({
        "emp_code": e["emp_code"],
        "date": d.strftime("%Y-%m-%d"),
        "status": st,
        "punch_in": pin,
        "punch_out": pout,
        "work_hours": wh,
        "late_minutes": random.randint(0, 60) if has_punch else 0,
        "overtime_minutes": random.choice([0, 0, 0, 15, 45]),
        "half_day": bool(wh is not None and wh < 4.5),
        "history": [],
    })
    if len(docs) >= 5000:
        db.attendance_logs.insert_many(docs)
        docs = []
if docs:
    db.attendance_logs.insert_many(docs)

# recreate indexes (collection was dropped) — same 8 as app startup
from pymongo import ASCENDING, DESCENDING
db.employees.create_index([("emp_code", ASCENDING)], unique=True)
db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
db.employees.create_index([("joined_on", ASCENDING)])
db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])
db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
print("docs:", db.attendance_logs.estimated_document_count())
print("indexes:", db.attendance_logs.index_information().keys())

# ---- explain on all five via the API ----
with TestClient(m.app) as c:
    checks = [
        ("attendance_list", {"date_from": "2026-05-01", "date_to": "2026-08-31", "page": 1, "page_size": 20}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-06"}),
        ("department_summary", {"month": "2026-06"}),
        ("late_leaderboard", {"month": "2026-06", "limit": 10}),
        ("department_trend", {"department": "Engineering", "from": "2026-06-01", "to": "2026-06-30"}),
    ]
    all_ok = True
    for name, params in checks:
        r = c.get(f"/admin/explain/{name}", params=params)
        if r.status_code != 200:
            print(f"FAIL {name}: HTTP {r.status_code} {r.text[:200]}")
            all_ok = False
            continue
        exp = r.json()["explain"]
        raw = json.dumps(exp)
        has_collscan = "COLLSCAN" in raw
        has_ixscan = "IXSCAN" in raw
        ok = (not has_collscan) and has_ixscan
        all_ok &= ok
        print(("PASS" if ok else "FAIL"), f"explain {name}: IXSCAN={has_ixscan} COLLSCAN={has_collscan}")

    # latency spot checks
    for name, params in [
        ("attendance list", {"date_from": "2026-05-01", "date_to": "2026-08-31", "page": 1}),
        (None, None),
    ]:
        pass
    t0 = time.time()
    r = c.get("/attendance", params={"page_size": 20})
    print(f"GET /attendance: {r.status_code} in {time.time()-t0:.3f}s")
    t0 = time.time()
    r = c.get("/analytics/departments/summary", params={"month": "2026-06"})
    print(f"GET dept summary: {r.status_code} in {time.time()-t0:.3f}s")
    t0 = time.time()
    r = c.get("/analytics/leaderboard/late", params={"month": "2026-06"})
    print(f"GET leaderboard: {r.status_code} in {time.time()-t0:.3f}s")
    t0 = time.time()
    r = c.get("/analytics/departments/Engineering/trend", params={"from": "2026-06-01", "to": "2026-08-31"})
    print(f"GET trend (92d): {r.status_code} rows={len(r.json().get('items', []))} in {time.time()-t0:.3f}s")
    t0 = time.time()
    r = c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-06"})
    print(f"GET monthly: {r.status_code} in {time.time()-t0:.3f}s")

print("ALL EXPLAIN OK" if all_ok else "EXPLAIN FAILURES")
sys.exit(0 if all_ok else 1)
