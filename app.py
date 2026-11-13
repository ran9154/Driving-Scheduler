import hashlib
import hmac
import json
import os
import random
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from excel_export import build_schedule_excel
from scheduler import generate_week_slots, run_smart_schedule

SECRET_KEY = os.environ.get("SECRET_KEY", "driving-scheduler-local-secret-key").encode()

app = FastAPI(title="Driving Instructor Scheduler")
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


@app.on_event("startup")
def on_startup() -> None:
    db.init_db()


def geocode_address(address: str, city: str = "") -> tuple[float, float] | None:
    """Resolve an Israeli street address + city into (lat, lng) using OpenStreetMap Nominatim."""
    query = ", ".join(p for p in [address.strip(), city.strip(), "Israel"] if p)
    if not query or query == "Israel":
        return None
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1, "countrycodes": "il"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "DrivingSchedulerApp/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        return None
    return None


def sign_student_token(student_id: int) -> str:
    msg = str(student_id).encode()
    sig = hmac.new(SECRET_KEY, msg, hashlib.sha256).hexdigest()[:24]
    return f"{student_id}.{sig}"


def verify_student_token(token: str | None) -> dict[str, Any]:
    if not token or "." not in token:
        raise HTTPException(status_code=401, detail="יש להתחבר מחדש")
    sid_str, sig = token.split(".", 1)
    try:
        sid = int(sid_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="טוקן לא תקין")
    expected = sign_student_token(sid).split(".", 1)[1]
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="טוקן לא תקין")

    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM students WHERE id = ?", (sid,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="תלמיד לא נמצא")
    student = dict(row)
    if student["status"] != "active":
        raise HTTPException(
            status_code=403,
            detail="החשבון אינו פעיל במערכת. אם מדובר בטעות, פנה למורה הנהיגה.",
        )
    return student


def require_instructor(x_instructor_pin: str | None) -> None:
    settings = db.get_settings()
    if not x_instructor_pin or x_instructor_pin != settings["instructor_pin"]:
        raise HTTPException(status_code=401, detail="קוד מורה שגוי")


# --- Pydantic models ---
class StudentLoginReq(BaseModel):
    phone: str
    pin: str


class StudentRegisterReq(BaseModel):
    name: str
    phone: str
    area: str = "רמלה"
    address: str = ""


class StudentConstraintsReq(BaseModel):
    week_start: str | None = None
    requested_lessons: int = 1
    prefer_double: bool = False
    blocked_slots: list[str] = []
    area: str | None = None
    address: str | None = None


class InstructorLoginReq(BaseModel):
    pin: str


class StudentCreateReq(BaseModel):
    name: str
    phone: str
    pin: str = ""
    area: str = "רמלה"
    address: str = ""
    notes: str = ""


class StudentUpdateReq(BaseModel):
    name: str | None = None
    phone: str | None = None
    pin: str | None = None
    area: str | None = None
    status: str | None = None
    notes: str | None = None


class LockOverrideReq(BaseModel):
    lock_override: int | None = None  # None=auto, 0=open, 1=locked


class BlockCreateReq(BaseModel):
    day: int
    start_time: str
    end_time: str
    block_type: str = "personal"
    title: str = ""


class LessonCreateReq(BaseModel):
    day: int
    start_time: str
    student_id: int
    is_double: bool = False
    is_locked: bool = True


class LessonUpdateReq(BaseModel):
    day: int | None = None
    start_time: str | None = None
    is_double: bool | None = None
    is_locked: bool | None = None


# --- Page Routes ---
@app.get("/")
def serve_student_app() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/instructor")
def serve_instructor_app() -> FileResponse:
    return FileResponse(STATIC_DIR / "instructor.html")


# --- Student Endpoints ---
@app.get("/api/public/areas")
def public_get_areas() -> dict[str, Any]:
    settings = db.get_settings()
    return {"areas": settings["areas"]}


@app.post("/api/student/register")
def student_register(body: StudentRegisterReq) -> dict[str, Any]:
    name = body.name.strip()
    phone = body.phone.strip().replace("-", "").replace(" ", "")
    area = body.area.strip() or "רמלה"
    address = body.address.strip()
    if not name or not phone:
        raise HTTPException(status_code=400, detail="יש למלא שם ומספר טלפון")

    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM students WHERE replace(replace(phone, '-', ''), ' ', '') = ?",
            (phone,),
        ).fetchone()
        if existing:
            raise HTTPException(
                status_code=409,
                detail="מספר טלפון זה כבר רשום במערכת — יש להתחבר עם הטלפון והקוד האישי",
            )

    pin = f"{random.randint(1000, 9999)}"
    coords = geocode_address(address, area) if (address or area) else None
    lat, lng = coords if coords else (None, None)
    now_iso = datetime.now().isoformat(timespec="seconds")

    with db.get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO students (name, phone, pin, area, address, lat, lng, status, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', '', ?)
            """,
            (name, phone, pin, area, address, lat, lng, now_iso),
        )
        sid = cur.lastrowid

    return {
        "token": sign_student_token(sid),
        "pin": pin,
        "student": {"id": sid, "name": name, "area": area, "address": address},
    }


@app.post("/api/student/login")
def student_login(body: StudentLoginReq) -> dict[str, Any]:
    phone = body.phone.strip().replace("-", "").replace(" ", "")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM students WHERE replace(replace(phone, '-', ''), ' ', '') = ?",
            (phone,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="מספר טלפון או קוד אישי שגויים")
    student = dict(row)
    if student["pin"] != body.pin.strip():
        raise HTTPException(status_code=401, detail="מספר טלפון או קוד אישי שגויים")
    if student["status"] != "active":
        raise HTTPException(
            status_code=403,
            detail="החשבון אינו פעיל במערכת. אם מדובר בטעות, פנה למורה הנהיגה.",
        )
    return {
        "token": sign_student_token(student["id"]),
        "student": {
            "id": student["id"],
            "name": student["name"],
            "area": student["area"],
        },
    }


@app.get("/api/student/week")
def student_get_week(x_student_token: str | None = Header(default=None)) -> dict[str, Any]:
    student = verify_student_token(x_student_token)
    week_start = db.get_next_sunday()
    locked = db.is_week_locked(week_start)
    settings = db.get_settings()
    slots = generate_week_slots(settings)

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM student_constraints WHERE week_start = ? AND student_id = ?",
            (week_start, student["id"]),
        ).fetchone()
        prev_row = conn.execute(
            "SELECT * FROM student_constraints WHERE week_start < ? AND student_id = ? ORDER BY week_start DESC LIMIT 1",
            (week_start, student["id"]),
        ).fetchone()

    submission = None
    if row:
        submission = {
            "requested_lessons": row["requested_lessons"],
            "prefer_double": bool(row["prefer_double"]),
            "blocked_slots": json.loads(row["blocked_slots"]),
            "submitted_at": row["submitted_at"],
        }

    last_submission = None
    if prev_row:
        last_submission = {
            "requested_lessons": prev_row["requested_lessons"],
            "prefer_double": bool(prev_row["prefer_double"]),
            "blocked_slots": json.loads(prev_row["blocked_slots"]),
        }

    return {
        "week_start": week_start,
        "is_locked": locked,
        "lock_day": settings["lock_day"],
        "lock_time": settings["lock_time"],
        "lesson_duration_min": settings["lesson_duration_min"],
        "areas": settings["areas"],
        "student": {
            "id": student["id"],
            "name": student["name"],
            "area": student["area"],
            "address": student.get("address", ""),
            "has_coords": student.get("lat") is not None,
        },
        "slots": [{"day": s.day, "start_time": s.start_time, "key": s.key} for s in slots],
        "submission": submission,
        "last_submission": last_submission,
    }


@app.post("/api/student/constraints")
def student_save_constraints(
    body: StudentConstraintsReq,
    x_student_token: str | None = Header(default=None),
) -> dict[str, Any]:
    student = verify_student_token(x_student_token)
    week_start = body.week_start or db.get_next_sunday()
    if db.is_week_locked(week_start):
        raise HTTPException(status_code=423, detail="מועד ההגשה לשבוע זה ננעל")

    now_iso = datetime.now().isoformat(timespec="seconds")
    new_area = body.area.strip() if body.area and body.area.strip() else student["area"]
    new_addr = body.address.strip() if body.address is not None else student.get("address", "")
    coords = None
    if (new_area != student["area"] or new_addr != student.get("address", "")) and (new_addr or new_area):
        coords = geocode_address(new_addr, new_area)

    with db.get_conn() as conn:
        if coords:
            conn.execute(
                "UPDATE students SET area = ?, address = ?, lat = ?, lng = ? WHERE id = ?",
                (new_area, new_addr, coords[0], coords[1], student["id"]),
            )
        elif new_area != student["area"] or new_addr != student.get("address", ""):
            conn.execute(
                "UPDATE students SET area = ?, address = ? WHERE id = ?",
                (new_area, new_addr, student["id"]),
            )
        conn.execute(
            """
            INSERT INTO student_constraints (week_start, student_id, requested_lessons, prefer_double, blocked_slots, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(week_start, student_id) DO UPDATE SET
                requested_lessons = excluded.requested_lessons,
                prefer_double = excluded.prefer_double,
                blocked_slots = excluded.blocked_slots,
                submitted_at = excluded.submitted_at
            """,
            (
                week_start,
                student["id"],
                max(0, min(4, body.requested_lessons)),
                int(body.prefer_double),
                json.dumps(body.blocked_slots),
                now_iso,
            ),
        )
    return {"ok": True, "submitted_at": now_iso}


# --- Instructor Endpoints ---
@app.post("/api/instructor/login")
def instructor_login(body: InstructorLoginReq) -> dict[str, Any]:
    require_instructor(body.pin.strip())
    return {"ok": True}


@app.get("/api/instructor/settings")
def instructor_get_settings(x_instructor_pin: str | None = Header(default=None)) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    return db.get_settings()


@app.put("/api/instructor/settings")
def instructor_update_settings(
    updates: dict[str, Any],
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    return db.update_settings(updates)


@app.get("/api/instructor/students")
def instructor_list_students(x_instructor_pin: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_instructor(x_instructor_pin)
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM students ORDER BY status = 'active' DESC, name ASC").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/instructor/students")
def instructor_create_student(
    body: StudentCreateReq,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    phone = body.phone.strip().replace("-", "").replace(" ", "")
    pin = body.pin.strip() or f"{random.randint(1000, 9999)}"
    area = body.area.strip() or "רמלה"
    address = body.address.strip()
    coords = geocode_address(address, area) if address else None
    lat, lng = coords if coords else (None, None)
    now_iso = datetime.now().isoformat(timespec="seconds")
    try:
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO students (name, phone, pin, area, address, lat, lng, status, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (body.name.strip(), phone, pin, area, address, lat, lng, body.notes.strip(), now_iso),
            )
            row = conn.execute("SELECT * FROM students WHERE id = ?", (cur.lastrowid,)).fetchone()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"שגיאה בהוספת תלמיד (אולי הטלפון כבר קיים?): {exc}")
    return dict(row)


@app.delete("/api/instructor/students/{student_id}")
def instructor_delete_student(
    student_id: int,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    with db.get_conn() as conn:
        cur = conn.execute("DELETE FROM students WHERE id = ?", (student_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="תלמיד לא נמצא")
    return {"ok": True}


@app.patch("/api/instructor/students/{student_id}")
def instructor_update_student(
    student_id: int,
    body: StudentUpdateReq,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="אין שדות לעדכון")
    if "phone" in fields:
        fields["phone"] = fields["phone"].strip().replace("-", "").replace(" ", "")
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [student_id]
    with db.get_conn() as conn:
        conn.execute(f"UPDATE students SET {set_clause} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="תלמיד לא נמצא")
    return dict(row)


@app.get("/api/instructor/week/{week_start}")
def instructor_get_week(
    week_start: str,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    if week_start == "next":
        week_start = db.get_next_sunday()
    week = db.ensure_week(week_start)
    locked = db.is_week_locked(week_start)
    settings = db.get_settings()
    slots = generate_week_slots(settings)

    with db.get_conn() as conn:
        students = [dict(r) for r in conn.execute("SELECT * FROM students ORDER BY name ASC").fetchall()]
        c_rows = conn.execute(
            "SELECT * FROM student_constraints WHERE week_start = ?", (week_start,)
        ).fetchall()
        blocks = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM instructor_blocks WHERE week_start = ? ORDER BY day, start_time", (week_start,)
            ).fetchall()
        ]
        lessons = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM scheduled_lessons WHERE week_start = ? ORDER BY day, start_time", (week_start,)
            ).fetchall()
        ]

    constraints_map = {}
    for r in c_rows:
        constraints_map[r["student_id"]] = {
            "requested_lessons": r["requested_lessons"],
            "prefer_double": bool(r["prefer_double"]),
            "blocked_slots": json.loads(r["blocked_slots"]),
            "submitted_at": r["submitted_at"],
        }

    active_students = [s for s in students if s["status"] == "active"]
    submitted_ids = set(constraints_map.keys())
    missing_students = [s for s in active_students if s["id"] not in submitted_ids]

    return {
        "week_start": week_start,
        "week_status": week["status"],
        "lock_override": week["lock_override"],
        "is_locked": locked,
        "settings": settings,
        "slots": [{"day": s.day, "start_time": s.start_time, "key": s.key} for s in slots],
        "students": students,
        "constraints": constraints_map,
        "missing_students": missing_students,
        "active_count": len(active_students),
        "submitted_count": sum(1 for s in active_students if s["id"] in submitted_ids),
        "instructor_blocks": blocks,
        "scheduled_lessons": lessons,
    }


@app.post("/api/instructor/week/{week_start}/lock")
def instructor_set_lock(
    week_start: str,
    body: LockOverrideReq,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    db.ensure_week(week_start)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE weeks SET lock_override = ? WHERE week_start = ?",
            (body.lock_override, week_start),
        )
    return {"ok": True, "is_locked": db.is_week_locked(week_start)}


@app.post("/api/instructor/week/{week_start}/student-constraints/{student_id}")
def instructor_override_student_constraints(
    week_start: str,
    student_id: int,
    body: StudentConstraintsReq,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    now_iso = datetime.now().isoformat(timespec="seconds")
    with db.get_conn() as conn:
        if body.area and body.area.strip():
            conn.execute(
                "UPDATE students SET area = ? WHERE id = ?",
                (body.area.strip(), student_id),
            )
        conn.execute(
            """
            INSERT INTO student_constraints (week_start, student_id, requested_lessons, prefer_double, blocked_slots, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(week_start, student_id) DO UPDATE SET
                requested_lessons = excluded.requested_lessons,
                prefer_double = excluded.prefer_double,
                blocked_slots = excluded.blocked_slots,
                submitted_at = excluded.submitted_at
            """,
            (
                week_start,
                student_id,
                max(0, min(4, body.requested_lessons)),
                int(body.prefer_double),
                json.dumps(body.blocked_slots),
                now_iso,
            ),
        )
    return {"ok": True}


@app.post("/api/instructor/week/{week_start}/blocks")
def instructor_add_block(
    week_start: str,
    body: BlockCreateReq,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    db.ensure_week(week_start)
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO instructor_blocks (week_start, day, start_time, end_time, block_type, title) VALUES (?, ?, ?, ?, ?, ?)",
            (week_start, body.day, body.start_time, body.end_time, body.block_type, body.title.strip()),
        )
        row = conn.execute("SELECT * FROM instructor_blocks WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


@app.delete("/api/instructor/blocks/{block_id}")
def instructor_delete_block(
    block_id: int,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM instructor_blocks WHERE id = ?", (block_id,))
    return {"ok": True}


@app.post("/api/instructor/week/{week_start}/auto-schedule")
def instructor_auto_schedule(
    week_start: str,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    db.ensure_week(week_start)
    settings = db.get_settings()

    with db.get_conn() as conn:
        students = [dict(r) for r in conn.execute("SELECT * FROM students").fetchall()]
        c_rows = conn.execute("SELECT * FROM student_constraints WHERE week_start = ?", (week_start,)).fetchall()
        blocks = [
            dict(r) for r in conn.execute("SELECT * FROM instructor_blocks WHERE week_start = ?", (week_start,)).fetchall()
        ]
        locked_lessons = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM scheduled_lessons WHERE week_start = ? AND is_locked = 1", (week_start,)
            ).fetchall()
        ]

    constraints_by_student = {
        r["student_id"]: {
            "requested_lessons": r["requested_lessons"],
            "prefer_double": bool(r["prefer_double"]),
            "blocked_slots": json.loads(r["blocked_slots"]),
        }
        for r in c_rows
    }

    result = run_smart_schedule(
        settings=settings,
        students=students,
        constraints_by_student=constraints_by_student,
        instructor_blocks=blocks,
        locked_lessons=locked_lessons,
    )

    with db.get_conn() as conn:
        conn.execute("DELETE FROM scheduled_lessons WHERE week_start = ? AND is_locked = 0", (week_start,))
        for ls in result["lessons"]:
            if ls["is_locked"]:
                continue
            conn.execute(
                """
                INSERT INTO scheduled_lessons (week_start, day, start_time, duration_min, student_id, is_locked, is_double)
                VALUES (?, ?, ?, ?, ?, 0, ?)
                """,
                (week_start, ls["day"], ls["start_time"], ls["duration_min"], ls["student_id"], int(ls["is_double"])),
            )
        conn.execute("UPDATE weeks SET status = 'draft' WHERE week_start = ?", (week_start,))
        saved = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM scheduled_lessons WHERE week_start = ? ORDER BY day, start_time", (week_start,)
            ).fetchall()
        ]

    return {
        "scheduled_lessons": saved,
        "unscheduled": result["unscheduled"],
        "stats": result["stats"],
    }


@app.post("/api/instructor/week/{week_start}/lessons")
def instructor_add_lesson(
    week_start: str,
    body: LessonCreateReq,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    settings = db.get_settings()
    dur = int(settings["lesson_duration_min"]) * (2 if body.is_double else 1)
    with db.get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO scheduled_lessons (week_start, day, start_time, duration_min, student_id, is_locked, is_double)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (week_start, body.day, body.start_time, dur, body.student_id, int(body.is_locked), int(body.is_double)),
        )
        row = conn.execute("SELECT * FROM scheduled_lessons WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


@app.patch("/api/instructor/lessons/{lesson_id}")
def instructor_update_lesson(
    lesson_id: int,
    body: LessonUpdateReq,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="אין שדות לעדכון")
    if "is_locked" in fields:
        fields["is_locked"] = int(fields["is_locked"])
    if "is_double" in fields:
        settings = db.get_settings()
        fields["duration_min"] = int(settings["lesson_duration_min"]) * (2 if fields["is_double"] else 1)
        fields["is_double"] = int(fields["is_double"])
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with db.get_conn() as conn:
        conn.execute(f"UPDATE scheduled_lessons SET {set_clause} WHERE id = ?", list(fields.values()) + [lesson_id])
        row = conn.execute("SELECT * FROM scheduled_lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="שיעור לא נמצא")
    return dict(row)


@app.delete("/api/instructor/lessons/{lesson_id}")
def instructor_delete_lesson(
    lesson_id: int,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM scheduled_lessons WHERE id = ?", (lesson_id,))
    return {"ok": True}


@app.post("/api/instructor/week/{week_start}/approve")
def instructor_approve_week(
    week_start: str,
    x_instructor_pin: str | None = Header(default=None),
) -> dict[str, Any]:
    require_instructor(x_instructor_pin)
    with db.get_conn() as conn:
        conn.execute("UPDATE weeks SET status = 'approved' WHERE week_start = ?", (week_start,))
    return {"ok": True}


@app.get("/api/instructor/week/{week_start}/export")
def instructor_export_excel(
    week_start: str,
    pin: str | None = None,
    x_instructor_pin: str | None = Header(default=None),
) -> StreamingResponse:
    require_instructor(x_instructor_pin or pin)
    settings = db.get_settings()
    with db.get_conn() as conn:
        students = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM students").fetchall()}
        lessons = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM scheduled_lessons WHERE week_start = ? ORDER BY day, start_time", (week_start,)
            ).fetchall()
        ]
        blocks = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM instructor_blocks WHERE week_start = ? ORDER BY day, start_time", (week_start,)
            ).fetchall()
        ]
        c_rows = conn.execute("SELECT * FROM student_constraints WHERE week_start = ?", (week_start,)).fetchall()
    constraints_by_student = {r["student_id"]: dict(r) for r in c_rows}

    buf = build_schedule_excel(
        week_start=week_start,
        settings=settings,
        students_by_id=students,
        lessons=lessons,
        instructor_blocks=blocks,
        constraints_by_student=constraints_by_student,
    )
    filename = f"schedule_{week_start}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/instructor/demo-seed")
def instructor_seed_demo(x_instructor_pin: str | None = Header(default=None)) -> dict[str, Any]:
    """Populate 50 realistic students and sample constraints for next week to test the scheduler."""
    require_instructor(x_instructor_pin)
    first_names = [
        "נועם", "איתי", "תמר", "מאיה", "עומר", "יובל", "שירה", "דניאל", "אורי", "רוני",
        "עידו", "ליהי", "אדם", "גיל", "הילה", "אלון", "נעה", "יהלי", "אריאל", "ליאור",
        "טל", "עדי", "גיא", "מיכל", "רועי",
    ]
    last_names = [
        "כהן", "לוי", "מזרחי", "פרץ", "ביטון", "דהן", "אברהם", "פרידמן", "כץ", "יוסף",
        "מלכה", "אזולאי", "גבאי", "שפירא", "חזן",
    ]
    settings = db.get_settings()
    areas = settings["areas"] or db.DEFAULT_AREAS
    rng = random.Random(101)
    week_start = db.get_next_sunday()
    db.ensure_week(week_start)
    slots = generate_week_slots(settings)
    now_iso = datetime.now().isoformat(timespec="seconds")

    created_ids = []
    with db.get_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) as c FROM students").fetchone()["c"]
        if existing == 0:
            for i in range(50):
                name = f"{first_names[i % len(first_names)]} {last_names[(i * 3) % len(last_names)]}"
                phone = f"050{1000000 + i:07d}"
                area = areas[i % len(areas)]
                cur = conn.execute(
                    "INSERT INTO students (name, phone, pin, area, status, notes, created_at) VALUES (?, ?, '1111', ?, 'active', '', ?)",
                    (name, phone, area, now_iso),
                )
                created_ids.append(cur.lastrowid)
        else:
            created_ids = [
                r["id"] for r in conn.execute("SELECT id FROM students WHERE status = 'active'").fetchall()
            ]

        # Seed constraints for ~92% of active students (leaving ~4 who haven't submitted yet)
        for idx, sid in enumerate(created_ids):
            if idx >= len(created_ids) - 4:
                continue
            # Block ~35% of slots randomly (simulating school/army/work)
            blocked = [s.key for s in slots if rng.random() < 0.35]
            req = 2 if idx % 4 == 0 else 1
            pref_double = 1 if (req == 2 and idx % 8 == 0) else 0
            conn.execute(
                """
                INSERT OR IGNORE INTO student_constraints
                (week_start, student_id, requested_lessons, prefer_double, blocked_slots, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (week_start, sid, req, pref_double, json.dumps(blocked), now_iso),
            )

        # Add an early morning test block if none exists
        blk_count = conn.execute(
            "SELECT COUNT(*) as c FROM instructor_blocks WHERE week_start = ?", (week_start,)
        ).fetchone()["c"]
        if blk_count == 0:
            conn.execute(
                "INSERT INTO instructor_blocks (week_start, day, start_time, end_time, block_type, title) VALUES (?, 1, '07:15', '08:30', 'test', 'טסט בוקר - נועם כהן')",
                (week_start,),
            )

    return {"ok": True, "students_count": len(created_ids), "week_start": week_start}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
