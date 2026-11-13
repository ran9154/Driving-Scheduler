from io import BytesIO
from openpyxl import load_workbook

import db
from excel_export import build_schedule_excel
from scheduler import has_valid_lunch_gap, run_smart_schedule, time_to_min


def test_smart_scheduler_respects_all_constraints():
    settings = {
        "lesson_duration_min": 40,
        "lunch_mode": "flexible",
        "lunch_window_start": "12:30",
        "lunch_window_end": "15:00",
        "lunch_fixed_start": "13:00",
        "work_hours": db.DEFAULT_WORK_HOURS,
    }
    students = [
        {"id": 1, "name": "איתי", "phone": "0501", "area": "צפון", "status": "active"},
        {"id": 2, "name": "נועם", "phone": "0502", "area": "צפון", "status": "active"},
        {"id": 3, "name": "תמר", "phone": "0503", "area": "דרום", "status": "active"},
        {"id": 4, "name": "עזב", "phone": "0504", "area": "מרכז", "status": "archived"},
    ]
    constraints = {
        1: {"requested_lessons": 1, "prefer_double": False, "blocked_slots": ["0:08:00", "0:08:40"]},
        2: {"requested_lessons": 2, "prefer_double": True, "blocked_slots": []},
        3: {"requested_lessons": 1, "prefer_double": False, "blocked_slots": []},
        4: {"requested_lessons": 2, "prefer_double": False, "blocked_slots": []},
    }
    blocks = [
        {"day": 0, "start_time": "07:30", "end_time": "08:30", "block_type": "test", "title": "טסט בוקר"},
    ]

    res = run_smart_schedule(
        settings=settings,
        students=students,
        constraints_by_student=constraints,
        instructor_blocks=blocks,
        locked_lessons=[],
    )

    lessons = res["lessons"]
    # Archived student 4 must never be scheduled
    assert all(l["student_id"] != 4 for l in lessons)
    # Student 1 must not be at 0:08:00 or 0:08:40
    assert all(not (l["student_id"] == 1 and l["day"] == 0 and l["start_time"] in ("08:00", "08:40")) for l in lessons)
    # No lesson on day 0 at 08:00 (overlaps 07:30-08:30 test)
    assert all(not (l["day"] == 0 and l["start_time"] == "08:00") for l in lessons)
    # Student 2 requested double -> should get a double lesson
    assert any(l["student_id"] == 2 and l["is_double"] for l in lessons)

    # Lunch gap >= 60 min preserved for each day 0..4
    for d in range(5):
        occ = [(time_to_min(l["start_time"]), time_to_min(l["start_time"]) + l["duration_min"]) for l in lessons if l["day"] == d]
        assert has_valid_lunch_gap(d, occ, time_to_min("12:30"), time_to_min("15:00"), 60)


def test_excel_export_creates_valid_workbook():
    settings = {
        "lesson_duration_min": 40,
        "work_hours": db.DEFAULT_WORK_HOURS,
    }
    students_by_id = {
        1: {"id": 1, "name": "איתי כהן", "phone": "0501111111", "area": "מרכז", "status": "active"},
    }
    lessons = [
        {"day": 0, "start_time": "08:00", "duration_min": 40, "student_id": 1, "is_locked": 0, "is_double": 0},
    ]
    blocks = [
        {"day": 1, "start_time": "07:15", "end_time": "08:30", "block_type": "test", "title": "טסט בוקר"},
    ]
    buf: BytesIO = build_schedule_excel(
        week_start="2026-11-15",
        settings=settings,
        students_by_id=students_by_id,
        lessons=lessons,
        instructor_blocks=blocks,
        constraints_by_student={1: {"requested_lessons": 1}},
    )
    wb = load_workbook(buf)
    assert "לוז שבועי" in wb.sheetnames
    assert "סיכום תלמידים" in wb.sheetnames
    ws = wb["לוז שבועי"]
    assert ws.views.sheetView[0].rightToLeft is True


def test_distance_clustering_prefers_closer_students():
    from scheduler import proximity_score

    # Two students in Ramla ~0.3 km apart
    ramla_a = {"area": "רמלה", "lat": 31.9292, "lng": 34.8656}
    ramla_b = {"area": "רמלה", "lat": 31.9310, "lng": 34.8670}
    # Student in Ness Ziona ~6.5 km away
    ness_ziona = {"area": "נס ציונה", "lat": 31.9293, "lng": 34.7987}

    assert proximity_score(ramla_a, ramla_b) > proximity_score(ramla_a, ness_ziona)


def test_never_schedules_more_than_two_lessons_per_day():
    settings = {
        "lesson_duration_min": 40,
        "lunch_mode": "flexible",
        "lunch_window_start": "12:30",
        "lunch_window_end": "15:00",
        "lunch_fixed_start": "13:00",
        "work_hours": {
            "0": {"enabled": True, "start": "08:00", "end": "12:00"},
            "1": {"enabled": True, "start": "08:00", "end": "12:00"},
            "2": {"enabled": False, "start": "08:00", "end": "12:00"},
            "3": {"enabled": False, "start": "08:00", "end": "12:00"},
            "4": {"enabled": False, "start": "08:00", "end": "12:00"},
            "5": {"enabled": False, "start": "08:00", "end": "12:00"},
        },
    }
    students = [{"id": 1, "name": "עומר", "phone": "0509", "area": "רמלה", "status": "active"}]
    constraints = {1: {"requested_lessons": 3, "prefer_double": True, "blocked_slots": []}}

    res = run_smart_schedule(
        settings=settings,
        students=students,
        constraints_by_student=constraints,
        instructor_blocks=[],
        locked_lessons=[],
    )

    per_day_units: dict[int, int] = {}
    for l in res["lessons"]:
        per_day_units[l["day"]] = per_day_units.get(l["day"], 0) + (2 if l["is_double"] else 1)

    assert sum(per_day_units.values()) == 3
    assert all(cnt <= 2 for cnt in per_day_units.values())
