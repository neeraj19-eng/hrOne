"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import os
from datetime import datetime, timedelta, timezone, date as date_cls
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from bson.timestamp import Timestamp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set in the environment")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

COLL_EMPLOYEES = "employees"
COLL_LOGS = "attendance_logs"

# --------------------------------------------------------------------------- #
# App & database
# --------------------------------------------------------------------------- #
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def _create_indexes() -> None:
    """Idempotent index creation. Called once on startup."""
    db[COLL_EMPLOYEES].create_index(
        [("emp_code", ASCENDING)], unique=True, name="emp_code_unique"
    )
    db[COLL_EMPLOYEES].create_index(
        [("department", ASCENDING)], name="dept_idx"
    )

    db[COLL_LOGS].create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True, name="emp_date_unique",
    )
    db[COLL_LOGS].create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_idx"
    )
    db[COLL_LOGS].create_index(
        [("emp_code", ASCENDING), ("punch_out", ASCENDING), ("punch_in", DESCENDING)],
        name="emp_open_punchin_idx",
    )
    db[COLL_LOGS].create_index([("date", ASCENDING)], name="date_idx")


# --------------------------------------------------------------------------- #
# Time / numeric utilities
# --------------------------------------------------------------------------- #
def _round_half_up(value: float, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _truncate_sec(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def _ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _to_ist(dt: datetime) -> datetime:
    return _ensure_aware(dt).astimezone(IST)


def _hhmm(s: str) -> Tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _is_overnight(shift_start: str, shift_end: str) -> bool:
    return _hhmm(shift_end) <= _hhmm(shift_start)


def _parse_iso_date(s: str) -> Optional[date_cls]:
    """Return a date if s is exactly YYYY-MM-DD, otherwise None."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if len(s) != 10:
        return None
    try:
        return date_cls.fromisoformat(s)
    except ValueError:
        return None


def _month_bounds(month: str) -> Tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    first = date_cls(y, m, 1)
    nxt = date_cls(y + 1, 1, 1) if m == 12 else date_cls(y, m + 1, 1)
    last = nxt - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _count_weekdays(start: date_cls, end: date_cls) -> int:
    if end < start:
        return 0
    total, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            total += 1
        d += timedelta(days=1)
    return total


# --------------------------------------------------------------------------- #
# Attendance-day and derived-value computation (R1-R5)
# --------------------------------------------------------------------------- #
def _attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = _to_ist(punch_in)
    if _is_overnight(shift_start, shift_end):
        eh, em = _hhmm(shift_end)
        if (local.hour, local.minute) < (eh, em):
            local = local - timedelta(days=1)
    return local.date().isoformat()


def _late_minutes(punch_in: datetime, shift_start: str) -> int:
    local = _to_ist(punch_in)
    sh, sm = _hhmm(shift_start)
    start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    seconds = (local - start).total_seconds()
    return 0 if seconds <= 600 else int(seconds // 60)


def _shift_end_instant(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    eh, em = _hhmm(shift_end)
    d = date_cls.fromisoformat(attendance_date)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if _is_overnight(shift_start, shift_end):
        end = end + timedelta(days=1)
    return end


def _overtime_minutes(punch_out: datetime,
                      attendance_date: str,
                      shift_start: str,
                      shift_end: str) -> int:
    end = _shift_end_instant(attendance_date, shift_start, shift_end)
    mins = int((_to_ist(punch_out) - end).total_seconds() // 60)
    return mins if mins >= 30 else 0


def _work_hours(punch_in: datetime, punch_out: datetime) -> float:
    secs = (punch_out - punch_in).total_seconds()
    return float((Decimal(secs) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    ))


def _is_half_day(work_hours: Optional[float]) -> bool:
    return work_hours is not None and work_hours < 4.50


def _derive(status: str,
            punch_in: Optional[datetime],
            punch_out: Optional[datetime],
            attendance_date: Optional[str],
            shift_start: str,
            shift_end: str) -> Dict[str, Any]:
    """Recompute late_minutes / work_hours / overtime_minutes / half_day."""
    if status in ("ABSENT", "LEAVE") or punch_in is None:
        return {"late_minutes": 0, "work_hours": None,
                "overtime_minutes": 0, "half_day": False}
    late = _late_minutes(punch_in, shift_start)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None,
                "overtime_minutes": 0, "half_day": False}
    wh = _work_hours(punch_in, punch_out)
    ot = _overtime_minutes(punch_out, attendance_date, shift_start, shift_end)
    return {"late_minutes": late, "work_hours": wh,
            "overtime_minutes": ot, "half_day": _is_half_day(wh)}


# --------------------------------------------------------------------------- #
# BSON -> JSON
# --------------------------------------------------------------------------- #
def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Timestamp):
        return {"time": obj.time, "inc": obj.inc}
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return _to_ms(obj)
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return obj.hex()
    if isinstance(obj, bytearray):
        return _jsonable(bytes(obj))
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)


# --------------------------------------------------------------------------- #
# Response builders
# --------------------------------------------------------------------------- #
def _employee_view(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _to_ms(doc["created_at"]),
    }


def _history_view(entry: Dict[str, Any]) -> Dict[str, Any]:
    raw = entry.get("changes", {}) or {}
    out: Dict[str, Any] = {}
    for key, val in raw.items():
        if key in ("punch_in", "punch_out"):
            out[key] = {"from": _to_ms(val.get("from")), "to": _to_ms(val.get("to"))}
        else:
            out[key] = {"from": val.get("from"), "to": val.get("to")}
    return {
        "at": _to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": out,
    }


def _attendance_view(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _to_ms(doc.get("punch_in")),
        "punch_out": _to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [_history_view(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
EpochMs = Field(..., ge=EPOCH_MIN, le=EPOCH_MAX)
EpochMsOpt = Field(None, ge=EPOCH_MIN, le=EPOCH_MAX)


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def _valid(cls, v: str) -> str:
        date_cls.fromisoformat(v)
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = EpochMsOpt
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def _presence(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = EpochMsOpt


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = EpochMsOpt
    punch_out: Optional[int] = EpochMsOpt
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def _status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ALL_STATUSES:
            raise ValueError("invalid status")
        return v


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> Dict[str, str]:
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> Dict[str, Any]:
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db[COLL_EMPLOYEES].insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return _employee_view(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db[COLL_EMPLOYEES].count_documents(q)
    items = list(
        db[COLL_EMPLOYEES].find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [_employee_view(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Punch-in / punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> Dict[str, Any]:
    emp = db[COLL_EMPLOYEES].find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = _truncate_sec(_from_ms(body.punched_at)) if body.punched_at \
        else _truncate_sec(datetime.now(UTC))

    att_date = _attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = _late_minutes(ts, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db[COLL_LOGS].insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return _attendance_view(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> Dict[str, Any]:
    emp = db[COLL_EMPLOYEES].find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = _truncate_sec(_from_ms(body.punched_at)) if body.punched_at \
        else _truncate_sec(datetime.now(UTC))

    rec = db[COLL_LOGS].find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no open punch-in found")

    pin = _ensure_aware(rec["punch_in"])
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punched_at more than 24h after punch_in")

    derived = _derive(rec["status"], pin, ts, rec["date"],
                      emp["shift_start"], emp["shift_end"])

    updated = db[COLL_LOGS].find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": derived["work_hours"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return _attendance_view(updated)


# --------------------------------------------------------------------------- #
# Attendance list
# --------------------------------------------------------------------------- #
def _attendance_filter(emp_code: Optional[str],
                       date_from: Optional[str],
                       date_to: Optional[str],
                       status: Optional[str]) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        rng: Dict[str, str] = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        q["date"] = rng
    if status is not None:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    if date_from is not None:
        date_from = date_from.strip()
        if _parse_iso_date(date_from) is None:
            raise HTTPException(422, "invalid date_from")
    if date_to is not None:
        date_to = date_to.strip()
        if _parse_iso_date(date_to) is None:
            raise HTTPException(422, "invalid date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")

    q = _attendance_filter(emp_code, date_from, date_to, status)
    total = db[COLL_LOGS].count_documents(q)
    items = list(
        db[COLL_LOGS].find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [_attendance_view(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Regularize (PATCH)
# --------------------------------------------------------------------------- #
def _compute_changes(rec: Dict[str, Any],
                     new: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    changes: Dict[str, Dict[str, Any]] = {}

    if new["status"] != rec["status"]:
        changes["status"] = {"from": rec["status"], "to": new["status"]}

    for field in ("punch_in", "punch_out"):
        old = _ensure_aware(rec.get(field))
        new_val = _ensure_aware(new.get(field))
        if old != new_val:
            changes[field] = {"from": rec.get(field), "to": new.get(field)}

    for field in ("late_minutes", "overtime_minutes"):
        old = rec.get(field, 0)
        if new[field] != old:
            changes[field] = {"from": old, "to": new[field]}

    old_wh = rec.get("work_hours")
    if new["work_hours"] != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new["work_hours"]}

    old_hd = rec.get("half_day", False)
    if new["half_day"] != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new["half_day"]}

    return changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn) -> Dict[str, Any]:
    if _parse_iso_date(date) is None:
        raise HTTPException(422, "invalid date")

    emp = db[COLL_EMPLOYEES].find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    rec = db[COLL_LOGS].find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    new_status = body.status if body.status is not None else rec["status"]
    new_pin = rec.get("punch_in")
    new_pout = rec.get("punch_out")

    if body.punch_in is not None:
        new_pin = _truncate_sec(_from_ms(body.punch_in))
    if body.punch_out is not None:
        new_pout = _truncate_sec(_from_ms(body.punch_out))

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_pin, new_pout = None, None
    elif new_pin is None:
        raise HTTPException(422, "presence status requires punch_in")

    if new_pin is not None:
        pin_date = _attendance_date(new_pin, emp["shift_start"], emp["shift_end"])
        if pin_date != date:
            raise HTTPException(422, "punch_in must fall on the record's attendance date")

    if new_pin is not None and new_pout is not None:
        if new_pout <= new_pin:
            raise HTTPException(422, "punch_out must be after punch_in")
        if new_pout - new_pin > timedelta(hours=24):
            raise HTTPException(422, "punch_out more than 24h after punch_in")

    derived = _derive(new_status, new_pin, new_pout, date,
                      emp["shift_start"], emp["shift_end"])

    new_state = {
        "status": new_status,
        "punch_in": new_pin,
        "punch_out": new_pout,
        **derived,
    }
    changes = _compute_changes(rec, new_state)
    if not changes:
        raise HTTPException(422, "no changes")

    hist_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    res = db[COLL_LOGS].update_one(
        {"_id": rec["_id"], "history": rec.get("history", [])},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pin,
                "punch_out": new_pout,
                "late_minutes": derived["late_minutes"],
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            },
            "$push": {"history": hist_entry},
        },
    )
    if res.modified_count == 0:
        raise HTTPException(409, "concurrent modification, retry")

    updated = db[COLL_LOGS].find_one({"_id": rec["_id"]})
    return _attendance_view(updated)


# --------------------------------------------------------------------------- #
# Aggregation builders
# --------------------------------------------------------------------------- #
def _pipeline_employee_monthly(emp_code: str, month: str) -> List[Dict[str, Any]]:
    first, last = _month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$addFields": {"_d": {"$dateFromString": {"dateString": "$date"}}}},
        {"$addFields": {"_dow": {"$dayOfWeek": "$_d"}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$lte": ["$_dow", 6]},
                ]},
                {"$cond": ["$half_day", 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


def _pipeline_department_summary(month: str, department: Optional[str]) -> List[Dict[str, Any]]:
    first, last = _month_bounds(month)
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": last}}
    if department is not None:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"},
        }},
        {"$lookup": {
            "from": COLL_LOGS,
            "let": {"codes": "$emp_codes"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$in": ["$emp_code", "$$codes"]},
                    {"$gte": ["$date", first]},
                    {"$lte": ["$date", last]},
                ]}}},
                {"$addFields": {"_d": {"$dateFromString": {"dateString": "$date"}}}},
                {"$addFields": {"_dow": {"$dayOfWeek": "$_d"}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$lte": ["$_dow", 6]},
                        ]},
                        {"$cond": ["$half_day", 0.5, 1]},
                        0,
                    ]}},
                    "wh_sum": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$work_hours", None]},
                        ]},
                        "$work_hours", 0,
                    ]}},
                    "wh_count": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$work_hours", None]},
                        ]},
                        1, 0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "agg",
        }},
        {"$addFields": {"agg": {"$ifNull": [{"$arrayElemAt": ["$agg", 0]}, {}]}}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": {"$ifNull": ["$agg.present_days", 0]},
            "wh_sum": {"$ifNull": ["$agg.wh_sum", 0]},
            "wh_count": {"$ifNull": ["$agg.wh_count", 0]},
            "late_count": {"$ifNull": ["$agg.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$agg.total_late_minutes", 0]},
            "leave_count": {"$ifNull": ["$agg.leave_count", 0]},
            "on_duty_count": {"$ifNull": ["$agg.on_duty_count", 0]},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


def _pipeline_leaderboard(month: str, limit: int, department: Optional[str]) -> List[Dict[str, Any]]:
    first, last = _month_bounds(month)
    stages: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": COLL_EMPLOYEES,
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        stages.append({"$match": {"emp.department": department}})
    stages += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return stages


def _pipeline_trend(department: str, date_from: str, date_to: str) -> List[Dict[str, Any]]:
    start_dt = datetime.fromisoformat(date_from).replace(tzinfo=UTC)
    end_dt = datetime.fromisoformat(date_to).replace(tzinfo=UTC)
    return [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
        {"$lookup": {
            "from": COLL_EMPLOYEES,
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": ["$half_day", 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$addFields": {"_d": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {
            "field": "_d",
            "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]},
        }},
        {"$addFields": {
            "_dateStr": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_d"}},
            "_dow": {"$dayOfWeek": "$_d"},
        }},
        {"$lookup": {
            "from": COLL_EMPLOYEES,
            "pipeline": [
                {"$match": {"department": department}},
                {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}},
            ],
            "as": "dept_emps",
        }},
        {"$addFields": {
            "headcount": {"$size": {"$filter": {
                "input": "$dept_emps",
                "as": "e",
                "cond": {"$lte": ["$$e.joined_on", "$_dateStr"]},
            }}},
            "is_working_day": {"$lte": ["$_dow", 6]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"_d": 1},
            "output": {"moving_avg_7d_raw": {
                "$avg": "$attendance_rate",
                "window": {"documents": [-6, 0]},
            }},
        }},
        {"$sort": {"_d": 1}},
        {"$project": {
            "_id": 0,
            "date": "$_dateStr",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": "$moving_avg_7d_raw",
        }},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
) -> Dict[str, Any]:
    emp = db[COLL_EMPLOYEES].find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    first, last = _month_bounds(month)
    joined = date_cls.fromisoformat(emp["joined_on"])
    start = max(joined, date_cls.fromisoformat(first))
    working_days = _count_weekdays(start, date_cls.fromisoformat(last))

    rows = list(db[COLL_LOGS].aggregate(_pipeline_employee_monthly(emp_code, month)))
    row = rows[0] if rows else {}

    present_days = float(row.get("present_days", 0))
    pct = None if working_days == 0 else _round_half_up(
        present_days / working_days * 100.0, 2
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> Dict[str, Any]:
    rows = list(db[COLL_EMPLOYEES].aggregate(
        _pipeline_department_summary(month, department)
    ))
    items = []
    for r in rows:
        wh_sum = float(r.get("wh_sum", 0) or 0)
        wh_count = int(r.get("wh_count", 0) or 0)
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r.get("present_days", 0)),
            "avg_work_hours": None if wh_count == 0 else _round_half_up(wh_sum / wh_count, 2),
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> Dict[str, Any]:
    rows = list(db[COLL_LOGS].aggregate(
        _pipeline_leaderboard(month, limit, department)
    ))
    return {
        "month": month,
        "items": [{
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        } for r in rows],
    }


def _validate_trend_range(from_: str, to: str) -> Tuple[date_cls, date_cls]:
    d1 = _parse_iso_date(from_)
    d2 = _parse_iso_date(to)
    if d1 is None or d2 is None:
        raise HTTPException(422, "invalid date format; use YYYY-MM-DD")
    if d2 < d1:
        raise HTTPException(422, "to must be >= from")
    if (d2 - d1).days > 91:
        raise HTTPException(422, "range exceeds 92 days")
    return d1, d2


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
) -> Dict[str, Any]:
    d1, d2 = _validate_trend_range(from_, to)

    if db[COLL_EMPLOYEES].count_documents({"department": department}) == 0:
        raise HTTPException(404, "unknown department")

    rows = list(db[COLL_LOGS].aggregate(
        _pipeline_trend(department, d1.isoformat(), d2.isoformat())
    ))

    items = []
    for r in rows:
        ar = r.get("attendance_rate")
        ma = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": None if ar is None else _round_half_up(ar, 4),
            "moving_avg_7d": None if ma is None else _round_half_up(ma, 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def _explain_find(coll: str, q: Dict[str, Any], sort, skip: int, limit: int) -> Dict[str, Any]:
    cur = db[coll].find(q).sort(sort).skip(skip).limit(limit)
    return cur.explain()


def _explain_aggregate(coll: str, pipeline: List[Dict[str, Any]]) -> Dict[str, Any]:
    return db.command(
        "explain",
        {"aggregate": coll, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    if endpoint == "attendance_list":
        q = _attendance_filter(emp_code, date_from, date_to, status)
        plan = _explain_find(
            COLL_LOGS, q,
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            (page - 1) * page_size, page_size,
        )
        return {"endpoint": endpoint, "collection": COLL_LOGS, "explain": _jsonable(plan)}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        plan = _explain_aggregate(
            COLL_LOGS, _pipeline_employee_monthly(emp_code, month)
        )
        return {"endpoint": endpoint, "collection": COLL_LOGS, "explain": _jsonable(plan)}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        plan = _explain_aggregate(
            COLL_EMPLOYEES, _pipeline_department_summary(month, department)
        )
        return {"endpoint": endpoint, "collection": COLL_EMPLOYEES, "explain": _jsonable(plan)}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        plan = _explain_aggregate(
            COLL_LOGS, _pipeline_leaderboard(month, limit, department)
        )
        return {"endpoint": endpoint, "collection": COLL_LOGS, "explain": _jsonable(plan)}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, to required")
        d1, d2 = _validate_trend_range(from_, to)
        plan = _explain_aggregate(
            COLL_LOGS, _pipeline_trend(department, d1.isoformat(), d2.isoformat())
        )
        return {"endpoint": endpoint, "collection": COLL_LOGS, "explain": _jsonable(plan)}

    raise HTTPException(422, "unknown endpoint")