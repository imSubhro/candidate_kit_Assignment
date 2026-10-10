"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real env vars win)
"""
import os
import re
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import bson.json_util
import json
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pydantic import StrictInt
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

# load_dotenv with override=False means real env vars always win
load_dotenv(override=False)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_SHIFT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# epoch ms bounds: >= year 1973-ish, <= year 2100
_EPOCH_MS_MIN = 100_000_000_000
_EPOCH_MS_MAX = 4_102_444_800_000

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}


# ─────────────────────────────────────────────────────────────────────────────
# MongoDB client & lifespan
# ─────────────────────────────────────────────────────────────────────────────
client: MongoClient = None  # type: ignore[assignment]
db = None  # type: ignore[assignment]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client, db
    uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.environ.get("MONGO_DB", "attendance_db")
    client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=2000)
    db = client[db_name]
    _create_indexes()
    yield
    client.close()


def _create_indexes():
    # employees indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, background=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], background=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], background=True)
    db.employees.create_index([("joined_on", ASCENDING)], background=True)

    # attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, background=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], background=True)
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], background=True)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], background=True)


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────────────────────────────────────
def ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds (truncated to seconds) → UTC-aware datetime."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    """Convert datetime → epoch milliseconds (integer math, no float error)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int((dt - EPOCH) / timedelta(milliseconds=1))


def validate_epoch_ms(v) -> int:
    """Validate that v is a strict int in [MIN, MAX]. Raises ValueError otherwise."""
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("must be an integer epoch milliseconds value")
    if v < _EPOCH_MS_MIN or v > _EPOCH_MS_MAX:
        raise ValueError(f"epoch ms must be between {_EPOCH_MS_MIN} and {_EPOCH_MS_MAX}")
    return v


def validate_date_str(s: str) -> str:
    """Validate YYYY-MM-DD strictly (regex + calendar parse). Returns the string."""
    if not _DATE_RE.match(s):
        raise ValueError(f"invalid date format: {s!r}")
    try:
        date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"invalid calendar date: {s!r}")
    return s


def validate_month_str(s: str) -> str:
    if not _MONTH_RE.match(s):
        raise ValueError(f"invalid month format: {s!r}")
    return s


def month_bounds(month: str):
    """Return (first_day_str, last_day_str, next_first_str) for a YYYY-MM month."""
    year, mon = int(month[:4]), int(month[5:])
    first = f"{year:04d}-{mon:02d}-01"
    last_day = calendar.monthrange(year, mon)[1]
    last = f"{year:04d}-{mon:02d}-{last_day:02d}"
    # next month first day
    if mon == 12:
        next_first = f"{year + 1:04d}-01-01"
    else:
        next_first = f"{year:04d}-{mon + 1:02d}-01"
    return first, last, next_first


def count_working_days(start_str: str, end_str: str) -> int:
    """Count Mon-Fri days from start_str to end_str inclusive."""
    start = date.fromisoformat(start_str)
    end = date.fromisoformat(end_str)
    if start > end:
        return 0
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # 0=Mon … 4=Fri
            count += 1
        cur += timedelta(days=1)
    return count


def round_half_up(value: Decimal, places: int) -> Decimal:
    quantizer = Decimal(10) ** -places
    return value.quantize(quantizer, rounding=ROUND_HALF_UP)


# ─────────────────────────────────────────────────────────────────────────────
# Business rule helpers (R1-R5)
# ─────────────────────────────────────────────────────────────────────────────
def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def compute_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Determine the attendance date string for a punch-in datetime (UTC-aware)."""
    # Convert to IST
    pin_ist = punch_in_dt.astimezone(IST)
    if is_overnight(shift_start, shift_end):
        se_h, se_m = map(int, shift_end.split(":"))
        shift_end_time_seconds = se_h * 3600 + se_m * 60
        pin_time_seconds = pin_ist.hour * 3600 + pin_ist.minute * 60 + pin_ist.second
        if pin_time_seconds < shift_end_time_seconds:
            # belongs to previous day's shift
            prev = pin_ist.date() - timedelta(days=1)
            return prev.isoformat()
    return pin_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """R2: Compute late_minutes. punch_in_dt is UTC-aware."""
    pin_ist = punch_in_dt.astimezone(IST)
    # Reconstruct shift_start on attendance_date in IST
    att_date = date.fromisoformat(attendance_date)
    ss_h, ss_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, ss_h, ss_m, 0, tzinfo=IST)
    diff_seconds = int((pin_ist - shift_start_dt).total_seconds())
    if diff_seconds <= 600:  # <= 10 min => not late (grace is strictly more than 10:00)
        return 0
    return diff_seconds // 60  # floor minutes since shift_start


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: Work hours rounded half-up to 2 decimal places."""
    secs = int((punch_out_dt - punch_in_dt).total_seconds())
    result = round_half_up(Decimal(secs) / Decimal(3600), 2)
    return float(result)


def compute_overtime_minutes(punch_out_dt: datetime, shift_end: str, attendance_date: str) -> int:
    """R3: Overtime minutes."""
    att_date = date.fromisoformat(attendance_date)
    se_h, se_m = map(int, shift_end.split(":"))
    # For overnight shifts shift_end is on the NEXT calendar day
    # We place shift_end on attendance_date; if overnight, it's +1 day
    # But we don't know here if overnight; caller should pass the right date context.
    # shift_end is always computed as: attendance_date's shift_end datetime
    # For overnight shifts, the shift end is att_date + 1 day.
    # We need the employee's shift info to know. Simpler: always use att_date; for overnight the
    # shift_end is next day. We pass a flag.
    shift_end_dt = datetime(att_date.year, att_date.month, att_date.day, se_h, se_m, 0, tzinfo=IST)
    return _overtime_from_shift_end(punch_out_dt, shift_end_dt)


def compute_overtime_minutes_with_overnight(
    punch_out_dt: datetime, shift_end: str, attendance_date: str, overnight: bool
) -> int:
    """R3: Overtime minutes, accounting for overnight shifts."""
    att_date = date.fromisoformat(attendance_date)
    se_h, se_m = map(int, shift_end.split(":"))
    if overnight:
        # shift ends on att_date + 1
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date
    shift_end_dt = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, se_h, se_m, 0, tzinfo=IST)
    return _overtime_from_shift_end(punch_out_dt, shift_end_dt)


def _overtime_from_shift_end(punch_out_dt: datetime, shift_end_dt: datetime) -> int:
    """Returns floor(minutes from shift_end to punch_out) if >= 30, else 0."""
    diff_seconds = int((punch_out_dt - shift_end_dt).total_seconds())
    if diff_seconds < 1800:  # < 30 min
        return 0
    return diff_seconds // 60


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = work_hours < 4.50 (using the already-rounded value)."""
    return Decimal(str(work_hours)) < Decimal("4.50")


# ─────────────────────────────────────────────────────────────────────────────
# Serialization helpers
# ─────────────────────────────────────────────────────────────────────────────
def serialize_employee(doc: dict) -> dict:
    """Convert a MongoDB employee doc to API response dict."""
    d = {k: v for k, v in doc.items() if k != "_id"}
    if isinstance(d.get("created_at"), datetime):
        d["created_at"] = dt_to_ms(d["created_at"])
    return d


def serialize_attendance(doc: dict) -> dict:
    """Convert a MongoDB attendance_log doc to API response dict."""
    d = {k: v for k, v in doc.items() if k != "_id"}
    # Defaults for missing legacy fields
    d.setdefault("late_minutes", 0)
    d.setdefault("overtime_minutes", 0)
    d.setdefault("half_day", False)
    d.setdefault("history", [])
    d.setdefault("punch_out", None)
    d.setdefault("work_hours", None)
    # Convert instants
    if isinstance(d.get("punch_in"), datetime):
        d["punch_in"] = dt_to_ms(d["punch_in"])
    if isinstance(d.get("punch_out"), datetime):
        d["punch_out"] = dt_to_ms(d["punch_out"])
    # Serialize history entries
    history = []
    for entry in d.get("history", []):
        h = dict(entry)
        if isinstance(h.get("at"), datetime):
            h["at"] = dt_to_ms(h["at"])
        changes = {}
        for field, change in h.get("changes", {}).items():
            c = dict(change)
            if field in ("punch_in", "punch_out"):
                if isinstance(c.get("from"), datetime):
                    c["from"] = dt_to_ms(c["from"])
                elif c.get("from") is None:
                    c["from"] = None
                if isinstance(c.get("to"), datetime):
                    c["to"] = dt_to_ms(c["to"])
                elif c.get("to") is None:
                    c["to"] = None
            changes[field] = c
        h["changes"] = changes
        history.append(h)
    d["history"] = history
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────
class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def val_emp_code(cls, v):
        if not _EMP_CODE_RE.match(v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("name")
    @classmethod
    def val_name(cls, v):
        if not (1 <= len(v) <= 100):
            raise ValueError("name must be 1-100 chars")
        return v

    @field_validator("email")
    @classmethod
    def val_email(cls, v):
        if len(v) > 120 or not _EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("department")
    @classmethod
    def val_department(cls, v):
        if not (1 <= len(v) <= 50):
            raise ValueError("department must be 1-50 chars")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def val_shift(cls, v):
        if not _SHIFT_RE.match(v):
            raise ValueError("shift must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def val_joined_on(cls, v):
        return validate_date_str(v)

    @model_validator(mode="after")
    def val_shift_diff(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_punched_at(cls, v):
        if v is None:
            return v
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def val_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {PRESENCE_STATUSES}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_punched_at(cls, v):
        if v is None:
            return v
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def val_instants(cls, v):
        if v is None:
            return v
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def val_status(cls, v):
        if v is not None and v not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
            raise ValueError("invalid status")
        return v

    @field_validator("reason")
    @classmethod
    def val_reason(cls, v):
        if not (5 <= len(v) <= 200):
            raise ValueError("reason must be 5-200 chars")
        return v

    @field_validator("regularized_by")
    @classmethod
    def val_regularized_by(cls, v):
        if not (1 <= len(v) <= 50):
            raise ValueError("regularized_by must be 1-50 chars")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# GET /health
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


# ─────────────────────────────────────────────────────────────────────────────
# POST /employees
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


# ─────────────────────────────────────────────────────────────────────────────
# GET /employees
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    filt = {}
    if department:
        filt["department"] = department
    total = db.employees.count_documents(filt)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(filt, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /attendance/punch-in
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    now_ms = dt_to_ms(datetime.now(tz=UTC))
    raw_ms = body.punched_at if body.punched_at is not None else now_ms
    # Truncate to whole seconds
    punch_in_dt = ms_to_dt(raw_ms)

    attendance_date = compute_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")

    return serialize_attendance(doc)


# ─────────────────────────────────────────────────────────────────────────────
# POST /attendance/punch-out
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    now_ms = dt_to_ms(datetime.now(tz=UTC))
    raw_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_out_dt = ms_to_dt(raw_ms)

    # Find most recent record with punch_in <= punch_out_dt
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in record found")

    if record["punch_out"] is not None:
        raise HTTPException(409, "already punched out")

    punch_in_dt = record["punch_in"]
    # Validate time bounds: punch_out must be strictly after punch_in, and within 24 h
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (punch_out_dt - punch_in_dt) > timedelta(hours=24):
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    attendance_date = record["date"]
    overnight = is_overnight(emp["shift_start"], emp["shift_end"])
    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = compute_overtime_minutes_with_overnight(
        punch_out_dt, emp["shift_end"], attendance_date, overnight
    )
    half_day = compute_half_day(work_hours)

    update = {
        "$set": {
            "punch_out": punch_out_dt,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }
    }
    # Atomic: filter on _id and punch_out being null (concurrency safety)
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        update,
    )
    if result.matched_count == 0:
        raise HTTPException(409, "already punched out (concurrent request)")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


# ─────────────────────────────────────────────────────────────────────────────
# GET /attendance
# ─────────────────────────────────────────────────────────────────────────────
def _build_attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    filt = {}
    if emp_code:
        filt["emp_code"] = emp_code
    date_q = {}
    if date_from:
        date_q["$gte"] = date_from
    if date_to:
        date_q["$lte"] = date_to
    if date_q:
        filt["date"] = date_q
    if status:
        filt["status"] = status
    return filt


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    errors = []
    if date_from is not None:
        try:
            validate_date_str(date_from)
        except ValueError as e:
            errors.append({"loc": ["query", "date_from"], "msg": str(e), "type": "value_error"})
    if date_to is not None:
        try:
            validate_date_str(date_to)
        except ValueError as e:
            errors.append({"loc": ["query", "date_to"], "msg": str(e), "type": "value_error"})
    if date_from and date_to and not errors and date_from > date_to:
        errors.append({"loc": ["query", "date_from"], "msg": "date_from must be <= date_to", "type": "value_error"})
    if status is not None and status not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
        errors.append({"loc": ["query", "status"], "msg": "invalid status value", "type": "value_error"})
    if errors:
        raise HTTPException(422, detail=errors)

    filt = _build_attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(filt)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(filt, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /attendance/{emp_code}/{date}
# ─────────────────────────────────────────────────────────────────────────────
@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeRequest):
    # Validate path date
    try:
        validate_date_str(date_str)
    except ValueError as e:
        raise HTTPException(422, str(e))

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    # We retry on concurrent update conflict
    for attempt in range(3):
        result_doc = _apply_regularize(emp, record, body)
        if result_doc is not None:
            return serialize_attendance(result_doc)
        # Reload and retry
        record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
        if record is None:
            raise HTTPException(404, "attendance record not found")

    raise HTTPException(409, "concurrent modification conflict; please retry")


def _apply_regularize(emp: dict, record: dict, body: RegularizeRequest) -> Optional[dict]:
    """
    Build new state, validate, compute changes, perform compare-and-set update.
    Returns the updated doc on success, None on concurrent conflict.
    Raises HTTPException for validation errors.
    """
    old_status = record["status"]
    old_punch_in = record.get("punch_in")  # datetime or None
    old_punch_out = record.get("punch_out")  # datetime or None
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)
    old_history = record.get("history") or []

    # New state: start from old, apply supplied fields
    new_status = body.status if body.status is not None else old_status
    new_punch_in_dt: Optional[datetime] = old_punch_in
    new_punch_out_dt: Optional[datetime] = old_punch_out

    if body.punch_in is not None:
        new_punch_in_dt = ms_to_dt(body.punch_in)
    if body.punch_out is not None:
        new_punch_out_dt = ms_to_dt(body.punch_out)

    # Rule: ABSENT/LEAVE clears punch times
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE status cannot have punch_in or punch_out")
        new_punch_in_dt = None
        new_punch_out_dt = None
    else:
        # Presence status requires a punch_in
        if new_punch_in_dt is None:
            raise HTTPException(422, "presence status requires punch_in (supplied or stored)")

        # punch_in must produce the same attendance date
        pin_date = compute_attendance_date(new_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if pin_date != record["date"]:
            raise HTTPException(422, "punch_in would change the attendance date")

        # punch_out validation
        if new_punch_out_dt is not None:
            if new_punch_out_dt <= new_punch_in_dt:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_punch_out_dt - new_punch_in_dt) > timedelta(hours=24):
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    overnight = is_overnight(emp["shift_start"], emp["shift_end"])

    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in_dt, emp["shift_start"], record["date"])
        if new_punch_out_dt is not None:
            new_work_hours = compute_work_hours(new_punch_in_dt, new_punch_out_dt)
            new_overtime_minutes = compute_overtime_minutes_with_overnight(
                new_punch_out_dt, emp["shift_end"], record["date"], overnight
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Build changes dict: only fields that actually changed
    changes: dict = {}

    def _changed(old_val, new_val, field: str):
        if old_val != new_val:
            changes[field] = {"from": old_val, "to": new_val}

    _changed(old_status, new_status, "status")
    _changed(old_punch_in, new_punch_in_dt, "punch_in")
    _changed(old_punch_out, new_punch_out_dt, "punch_out")
    _changed(old_work_hours, new_work_hours, "work_hours")
    _changed(old_late_minutes, new_late_minutes, "late_minutes")
    _changed(old_overtime_minutes, new_overtime_minutes, "overtime_minutes")
    _changed(old_half_day, new_half_day, "half_day")

    if not changes:
        raise HTTPException(422, "no changes detected")

    now_dt = datetime.now(tz=UTC)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare-and-set: match on _id, current punch_in, punch_out, and history length
    history_len = len(old_history)
    update_filter = {
        "_id": record["_id"],
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
    }

    set_doc = {
        "status": new_status,
        "punch_in": new_punch_in_dt,
        "punch_out": new_punch_out_dt,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    result = db.attendance_logs.update_one(
        update_filter,
        {"$set": set_doc, "$push": {"history": history_entry}},
    )

    if result.matched_count == 0:
        return None  # signal retry

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return updated


# ─────────────────────────────────────────────────────────────────────────────
# Analytics helpers
# ─────────────────────────────────────────────────────────────────────────────
def _round_half_up_float(value: float, places: int) -> float:
    """Round a float half-up to given decimal places via Decimal."""
    return float(round_half_up(Decimal(str(value)), places))


def _round_half_up_from_fraction(numerator, denominator, places: int) -> Optional[float]:
    """Divide numerator by denominator and round half-up. Returns None if denominator is 0."""
    if denominator == 0:
        return None
    result = round_half_up(Decimal(str(numerator)) / Decimal(str(denominator)), places)
    return float(result)


# ─────────────────────────────────────────────────────────────────────────────
# GET /analytics/employees/{emp_code}/monthly
# ─────────────────────────────────────────────────────────────────────────────
def _build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last, next_first = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ec"]},
                            "date": {"$gte": first, "$lt": next_first},
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            # present_days: only Mon-Fri, weight 0.5 for half_day
                            "present_days_x2": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                {"$in": [
                                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                                    [2, 3, 4, 5, 6]  # Mon-Fri
                                                ]},
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                                1,   # 0.5 * 2
                                                2,   # 1.0 * 2
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "leave_days": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "total_overtime_minutes": {
                                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                            },
                        }
                    },
                ],
                "as": "log_agg",
            }
        },
        {"$unwind": {"path": "$log_agg", "preserveNullAndEmptyArrays": True}},
    ]
    return pipeline


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    first, last, next_first = month_bounds(month)

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    # Working days: max(month_start, joined_on) to month_end
    joined_on = emp["joined_on"]
    wd_start = max(first, joined_on)
    working_days = count_working_days(wd_start, last)

    # Run aggregation for log stats
    pipeline = _build_employee_monthly_pipeline(emp_code, month)
    rows = list(db.employees.aggregate(pipeline))
    if not rows:
        raise HTTPException(404, "employee not found")

    row = rows[0]
    agg = row.get("log_agg") or {}
    present_days_x2 = agg.get("present_days_x2", 0)
    present_days = present_days_x2 / 2
    leave_days = agg.get("leave_days", 0)
    late_count = agg.get("late_count", 0)
    total_late_minutes = agg.get("total_late_minutes", 0)
    total_overtime_minutes = agg.get("total_overtime_minutes", 0)

    # attendance_pct = present_days / working_days * 100, 2dp half-up
    if working_days == 0:
        attendance_pct = None
    else:
        # present_days = present_days_x2 / 2
        # pct = present_days_x2 / 2 / working_days * 100
        #     = present_days_x2 * 50 / working_days
        attendance_pct = _round_half_up_from_fraction(
            present_days_x2 * 50,
            working_days,
            2,
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /analytics/departments/summary
# ─────────────────────────────────────────────────────────────────────────────
def _build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    first, last, next_first = month_bounds(month)

    emp_match: dict = {"joined_on": {"$lte": last}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$ec"]},
                            "date": {"$gte": first, "$lt": next_first},
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days_x2": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                {"$in": [
                                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                                    [2, 3, 4, 5, 6]
                                                ]},
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                                1,
                                                2,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            # work_hours sum scaled by 10000 (4 decimal places) for half-up avg in Python
                            # Use $multiply only — no $round inside pipeline (it's half-even)
                            "wh_sum_x10000": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                            ]
                                        },
                                        {"$multiply": ["$work_hours", 10000]},
                                        0,
                                    ]
                                }
                            },
                            "wh_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "log_agg",
            }
        },
        {"$unwind": {"path": "$log_agg", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days_x2": {"$sum": {"$ifNull": ["$log_agg.present_days_x2", 0]}},
                "wh_sum_x10000": {"$sum": {"$ifNull": ["$log_agg.wh_sum_x10000", 0]}},
                "wh_count": {"$sum": {"$ifNull": ["$log_agg.wh_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$log_agg.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$log_agg.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$log_agg.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$log_agg.on_duty_count", 0]}},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = _build_department_summary_pipeline(month, department)
    rows = list(db.employees.aggregate(pipeline))

    items = []
    for row in rows:
        dept = row["_id"]
        headcount = row["headcount"]
        if headcount == 0:
            continue  # omit zero-headcount departments
        present_days = row["present_days_x2"] / 2
        wh_sum_x10000 = row["wh_sum_x10000"]
        wh_count = row["wh_count"]
        # avg_work_hours = sum(work_hours) / count, rounded half-up to 2 dp
        # sum is stored as sum_of(work_hours * 10000); so avg = sum / count / 10000
        avg_work_hours = (
            _round_half_up_from_fraction(int(round(wh_sum_x10000)), wh_count * 10000, 2)
            if wh_count > 0
            else None
        )

        items.append({
            "department": dept,
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_work_hours,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })

    return {"month": month, "items": items}


# ─────────────────────────────────────────────────────────────────────────────
# GET /analytics/leaderboard/late
# ─────────────────────────────────────────────────────────────────────────────
def _build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    first, last, next_first = month_bounds(month)

    pipeline: list = [
        {
            "$match": {
                "date": {"$gte": first, "$lt": next_first},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},  # drops logs for non-existent employees
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = _build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


# ─────────────────────────────────────────────────────────────────────────────
# GET /analytics/departments/{department}/trend
# ─────────────────────────────────────────────────────────────────────────────
def _department_emp_codes(department: str) -> list:
    """Resolve the department's emp_code list (also serves as the 404 check)."""
    return [
        d["emp_code"]
        for d in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})
    ]


def _build_department_trend_pipeline(
    department: str, from_str: str, to_str: str, emp_codes: Optional[list] = None
) -> list:
    from_date = date.fromisoformat(from_str)
    to_date = date.fromisoformat(to_str)

    if emp_codes is None:
        emp_codes = _department_emp_codes(department)

    # densify bounds must be literal datetime objects (UTC midnight for each date)
    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=UTC)
    to_plus1_dt = datetime(
        (to_date + timedelta(days=1)).year,
        (to_date + timedelta(days=1)).month,
        (to_date + timedelta(days=1)).day,
        tzinfo=UTC,
    )

    # Pipeline runs on attendance_logs collection
    pipeline: list = [
        # Step 1: match this department's logs in the date range
        # (emp_code list resolved up front; $in rides the unique {emp_code, date} index)
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        # Group by date string
        {
            "$group": {
                "_id": "$date",
                "present_count_x2": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    1,  # 0.5 * 2
                                    2,  # 1.0 * 2
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        # Convert date string to UTC midnight datetime for $densify
        {
            "$addFields": {
                "date_dt": {
                    "$dateFromString": {
                        "dateString": "$_id",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
            }
        },
        # Densify: fill every missing day in the range
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_plus1_dt],
                },
            }
        },
        # After densify, fill null counts for gap-filled rows with 0
        {
            "$addFields": {
                "present_count_x2": {"$ifNull": ["$present_count_x2", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        # Rebuild date string from date_dt (UTC midnight → YYYY-MM-DD)
        {
            "$addFields": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt", "timezone": "UTC"}
                }
            }
        },
        # is_working_day: $dayOfWeek 1=Sun, 2=Mon…6=Fri, 7=Sat
        {
            "$addFields": {
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]
                }
            }
        },
        # Headcount: count employees in this department with joined_on <= date
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "hc_result",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$hc_result.n", 0]}, 0]
                }
            }
        },
        # present_count = present_count_x2 / 2
        {
            "$addFields": {
                "present_count": {"$divide": ["$present_count_x2", 2]},
            }
        },
        # attendance_rate_raw: ratio for working days with headcount > 0 (finalized in Python)
        {
            "$addFields": {
                "attendance_rate_raw": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count_x2", {"$multiply": ["$headcount", 2]}]},
                        None,
                    ]
                }
            }
        },
        # Sort by date ascending (required for correct ordering)
        {"$sort": {"date_dt": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate_raw": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    # Validate dates
    try:
        validate_date_str(from_)
        validate_date_str(to)
    except ValueError as e:
        raise HTTPException(422, str(e))

    from_date = date.fromisoformat(from_)
    to_date = date.fromisoformat(to)

    if to_date < from_date:
        raise HTTPException(422, "to must be >= from")

    day_count = (to_date - from_date).days + 1
    if day_count > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    # 404 if no employees in this department
    emp_codes = _department_emp_codes(department)
    if not emp_codes:
        raise HTTPException(404, "department not found")

    pipeline = _build_department_trend_pipeline(department, from_, to, emp_codes)
    rows = list(db.attendance_logs.aggregate(pipeline))

    # First pass: round attendance_rate for each row (at most 92 rows)
    rounded_rates = []
    for row in rows:
        rate_raw = row.get("attendance_rate_raw")
        if rate_raw is not None:
            rounded_rates.append(float(round_half_up(Decimal(str(rate_raw)), 4)))
        else:
            rounded_rates.append(None)

    items = []
    for i, row in enumerate(rows):
        attendance_rate = rounded_rates[i]

        # moving_avg_7d: mean of non-null rounded attendance_rate in the [-6, 0] window
        # (rows before `from` are not in the result, so early rows use a shorter window)
        window_rates = [r for r in rounded_rates[max(0, i - 6): i + 1] if r is not None]
        if window_rates:
            avg_dec = Decimal(sum(Decimal(str(r)) for r in window_rates)) / Decimal(len(window_rates))
            moving_avg_7d = float(round_half_up(avg_dec, 4))
        else:
            moving_avg_7d = None

        items.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": row["present_count"],
            "late_count": row["late_count"],
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })

    return {"department": department, "items": items}


# ─────────────────────────────────────────────────────────────────────────────
# GET /admin/explain/{endpoint}
# ─────────────────────────────────────────────────────────────────────────────
_VALID_EXPLAIN_ENDPOINTS = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint not in _VALID_EXPLAIN_ENDPOINTS:
        raise HTTPException(422, f"endpoint must be one of {sorted(_VALID_EXPLAIN_ENDPOINTS)}")

    try:
        if endpoint == "attendance_list":
            if date_from:
                validate_date_str(date_from)
            if date_to:
                validate_date_str(date_to)
            if date_from and date_to and date_from > date_to:
                raise HTTPException(422, "date_from must be <= date_to")
            filt = _build_attendance_list_query(emp_code, date_from, date_to, status)
            skip = (page - 1) * page_size
            explain_cmd = {
                "explain": {
                    "find": "attendance_logs",
                    "filter": filt,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": skip,
                    "limit": page_size,
                },
                "verbosity": "executionStats",
            }
            collection_name = "attendance_logs"
            raw = db.command(explain_cmd)

        elif endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(422, "emp_code is required for employee_monthly")
            if not month:
                raise HTTPException(422, "month is required for employee_monthly")
            validate_month_str(month)
            pipeline = _build_employee_monthly_pipeline(emp_code, month)
            explain_cmd = {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            collection_name = "employees"
            raw = db.command(explain_cmd)

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(422, "month is required for department_summary")
            validate_month_str(month)
            pipeline = _build_department_summary_pipeline(month, department)
            explain_cmd = {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            collection_name = "employees"
            raw = db.command(explain_cmd)

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(422, "month is required for late_leaderboard")
            validate_month_str(month)
            pipeline = _build_late_leaderboard_pipeline(month, limit, department)
            explain_cmd = {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            collection_name = "attendance_logs"
            raw = db.command(explain_cmd)

        elif endpoint == "department_trend":
            if not department:
                raise HTTPException(422, "department is required for department_trend")
            if not from_:
                raise HTTPException(422, "from is required for department_trend")
            if not to:
                raise HTTPException(422, "to is required for department_trend")
            validate_date_str(from_)
            validate_date_str(to)
            from_date = date.fromisoformat(from_)
            to_date = date.fromisoformat(to)
            if to_date < from_date:
                raise HTTPException(422, "to must be >= from")
            if (to_date - from_date).days + 1 > 92:
                raise HTTPException(422, "date range cannot exceed 92 days")
            pipeline = _build_department_trend_pipeline(department, from_, to)
            explain_cmd = {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            collection_name = "attendance_logs"
            raw = db.command(explain_cmd)

        else:
            raise HTTPException(422, "unknown endpoint")

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"explain failed: {exc}") from exc

    # Make JSON-safe
    safe = json.loads(bson.json_util.dumps(raw))
    return {"endpoint": endpoint, "collection": collection_name, "explain": safe}
