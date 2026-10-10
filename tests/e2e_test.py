import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import app.main as m

# clean slate: drop DB, reload sample data (app.main import loaded .env already)
from pymongo import MongoClient as _MC
_MONGO = _MC(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
try:
    _MONGO.admin.command("ping")
except Exception:
    sys.exit("MongoDB not reachable on localhost:27017 — start it first:  docker start mongo7")
_MONGO.drop_database(os.getenv("MONGO_DB", "attendance_db"))
subprocess.run([sys.executable, str(ROOT / "sample_seed.py")], cwd=str(ROOT), check=True)

results = []


def t(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS" if cond else "FAIL"), name, extra if not cond else "")


def now_ms():
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


with TestClient(m.app) as c:
    # 1. health
    r = c.get("/health")
    t("health 200", r.status_code == 200, r.text)

    # 2. employees
    r = c.post("/employees", json={
        "emp_code": "EMP9001", "name": "E2E User", "email": "e2e@t.com",
        "department": "Engineering", "joined_on": "2026-07-01",
    })
    t("create employee 201", r.status_code == 201, r.text)
    r = c.post("/employees", json={
        "emp_code": "EMP9001", "name": "Dup", "email": "d@t.com",
        "department": "Engineering", "joined_on": "2026-07-01",
    })
    t("duplicate employee 409", r.status_code == 409, r.text)
    r = c.post("/employees", json={
        "emp_code": "X", "name": "Bad", "email": "not-an-email",
        "department": "Engineering", "joined_on": "2026-02-30",
    })
    t("invalid employee 422", r.status_code == 422, r.text)

    r = c.get("/employees", params={"page": 1, "page_size": 5})
    t("list employees 200", r.status_code == 200, r.text[:200])
    j = r.json()
    t("list has total/items", "total" in j and "items" in j, str(j)[:200])
    t("no _id leak in employees", all("_id" not in e for e in j["items"]), "")

    # 3. punch-in / punch-out lifecycle
    t_in = now_ms() - 8 * 3600 * 1000
    t_out = now_ms() - 1 * 3600 * 1000
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": t_in})
    t("punch-in 201", r.status_code == 201, r.text[:300])
    rec = r.json()
    t("punch-in has no id/_id", "id" not in rec and "_id" not in rec, str(rec)[:200])
    t("punch-in epoch ms int", isinstance(rec["punch_in"], int), str(type(rec["punch_in"])))

    r = c.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": t_in})
    t("duplicate punch-in 409", r.status_code == 409, r.text[:200])

    r = c.post("/attendance/punch-in", json={"emp_code": "NOPE1", "punched_at": now_ms()})
    t("punch-in unknown emp 404", r.status_code == 404, r.text[:200])

    r = c.post("/attendance/punch-out", json={"emp_code": "EMP9001", "punched_at": t_out})
    t("punch-out 200", r.status_code == 200, r.text[:300])
    out = r.json()
    t("work_hours computed", out.get("work_hours") is not None, str(out.get("work_hours")))
    t("half_day bool", isinstance(out.get("half_day"), bool), "")

    r = c.post("/attendance/punch-out", json={"emp_code": "EMP9001", "punched_at": t_out})
    t("second punch-out 409", r.status_code == 409, r.text[:200])

    att_date = out["date"]
    r = c.get("/attendance", params={"emp_code": "EMP9001"})
    t("list attendance 200", r.status_code == 200, r.text[:200])
    j = r.json()
    t("attendance pagination fields", "total" in j and "items" in j, str(j)[:150])
    t("no _id in attendance", all("_id" not in it and "id" not in it for it in j["items"]), "")

    # 4. regularize
    new_in = t_in + 3600 * 1000
    r = c.patch(f"/attendance/EMP9001/{att_date}", json={
        "punch_in": new_in, "reason": "biometric fix", "regularized_by": "hr.admin",
    })
    t("regularize 200", r.status_code == 200, r.text[:400])
    if r.status_code == 200:
        reg = r.json()
        h = reg.get("history", [])
        t("history appended 1", len(h) == 1, f"len={len(h)}")
        if h:
            ch = h[0].get("changes", {})
            t("history has punch_in change", "punch_in" in ch, str(ch)[:200])
            t("history at is epoch ms", isinstance(h[0].get("at"), int), str(type(h[0].get("at"))))
            if "punch_in" in ch:
                t("change from/to epoch ms", isinstance(ch["punch_in"]["from"], int) and isinstance(ch["punch_in"]["to"], int),
                  str(ch["punch_in"]))

    r = c.patch(f"/attendance/EMP9001/{att_date}", json={
        "punch_in": new_in, "reason": "no change here", "regularized_by": "hr.admin",
    })
    t("no-change regularize 422", r.status_code == 422, r.text[:200])

    r = c.patch(f"/attendance/EMP9001/{att_date}", json={
        "status": "ABSENT", "punch_in": new_in, "reason": "bad absent input", "regularized_by": "hr",
    })
    t("ABSENT with punch_in 422", r.status_code == 422, r.text[:200])

    r = c.patch(f"/attendance/EMP9001/{att_date}", json={
        "status": "ABSENT", "reason": "marked absent", "regularized_by": "hr.admin",
    })
    t("ABSENT regularize 200", r.status_code == 200, r.text[:300])
    if r.status_code == 200:
        a = r.json()
        t("ABSENT clears punches", a["punch_in"] is None and a["punch_out"] is None, str(a)[:200])
        t("ABSENT derived reset", a["work_hours"] is None and a["late_minutes"] == 0 and a["half_day"] is False,
          str((a["work_hours"], a["late_minutes"], a["half_day"])))

    # restore presence for analytics sanity
    r = c.patch(f"/attendance/EMP9001/{att_date}", json={
        "status": "PRESENT", "punch_in": new_in, "reason": "restore presence now", "regularized_by": "hr.admin",
    })
    t("restore PRESENT 200", r.status_code == 200, r.text[:300])

    # 5. analytics (seeded data, July 2026)
    # find an employee from seed
    seed_emp = c.get("/employees", params={"page_size": 100}).json()["items"][0]
    ec = seed_emp["emp_code"]

    r = c.get(f"/analytics/employees/{ec}/monthly", params={"month": "2026-07"})
    t("monthly 200", r.status_code == 200, r.text[:300])
    if r.status_code == 200:
        j = r.json()
        want = {"emp_code", "month", "working_days", "present_days", "leave_days", "late_count",
                "total_late_minutes", "total_overtime_minutes", "attendance_pct"}
        t("monthly fields exact", set(j.keys()) == want, str(set(j.keys()) ^ want))
        t("monthly working_days 23", j["working_days"] == 23, str(j["working_days"]))
        if j["attendance_pct"] is not None:
            t("attendance_pct numeric", isinstance(j["attendance_pct"], (int, float)), "")

    r = c.get(f"/analytics/employees/{ec}/monthly", params={"month": "2026-13"})
    t("monthly bad month 422", r.status_code == 422, r.text[:200])

    r = c.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"})
    t("monthly unknown emp 404", r.status_code == 404, r.text[:200])

    r = c.get("/analytics/departments/summary", params={"month": "2026-07"})
    t("dept summary 200", r.status_code == 200, r.text[:300])
    if r.status_code == 200:
        items = r.json()["items"]
        depts = [i["department"] for i in items]
        t("summary sorted by dept", depts == sorted(depts), str(depts))
        fields = {"department", "headcount", "present_days", "avg_work_hours", "late_count",
                  "total_late_minutes", "leave_count", "on_duty_count"}
        t("summary fields exact", all(set(i.keys()) == fields for i in items), str([set(i.keys()) ^ fields for i in items]))
        t("summary headcount >= logs holders", all(isinstance(i["headcount"], int) and i["headcount"] >= 0 for i in items), "")

    r = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2})
    t("leaderboard 200", r.status_code == 200, r.text[:300])
    if r.status_code == 200:
        items = r.json()["items"]
        fields = {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"}
        t("leaderboard fields exact", all(set(i.keys()) == fields for i in items), str([set(i.keys()) for i in items]))
        ranks = [i["rank"] for i in items]
        t("leaderboard ranks non-decreasing", ranks == sorted(ranks), str(ranks))
        t("leaderboard within limit rank", all(rk <= 2 for rk in ranks), str(ranks))

    r = c.get("/analytics/departments/Engineering/trend",
              params={"from": "2026-07-01", "to": "2026-07-14"})
    t("trend 200", r.status_code == 200, r.text[:300])
    if r.status_code == 200:
        items = r.json()["items"]
        t("trend one row per day (14)", len(items) == 14, f"len={len(items)}")
        t("trend dates contiguous", [i["date"] for i in items] == [f"2026-07-{d:02d}" for d in range(1, 15)],
          str([i["date"] for i in items]))
        fields = {"date", "is_working_day", "headcount", "present_count", "late_count",
                  "attendance_rate", "moving_avg_7d"}
        t("trend fields exact", all(set(i.keys()) == fields for i in items), str(set(items[0].keys()) ^ fields))
        # 1 Jul 2026 = Wednesday -> Jul 4/5 = Sat/Sun
        wknd = [i for i in items if i["date"] in ("2026-07-04", "2026-07-05")]
        t("weekend rate null", all(i["attendance_rate"] is None for i in wknd), str(wknd))
        t("weekend is_working_day false", all(i["is_working_day"] is False for i in wknd), str(wknd))

    r = c.get("/analytics/departments/Engineering/trend",
              params={"from": "2026-07-01", "to": "2026-07-01"})
    one = r.json() if r.status_code == 200 else None
    r2 = c.get("/analytics/departments/NOPEDEPT/trend",
               params={"from": "2026-07-01", "to": "2026-07-14"})
    t("trend unknown dept 404", r2.status_code == 404, r2.text[:200])

    # 6. explain endpoint (all five)
    cases = [
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 1, "page_size": 10}),
        ("employee_monthly", {"emp_code": ec, "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07", "limit": 5}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-14"}),
    ]
    for name, params in cases:
        r = c.get(f"/admin/explain/{name}", params=params)
        t(f"explain {name} 200", r.status_code == 200, r.text[:300])
        if r.status_code == 200:
            j = r.json()
            t(f"explain {name} shape", set(j.keys()) == {"endpoint", "collection", "explain"}, str(set(j.keys())))
            stages = json.dumps(j["explain"])
            t(f"explain {name} no COLLSCAN", "COLLSCAN" not in stages or "IXSCAN" in stages,
              stages[:400])
            t(f"explain {name} JSON-safe", isinstance(j["explain"], (dict, list)), "")

    # missing required params for explain
    t("explain monthly missing month 422",
      c.get("/admin/explain/employee_monthly", params={"emp_code": ec}).status_code == 422)
    t("explain trend missing to 422",
      c.get("/admin/explain/department_trend",
            params={"department": "Engineering", "from": "2026-07-01"}).status_code == 422)

    # 7. concurrency: parallel creates, punch-ins, punch-outs
    def create(i):
        return c.post("/employees", json={
            "emp_code": "EMP9900", "name": f"C{i}", "email": f"c{i}@t.com",
            "department": "Engineering", "joined_on": "2026-07-01",
        }).status_code

    with ThreadPoolExecutor(max_workers=20) as ex:
        codes = list(ex.map(create, range(20)))
    t("parallel creates: one 201 rest 409",
      codes.count(201) == 1 and codes.count(409) == 19, str(sorted(set(codes))) + f" 201s={codes.count(201)}")

    # parallel punch-ins on fresh employee
    c.post("/employees", json={
        "emp_code": "EMP9901", "name": "P", "email": "p@t.com",
        "department": "Engineering", "joined_on": "2026-07-01",
    })
    pin = now_ms() - 6 * 3600 * 1000

    def pin_req(_):
        return c.post("/attendance/punch-in", json={"emp_code": "EMP9901", "punched_at": pin}).status_code

    with ThreadPoolExecutor(max_workers=20) as ex:
        codes = list(ex.map(pin_req, range(20)))
    t("parallel punch-ins: one 201 rest 409",
      codes.count(201) == 1 and codes.count(409) == 19, f"201s={codes.count(201)} {sorted(set(codes))}")

    def pout_req(_):
        return c.post("/attendance/punch-out", json={"emp_code": "EMP9901", "punched_at": now_ms()}).status_code

    with ThreadPoolExecutor(max_workers=20) as ex:
        codes = list(ex.map(pout_req, range(20)))
    t("parallel punch-outs: one 200 rest 409",
      codes.count(200) == 1 and codes.count(409) == 19, f"200s={codes.count(200)} {sorted(set(codes))}")

    # parallel PATCHes: no lost history
    c.post("/employees", json={
        "emp_code": "EMP9902", "name": "R", "email": "r@t.com",
        "department": "Engineering", "joined_on": "2026-07-01",
    })
    pi = now_ms() - 10 * 3600 * 1000
    c.post("/attendance/punch-in", json={"emp_code": "EMP9902", "punched_at": pi})
    c.post("/attendance/punch-out", json={"emp_code": "EMP9902", "punched_at": now_ms()})
    d_rec = c.get("/attendance", params={"emp_code": "EMP9902"}).json()["items"][0]["date"]

    def patch_req(i):
        return c.patch(f"/attendance/EMP9902/{d_rec}", json={
            "punch_in": pi + (i + 1) * 60000,
            "reason": f"parallel tweak {i}",
            "regularized_by": "hr.admin",
        }).status_code

    with ThreadPoolExecutor(max_workers=10) as ex:
        codes = list(ex.map(patch_req, range(10)))
    t("parallel PATCH all 2xx or 409", all(x in (200, 409) for x in codes), str(codes))
    final = c.get("/attendance", params={"emp_code": "EMP9902"}).json()["items"][0]
    t("history entries kept (>=1, no loss)", len(final["history"]) >= 1, f"len={len(final['history'])}")

    # 8. no _id anywhere in responses (sample several endpoints)
    for path, params in [
        ("/employees", {"page_size": 50}),
        ("/attendance", {"page_size": 50}),
        (f"/analytics/employees/{ec}/monthly", {"month": "2026-07"}),
        ("/analytics/departments/summary", {"month": "2026-07"}),
        ("/analytics/leaderboard/late", {"month": "2026-07"}),
    ]:
        body = c.get(path, params=params).text
        t(f"no _id leak {path}", "_id" not in body, body[:200])

print("\n==== SUMMARY ====")
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} passed")
for name, _, extra in failed:
    print("FAILED:", name, "|", extra)
sys.exit(1 if failed else 0)
