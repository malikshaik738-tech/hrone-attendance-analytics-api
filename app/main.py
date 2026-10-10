"""Employee Attendance & Analytics API (HROne assignment).

Configuration: MONGO_URI and MONGO_DB; environment variables override .env.
Run from project root: uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000, tz_aware=False)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- Validation ---------------------------------
def valid_epoch_ms(value: Any) -> int:
    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:
        raise ValueError("must be an epoch-millisecond integer in the supported range")
    return value


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def epoch_to_utc(value: int) -> datetime:
    valid_epoch_ms(value)
    # Truncate the instant to whole seconds before storing or calculating.
    return datetime.fromtimestamp(value // 1000, tz=UTC).replace(tzinfo=None)


def utc_to_epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.replace(microsecond=0)
    return int(value.timestamp()) * 1000


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_ist(value: datetime) -> datetime:
    return as_utc(value).astimezone(IST)


def now_utc_naive() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_datetime(attendance_date: str, shift: str, overnight_next_day: bool = False) -> datetime:
    day = parse_date(attendance_date)
    hh, mm = map(int, shift.split(":"))
    if overnight_next_day:
        day += timedelta(days=1)
    return datetime.combine(day, time(hh, mm), tzinfo=IST)


def attendance_date_for(punch_in: datetime, employee: dict) -> str:
    local = to_ist(punch_in)
    start, end = employee["shift_start"], employee["shift_end"]
    overnight = end <= start
    chosen = local.date()
    if overnight and local.strftime("%H:%M") < end:
        chosen -= timedelta(days=1)
    return chosen.isoformat()


def compute_late_minutes(punch_in: Optional[datetime], shift_start: str, day: str) -> int:
    if punch_in is None:
        return 0
    start = shift_datetime(day, shift_start)
    seconds = (as_utc(punch_in) - start.astimezone(UTC)).total_seconds()
    if seconds <= 600:
        return 0
    return max(0, int(seconds // 60))


def compute_overtime(punch_out: Optional[datetime], shift_end: str, day: str, shift_start: str) -> int:
    if punch_out is None:
        return 0
    overnight = shift_end <= shift_start
    end = shift_datetime(day, shift_end, overnight_next_day=overnight)
    seconds = (as_utc(punch_out) - end.astimezone(UTC)).total_seconds()
    minutes = int(seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_derived(emp: dict, day: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    late = compute_late_minutes(punch_in, emp["shift_start"], day)
    if punch_in is None or punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
    hours = round_half_up(Decimal(int(seconds)) / Decimal(3600), 2)
    return {
        "late_minutes": late,
        "work_hours": hours,
        "overtime_minutes": compute_overtime(punch_out, emp["shift_end"], day, emp["shift_start"]),
        "half_day": hours < 4.50,
    }


def clean_employee(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "_id"}


def clean_attendance(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k not in {"_id", "id"}}
    for field in ("punch_in", "punch_out"):
        out[field] = utc_to_epoch(out.get(field))
    history = []
    for entry in out.get("history", []):
        item = dict(entry)
        item["at"] = utc_to_epoch(item.get("at"))
        changes = {}
        for key, change in item.get("changes", {}).items():
            c = dict(change)
            if key in {"punch_in", "punch_out"}:
                c["from"] = utc_to_epoch(c.get("from"))
                c["to"] = utc_to_epoch(c.get("to"))
            changes[key] = c
        item["changes"] = changes
        history.append(item)
    out["history"] = history
    return out


def init_indexes() -> None:
    """Create indexes idempotently. Unique natural keys make concurrent writes safe."""
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_code")
    attendance.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_code_date")
    attendance.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_status_date_code")
    attendance.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_punch_in")


@app.on_event("startup")
def startup() -> None:
    try:
        init_indexes()
    except PyMongoError:
        # Health remains the authoritative readiness signal; requests will report DB failures normally.
        pass


# --------------------------------- Models -----------------------------------
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_date_valid(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def epoch_valid(cls, value):
        if value is not None:
            valid_epoch_ms(value)
        return value


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def epoch_valid(cls, value):
        if value is not None:
            valid_epoch_ms(value)
        return value


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def epoch_valid(cls, value):
        if value is not None:
            valid_epoch_ms(value)
        return value


# --------------------------------- Helpers ----------------------------------
def page_args(page: int, page_size: int) -> None:
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100")


def month_bounds(month: str) -> tuple[str, str, date, date]:
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat(), start, end


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def presence_fraction(record: dict) -> float:
    if record.get("status") not in PRESENCE:
        return 0.0
    return 0.5 if record.get("half_day") else 1.0


def response_round(value: Optional[float], places: int = 2) -> Optional[float]:
    return None if value is None else round_half_up(value, places)


def attendance_filter(emp_code=None, date_from=None, date_to=None, status=None) -> dict:
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


def attendance_list_pipeline(q: dict, page: int, page_size: int) -> list[dict]:
    return [{"$match": q}, {"$sort": {"date": -1, "emp_code": 1}}, {"$facet": {
        "items": [{"$skip": (page - 1) * page_size}, {"$limit": page_size}, {"$project": {"_id": 0}}],
        "total": [{"$count": "count"}],
    }}]


def month_logs_match(start_s: str, end_s: str, emp_code: Optional[str] = None) -> dict:
    q: dict[str, Any] = {"date": {"$gte": start_s, "$lte": end_s}}
    if emp_code:
        q["emp_code"] = emp_code
    return q


# -------------------------------- Endpoints ---------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_naive()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return {**clean_employee(doc), "created_at": utc_to_epoch(doc["created_at"])}


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    page_args(page, page_size)
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    docs = list(employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    for doc in docs:
        doc["created_at"] = utc_to_epoch(doc.get("created_at"))
    return {"items": docs, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = epoch_to_utc(body.punched_at) if body.punched_at is not None else now_utc_naive()
    day = attendance_date_for(ts, emp)
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return clean_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = epoch_to_utc(body.punched_at) if body.punched_at is not None else now_utc_naive()
    current = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if current is None:
        raise HTTPException(404, "no punch-in found")
    if current.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    seconds = (as_utc(ts) - as_utc(current["punch_in"])).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    derived = compute_derived(emp, current["date"], current["punch_in"], ts)
    updated = attendance.find_one_and_update(
        {"_id": current["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}}, return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return clean_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    page_args(page, page_size)
    try:
        for d in (date_from, date_to):
            if d is not None:
                parse_date(d)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    q = attendance_filter(emp_code, date_from, date_to, status)
    # A single aggregation applies filters, ordering, pagination and total in MongoDB.
    result = list(attendance.aggregate(attendance_list_pipeline(q, page, page_size)))
    facet = result[0] if result else {"items": [], "total": []}
    items = [clean_attendance(doc) for doc in facet.get("items", [])]
    total = facet.get("total", [{}])[0].get("count", 0) if facet.get("total") else 0
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    if (("punch_in" in body.model_fields_set and body.punch_in is None)
            or ("punch_out" in body.model_fields_set and body.punch_out is None)):
        raise HTTPException(422, "punch_in and punch_out must be epoch-millisecond integers when supplied")
    try:
        parse_date(date)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = attendance.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "attendance record not found")

    status = body.status if body.status is not None else old["status"]
    pin = epoch_to_utc(body.punch_in) if body.punch_in is not None else old.get("punch_in")
    pout = epoch_to_utc(body.punch_out) if body.punch_out is not None else old.get("punch_out")
    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot be supplied with punch times")
        pin = pout = None
    elif pin is None:
        raise HTTPException(422, "presence status requires punch_in")
    if pin is not None and attendance_date_for(pin, emp) != date:
        raise HTTPException(422, "punch_in must belong to the record attendance date")
    if pout is not None:
        if pin is None:
            raise HTTPException(422, "punch_out requires punch_in")
        secs = (as_utc(pout) - as_utc(pin)).total_seconds()
        if secs <= 0 or secs > 24 * 3600:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    derived = compute_derived(emp, date, pin, pout)
    new_fields = {"status": status, "punch_in": pin, "punch_out": pout, **derived}
    # Legacy documents may omit fields whose documented defaults are 0/false/[].
    # Compare against those effective defaults so the audit trail records real changes only.
    old_fields = {key: old.get(key) for key in new_fields}
    old_fields["late_minutes"] = old.get("late_minutes", 0)
    old_fields["overtime_minutes"] = old.get("overtime_minutes", 0)
    old_fields["half_day"] = old.get("half_day", False)
    changed = {key: {"from": old_fields[key], "to": new_fields[key]} for key in new_fields if old_fields[key] != new_fields[key]}
    if not changed:
        raise HTTPException(422, "request does not change the record")
    history_changes = {}
    for key, values in changed.items():
        # Store instants as BSON datetimes; clean_attendance serializes them as epoch milliseconds.
        history_changes[key] = dict(values)
    entry = {"at": now_utc_naive(), "by": body.regularized_by, "reason": body.reason, "changes": history_changes}
    # Optimistic compare-and-set: concurrent edits cannot overwrite each other's history.
    filter_doc = {"_id": old["_id"]}
    # A legacy record can have no history field at all; match that state explicitly.
    if "history" in old:
        filter_doc["history"] = old["history"]
    else:
        filter_doc["history"] = {"$exists": False}
    for key in ("status", "punch_in", "punch_out"):
        if key in old:
            filter_doc[key] = old[key]
        else:
            filter_doc[key] = {"$exists": False}
    updated = attendance.find_one_and_update(filter_doc, {"$set": new_fields, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return clean_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    start_s, end_s, start, end = month_bounds(month)
    # MongoDB performs the record aggregation; Python only supplies calendar weekdays and response formatting.
    pipeline = [
        {"$match": month_logs_match(start_s, end_s, emp_code)},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"}, "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }}
    ]
    agg = list(attendance.aggregate(pipeline))
    values = agg[0] if agg else {}
    first = max(start, parse_date(emp["joined_on"]))
    wd = working_days(first, end) if first <= end else 0
    # Present days count only weekday records, per R7.
    weekday_pipe = [
        {"$match": month_logs_match(start_s, end_s, emp_code)},
        {"$addFields": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$match": {"_weekday": {"$gte": 2, "$lte": 6}}},
        {"$group": {"_id": None, "present_days": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}}}},
    ]
    wd_agg = list(attendance.aggregate(weekday_pipe))
    present = wd_agg[0]["present_days"] if wd_agg else 0.0
    return {
        "emp_code": emp_code, "month": month, "working_days": wd,
        "present_days": round_half_up(present), "leave_days": int(values.get("leave_days", 0)),
        "late_count": int(values.get("late_count", 0)), "total_late_minutes": int(values.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(values.get("total_overtime_minutes", 0)),
        "attendance_pct": None if wd == 0 else round_half_up(Decimal(str(present)) / Decimal(wd) * 100, 4),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start_s, end_s, start, end = month_bounds(month)
    # Start from employees to include employees without logs; lookup is bounded by month and uses indexed keys.
    emp_match: dict[str, Any] = {"joined_on": {"$lte": end_s}}
    if department is not None:
        emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}},
            {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1, "overtime_minutes": 1}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$ne": ["$logs", None]}, {"$in": ["$logs.status", list(PRESENCE)]},
                 {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}]},
                {"$cond": ["$logs.half_day", 0.5, 1]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]}, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]
    raw = list(employees.aggregate(pipeline, allowDiskUse=True))
    items = []
    for row in raw:
        if row["headcount"] == 0:
            continue
        items.append({"department": row["department"], "headcount": row["headcount"],
            "present_days": round_half_up(row.get("present_days", 0)),
            "avg_work_hours": response_round(row.get("avg_work_hours")),
            "late_count": row.get("late_count", 0), "total_late_minutes": row.get("total_late_minutes", 0),
            "leave_count": row.get("leave_count", 0), "on_duty_count": row.get("on_duty_count", 0)})
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start_s, end_s, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return {"month": month, "items": list(attendance.aggregate(pipeline, allowDiskUse=True))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to: str = Query(...),
):
    return _department_trend_core(department, from_date, to)


def department_trend_pipeline(department: str, start: date, end: date) -> list[dict]:
    """Build a DB-side calendar and aggregate each day, including dates with zero logs."""
    day_count = (end - start).days + 1
    start_dt = datetime.combine(start, time.min)
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "days": {"$range": [0, day_count]}}},
        {"$unwind": "$days"},
        {"$set": {"date_dt": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$days"}}}},
        {"$set": {"date_str": {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date_str"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}},
            {"$count": "count"},
        ], "as": "hc"}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date_str"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }},
        ], "as": "daily"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
            "weekday": {"$dayOfWeek": "$date_dt"}}},
        {"$set": {"is_working_day": {"$and": [{"$gte": ["$weekday", 2]}, {"$lte": ["$weekday", 6]}]},
            "attendance_rate": {"$cond": [{"$and": [{"$gte": ["$weekday", 2]}, {"$lte": ["$weekday", 6]}, {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date_dt": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date_dt": 1}},
        {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


def _department_trend_core(department: str, from_s: str, to_s: str):
    try:
        start = parse_date(from_s); end = parse_date(to_s)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    raw = list(employees.aggregate(department_trend_pipeline(department, start, end), allowDiskUse=True))
    items = []
    for row in raw:
        rate = row.get("attendance_rate")
        moving = row.get("moving_avg_7d")
        items.append({"date": row["date"], "is_working_day": row["is_working_day"], "headcount": row["headcount"],
            "present_count": round_half_up(row.get("present_count", 0)), "late_count": row.get("late_count", 0),
            "attendance_rate": None if rate is None else round_half_up(rate, 4),
            "moving_avg_7d": None if moving is None else round_half_up(moving, 4)})
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    page_args(page, page_size)
    if endpoint == "attendance_list":
        try:
            for d in (date_from, date_to):
                if d is not None: parse_date(d)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
        if status is not None and status not in ALL_STATUSES: raise HTTPException(422, "invalid status")
        q = attendance_filter(emp_code, date_from, date_to, status)
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": attendance_list_pipeline(q, page, page_size), "cursor": {}}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        start_s, end_s, _, _ = month_bounds(month)
        pipe = [{"$match": month_logs_match(start_s, end_s, emp_code)}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        start_s, end_s, _, end = month_bounds(month)
        q = {"joined_on": {"$lte": end_s}}
        if department is not None: q["department"] = department
        exp = db.command("explain", {"aggregate": "employees", "pipeline": [{"$match": q}, {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}}], "cursor": {}}, verbosity="executionStats")
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        start_s, end_s, _, _ = month_bounds(month)
        q = {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": [{"$match": q}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}], "cursor": {}}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_ or not to: raise HTTPException(422, "department, from and to are required")
        try:
            parse_date(from_); parse_date(to)
        except ValueError as exc: raise HTTPException(422, str(exc))
        try:
            start_date = parse_date(from_); end_date = parse_date(to)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if end_date < start_date or (end_date - start_date).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        exp = db.command("explain", {"aggregate": "employees", "pipeline": department_trend_pipeline(department, start_date, end_date), "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
        collection = "employees"
    else:
        raise HTTPException(422, "unsupported endpoint")
    return {"endpoint": endpoint, "collection": collection, "explain": exp}
