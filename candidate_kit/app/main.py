import os
import json
import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def round_half_up(val: float, decimals: int = 2) -> float:
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(Decimal(str(val)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def mongo_round_half_up(expr: Any, decimals: int = 2) -> Dict[str, Any]:
    factor = 10 ** decimals
    return {
        "$toDouble": {
            "$divide": [
                {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, factor]}, {"$toDecimal": "0.5"}]}},
                factor,
            ]
        }
    }


def validate_date_str(date_str: str) -> str:
    try:
        if len(date_str) != 10:
            raise ValueError()
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        raise ValueError("Invalid date format, expected YYYY-MM-DD")


def truncate_to_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    if not isinstance(ms, int) or isinstance(ms, bool) or ms < 100000000000 or ms > 4102444800000:
        raise ValueError("Must be valid epoch milliseconds")
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_shift_datetimes(date_str: str, shift_start: str, shift_end: str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    hs, ms = map(int, shift_start.split(":"))
    he, me = map(int, shift_end.split(":"))
    start_dt = d.replace(hour=hs, minute=ms, second=0, microsecond=0, tzinfo=IST)
    end_dt = d.replace(hour=he, minute=me, second=0, microsecond=0, tzinfo=IST)
    if (he * 60 + me) <= (hs * 60 + ms):
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def determine_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    ist_time = punch_in.astimezone(IST)
    hs, ms = map(int, shift_start.split(":"))
    he, me = map(int, shift_end.split(":"))
    if (he * 60 + me) <= (hs * 60 + ms):
        punch_in_seconds = ist_time.hour * 3600 + ist_time.minute * 60 + ist_time.second
        if punch_in_seconds < (he * 3600 + me * 60):
            return (ist_time.date() - timedelta(days=1)).isoformat()
    return ist_time.date().isoformat()


def compute_derived_fields(punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict, date_str: str):
    if not punch_in:
        return 0, None, 0, False
    punch_in = truncate_to_seconds(punch_in)
    start_dt, end_dt = get_shift_datetimes(date_str, emp["shift_start"], emp["shift_end"])
    diff_in = (punch_in - start_dt).total_seconds()
    late_mins = int(diff_in // 60) if diff_in > 600 else 0
    work_hours = None
    overtime_mins = 0
    half_day = False
    if punch_out:
        punch_out = truncate_to_seconds(punch_out)
        diff_out = (punch_out - end_dt).total_seconds()
        over_m = int(diff_out // 60)
        overtime_mins = over_m if over_m >= 30 else 0
        wh = Decimal(str((punch_out - punch_in).total_seconds())) / Decimal("3600")
        work_hours = float(wh.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        half_day = work_hours < 4.50
    return late_mins, work_hours, overtime_mins, half_day


def format_doc(doc: dict) -> dict:
    if not doc:
        return doc
    doc = dict(doc)
    doc.pop("_id", None)
    for field in ["created_at", "punch_in", "punch_out"]:
        if field in doc:
            doc[field] = dt_to_ms(doc[field])
    if "punch_in" in doc or "status" in doc:
        doc.setdefault("punch_in", None)
        doc.setdefault("punch_out", None)
        doc.setdefault("work_hours", None)
        doc["late_minutes"] = doc.get("late_minutes") or 0
        doc["overtime_minutes"] = doc.get("overtime_minutes") or 0
        doc["half_day"] = bool(doc.get("half_day", False))
        raw_history = doc.get("history") or []
        formatted_history = []
        for entry in raw_history:
            e_copy = dict(entry)
            e_copy["at"] = dt_to_ms(e_copy.get("at"))
            changes_copy = {}
            for key, val in e_copy.get("changes", {}).items():
                if key in ["punch_in", "punch_out"]:
                    changes_copy[key] = {"from": dt_to_ms(val.get("from")), "to": dt_to_ms(val.get("to"))}
                else:
                    changes_copy[key] = {"from": val.get("from"), "to": val.get("to")}
            e_copy["changes"] = changes_copy
            formatted_history.append(e_copy)
        doc["history"] = formatted_history
    return doc


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v: str) -> str:
        return validate_date_str(v)

    @model_validator(mode="after")
    def check_shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unreachable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(timezone.utc))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return format_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    return {"items": [format_doc(i) for i in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else truncate_to_seconds(datetime.now(timezone.utc))
    d = determine_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late, wh, over, hd = compute_derived_fields(ts, None, emp, d)
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": over,
        "half_day": hd,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
    return format_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        if date_from is not None:
            validate_date_str(date_from)
        if date_to is not None:
            validate_date_str(date_to)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")
    q: Dict[str, Any] = {}
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
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    )
    return {"items": [format_doc(i) for i in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else truncate_to_seconds(datetime.now(timezone.utc))
    log = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not log:
        if db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$gt": ts}}):
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "No matching punch-in found")
    if log.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    punch_in_dt = truncate_to_seconds(log["punch_in"])
    if ts <= punch_in_dt or (ts - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, "punched_at must be > punch_in and within 24 hours")
    late, wh, over, hd = compute_derived_fields(punch_in_dt, ts, emp, log["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": wh, "overtime_minutes": over, "half_day": hd}},
        return_document=True,
    )
    if not updated:
        raise HTTPException(409, "Already punched out")
    return format_doc(updated)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        validate_date_str(date)
    except ValueError as e:
        raise HTTPException(422, str(e))
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    for _ in range(3):
        log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not log:
            raise HTTPException(404, "Record not found")
        old_status = log.get("status")
        old_in = truncate_to_seconds(log["punch_in"]) if log.get("punch_in") else None
        old_out = truncate_to_seconds(log["punch_out"]) if log.get("punch_out") else None
        old_wh = log.get("work_hours")
        old_late = log.get("late_minutes", 0)
        old_over = log.get("overtime_minutes", 0)
        old_hd = bool(log.get("half_day", False))
        new_status = body.status if body.status is not None else old_status
        if new_status in ["ABSENT", "LEAVE"]:
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, "Cannot supply punch_in or punch_out with ABSENT or LEAVE")
            new_in, new_out = None, None
            new_late, new_wh, new_over, new_hd = 0, None, 0, False
        else:
            new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
            new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
            if not new_in:
                raise HTTPException(422, "Presence status requires punch_in")
            if determine_attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
            if new_out and (new_out <= new_in or (new_out - new_in).total_seconds() > 86400):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            new_late, new_wh, new_over, new_hd = compute_derived_fields(new_in, new_out, emp, date)
        old_state = {
            "status": old_status,
            "punch_in": old_in,
            "punch_out": old_out,
            "work_hours": old_wh,
            "late_minutes": old_late,
            "overtime_minutes": old_over,
            "half_day": old_hd,
        }
        new_state = {
            "status": new_status,
            "punch_in": new_in,
            "punch_out": new_out,
            "work_hours": new_wh,
            "late_minutes": new_late,
            "overtime_minutes": new_over,
            "half_day": new_hd,
        }
        changes = {k: {"from": old_state[k], "to": new_state[k]} for k in old_state if old_state[k] != new_state[k]}
        if not changes:
            raise HTTPException(422, "Request changes nothing")
        history_entry = {
            "at": truncate_to_seconds(datetime.now(timezone.utc)),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }
        concurrency_filter = (
            {
                "_id": log["_id"],
                "status": log.get("status"),
                "punch_in": log.get("punch_in"),
                "punch_out": log.get("punch_out"),
                "history": log.get("history", []),
            }
            if "history" in log
            else {"_id": log["_id"], "history": {"$exists": False}}
        )
        updated = db.attendance_logs.find_one_and_update(
            concurrency_filter,
            {"$set": new_state, "$push": {"history": history_entry}},
            return_document=True,
        )
        if updated:
            return format_doc(updated)
    raise HTTPException(409, "Concurrent modification conflict")


def _build_employee_monthly_pipeline(emp_code: str, month: str):
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"}}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "is_weekday": {
                    "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start, month_end = f"{month}-01", f"{month}-{last_day:02d}"
    start_date = max(month_start, emp["joined_on"])
    if start_date <= month_end:
        d_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        d_end = datetime.strptime(month_end, "%Y-%m-%d").date()
        working_days = sum(
            1 for i in range((d_end - d_start).days + 1) if (d_start + timedelta(days=i)).weekday() < 5
        )
    else:
        working_days = 0
    res = list(db.attendance_logs.aggregate(_build_employee_monthly_pipeline(emp_code, month)))
    data = (
        res[0]
        if res
        else {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }
    )
    pct = round_half_up(data["present_days"] / working_days * 100, 2) if working_days > 0 else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": data["present_days"],
        "leave_days": data["leave_days"],
        "late_count": data["late_count"],
        "total_late_minutes": data["total_late_minutes"],
        "total_overtime_minutes": data["total_overtime_minutes"],
        "attendance_pct": pct,
    }


def _build_department_summary_pipeline(month: str, department: Optional[str] = None):
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start_str, month_end_str = f"{month}-01", f"{month}-{last_day:02d}"
    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end_str}}
    if department is not None:
        match_stage["department"] = department
    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": month_start_str, "$lte": month_end_str}}}],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "is_present": {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "present_val": {
                    "$cond": [
                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                        0,
                    ]
                },
                "is_weekday": {
                    "$cond": [
                        {
                            "$and": [
                                {"$ifNull": ["$logs.date", False]},
                                {
                                    "$in": [
                                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                                        [2, 3, 4, 5, 6],
                                    ]
                                },
                            ]
                        },
                        True,
                        False,
                    ]
                },
                "work_hours": "$logs.work_hours",
                "is_late": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]},
                "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
                "is_leave": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]},
                "is_on_duty": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]},
            }
        },
        {
            "$group": {
                "_id": {"dept": "$department", "emp": "$emp_code"},
                "present_days": {"$sum": {"$cond": ["$is_weekday", "$present_val", 0]}},
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_present", {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]},
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_present", {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": "$is_late"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": "$is_leave"},
                "on_duty_count": {"$sum": "$is_on_duty"},
            }
        },
        {
            "$group": {
                "_id": "$_id.dept",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "total_wh_sum": {"$sum": "$work_hours_sum"},
                "total_wh_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$total_wh_count", 0]},
                        mongo_round_half_up({"$divide": ["$total_wh_sum", "$total_wh_count"]}, 2),
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    res = list(db.employees.aggregate(_build_department_summary_pipeline(month, department)))
    return {"month": month, "items": res}


def _build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None):
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend(
        [
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
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
    )
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    res = list(db.attendance_logs.aggregate(_build_late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": res}


def _build_department_trend_pipeline(department: str, from_date: str, to_date: str):
    start_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = (datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=timezone.utc)
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "date_dt": {"$literal": start_dt}}},
        {"$densify": {"field": "date_dt", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}}],
                "as": "emps",
            }
        },
        {"$addFields": {"headcount": {"$size": "$emps"}}},
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [{"$match": {"department": department}}, {"$project": {"_id": 0, "emp_code": 1}}],
                "as": "all_dept_emps",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "date",
                "foreignField": "date",
                "let": {"dept_codes": "$all_dept_emps.emp_code"},
                "pipeline": [{"$match": {"$expr": {"$in": ["$emp_code", "$$dept_codes"]}}}],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                }
                            },
                            "as": "pl",
                            "in": {"$cond": [{"$eq": [{"$ifNull": ["$$pl.half_day", False]}, True]}, 0.5, 1]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        mongo_round_half_up({"$divide": ["$present_count", "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [{"$ne": ["$moving_avg_7d", None]}, mongo_round_half_up("$moving_avg_7d", 4), None]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        validate_date_str(from_date)
        validate_date_str(to_date)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if from_date > to_date:
        raise HTTPException(422, "from cannot be greater than to")
    if (datetime.strptime(to_date, "%Y-%m-%d") - datetime.strptime(from_date, "%Y-%m-%d")).days > 92:
        raise HTTPException(422, "Range exceeds 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")
    res = list(db.employees.aggregate(_build_department_trend_pipeline(department, from_date, to_date)))
    return {"department": department, "items": res}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(
        ...,
        pattern="^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$",
    ),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    explain_res, coll = None, ""
    if endpoint == "attendance_list":
        try:
            if date_from is not None:
                validate_date_str(date_from)
            if date_to is not None:
                validate_date_str(date_to)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from > date_to")
        coll = "attendance_logs"
        q: Dict[str, Any] = {}
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
        explain_res = db.command(
            "explain",
            {
                "find": coll,
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "Missing emp_code or month")
        coll = "attendance_logs"
        pipe = _build_employee_monthly_pipeline(emp_code, month)
        explain_res = db.command(
            "explain", {"aggregate": coll, "pipeline": pipe, "cursor": {}}, verbosity="executionStats"
        )
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "Missing month")
        coll = "employees"
        pipe = _build_department_summary_pipeline(month, department)
        explain_res = db.command(
            "explain", {"aggregate": coll, "pipeline": pipe, "cursor": {}}, verbosity="executionStats"
        )
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "Missing month")
        coll = "attendance_logs"
        pipe = _build_late_leaderboard_pipeline(month, limit, department)
        explain_res = db.command(
            "explain", {"aggregate": coll, "pipeline": pipe, "cursor": {}}, verbosity="executionStats"
        )
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "Missing department, from, or to")
        try:
            validate_date_str(from_date)
            validate_date_str(to_date)
        except ValueError as e:
            raise HTTPException(422, str(e))
        if from_date > to_date or (
            datetime.strptime(to_date, "%Y-%m-%d") - datetime.strptime(from_date, "%Y-%m-%d")
        ).days > 92:
            raise HTTPException(422, "Invalid date range")
        coll = "employees"
        pipe = _build_department_trend_pipeline(department, from_date, to_date)
        explain_res = db.command(
            "explain", {"aggregate": coll, "pipeline": pipe, "cursor": {}}, verbosity="executionStats"
        )
    clean_explain = json.loads(json_util.dumps(explain_res))
    return {"endpoint": endpoint, "collection": coll, "explain": clean_explain}