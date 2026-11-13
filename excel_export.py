from datetime import date, timedelta
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from scheduler import generate_week_slots, intervals_overlap, time_to_min

DAY_NAMES = ["יום א'", "יום ב'", "יום ג'", "יום ד'", "יום ה'", "יום ו'"]

AREA_PALETTE = ["E0F2FE", "DCFCE7", "FEF3C7", "F3E8FF", "FCE7F3", "FFEDD5", "CCFBF1", "E0E7FF"]


def get_area_color(area: str) -> str:
    if not area:
        return "EFF6FF"
    idx = sum(ord(c) for c in area) % len(AREA_PALETTE)
    return AREA_PALETTE[idx]


def build_schedule_excel(
    week_start: str,
    settings: dict[str, Any],
    students_by_id: dict[int, dict[str, Any]],
    lessons: list[dict[str, Any]],
    instructor_blocks: list[dict[str, Any]],
    constraints_by_student: dict[int, dict[str, Any]],
) -> BytesIO:
    wb = Workbook()

    # Sheet 1: Weekly Grid
    ws = wb.active
    ws.title = "לוז שבועי"
    ws.views.sheetView[0].rightToLeft = True

    thin = Side(border_style="thin", color="CBD5E1")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill("solid", fgColor="1E293B")
    header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    time_fill = PatternFill("solid", fgColor="F1F5F9")
    time_font = Font(name="Arial", size=10, bold=True, color="334155")
    block_fill = PatternFill("solid", fgColor="FEE2E2")
    test_fill = PatternFill("solid", fgColor="FFEDD5")

    sunday = date.fromisoformat(week_start)

    # Title row
    ws.merge_cells("A1:G1")
    title_cell = ws["A1"]
    title_cell.value = f"לוז שבועי - שבוע {(sunday).strftime('%d/%m/%Y')} עד {(sunday + timedelta(days=5)).strftime('%d/%m/%Y')}"
    title_cell.font = Font(name="Arial", size=14, bold=True, color="0F172A")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28

    # Header row
    headers = ["שעה"]
    for d in range(6):
        day_dt = sunday + timedelta(days=d)
        headers.append(f"{DAY_NAMES[d]} ({day_dt.strftime('%d/%m')})")

    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
    ws.row_dimensions[2].height = 24

    # Early morning tests (before 08:00) summary row if any
    early_blocks = [b for b in instructor_blocks if time_to_min(b["start_time"]) < time_to_min("08:00")]
    row_offset = 3
    if early_blocks:
        c0 = ws.cell(row=row_offset, column=1, value="מוקדם (לפני 08:00)")
        c0.fill = time_fill
        c0.font = time_font
        c0.alignment = Alignment(horizontal="center", vertical="center")
        c0.border = border
        for d in range(6):
            day_early = [b for b in early_blocks if b["day"] == d]
            cell = ws.cell(row=row_offset, column=d + 2)
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            if day_early:
                lines = [f"{b['start_time']}-{b['end_time']} | {b['title'] or 'טסט/חסימה'}" for b in day_early]
                cell.value = "\n".join(lines)
                cell.fill = test_fill
                cell.font = Font(name="Arial", size=9, bold=True, color="9A3412")
        ws.row_dimensions[row_offset].height = 34
        row_offset += 1

    # Unique ordered start times across the week
    slots = generate_week_slots(settings)
    unique_times = sorted({s.start_time for s in slots})
    duration = int(settings["lesson_duration_min"])

    # Index lessons by (day, start_time) and track second slot of double lessons
    lesson_map: dict[tuple[int, str], dict[str, Any]] = {}
    double_cont_map: dict[tuple[int, str], dict[str, Any]] = {}
    for ls in lessons:
        lesson_map[(ls["day"], ls["start_time"])] = ls
        if ls["is_double"]:
            next_m = time_to_min(ls["start_time"]) + duration
            next_t = f"{next_m // 60:02d}:{next_m % 60:02d}"
            double_cont_map[(ls["day"], next_t)] = ls

    for idx, t in enumerate(unique_times):
        r = row_offset + idx
        t_start_m = time_to_min(t)
        t_end_m = t_start_m + duration
        t_end_str = f"{t_end_m // 60:02d}:{t_end_m % 60:02d}"

        tc = ws.cell(row=r, column=1, value=f"{t} - {t_end_str}")
        tc.fill = time_fill
        tc.font = time_font
        tc.alignment = Alignment(horizontal="center", vertical="center")
        tc.border = border

        ws.row_dimensions[r].height = 40

        for d in range(6):
            cell = ws.cell(row=r, column=d + 2)
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

            # Check if slot exists for this day
            day_has_slot = any(s.day == d and s.start_time == t for s in slots)
            if not day_has_slot:
                cell.fill = PatternFill("solid", fgColor="E2E8F0")
                continue

            ls = lesson_map.get((d, t))
            if ls:
                st = students_by_id.get(ls["student_id"], {})
                name = st.get("name", f"תלמיד #{ls['student_id']}")
                area = st.get("area", "")
                phone = st.get("phone", "")
                double_tag = " (כפול)" if ls["is_double"] else ""
                cell.value = f"{name}{double_tag}\n{area} | {phone}"
                color = get_area_color(area)
                cell.fill = PatternFill("solid", fgColor=color)
                cell.font = Font(name="Arial", size=10, bold=True, color="0F172A")
                continue

            cont = double_cont_map.get((d, t))
            if cont:
                st = students_by_id.get(cont["student_id"], {})
                cell.value = f"↑ המשך שיעור כפול ({st.get('name', '')})"
                color = get_area_color(st.get("area", ""))
                cell.fill = PatternFill("solid", fgColor=color)
                cell.font = Font(name="Arial", size=9, italic=True, color="475569")
                continue

            # Check instructor blocks
            overlapping_blocks = [
                b for b in instructor_blocks
                if b["day"] == d and intervals_overlap(t_start_m, t_end_m, time_to_min(b["start_time"]), time_to_min(b["end_time"]))
            ]
            if overlapping_blocks:
                b = overlapping_blocks[0]
                label = "טסט: " if b["block_type"] == "test" else ""
                cell.value = f"{label}{b['title'] or 'חסום'}"
                cell.fill = test_fill if b["block_type"] == "test" else block_fill
                cell.font = Font(name="Arial", size=9, bold=True, color="991B1B")

    ws.column_dimensions["A"].width = 16
    for col_idx in range(2, 8):
        ws.column_dimensions[get_column_letter(col_idx)].width = 24

    # Sheet 2: Student Summary
    ws2 = wb.create_sheet("סיכום תלמידים")
    ws2.views.sheetView[0].rightToLeft = True

    sum_headers = ["שם תלמיד", "טלפון", "אזור איסוף", "מבוקש", "שובץ", "שעות משובצות"]
    for col_idx, h in enumerate(sum_headers, start=1):
        cell = ws2.cell(row=1, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
    ws2.row_dimensions[1].height = 24

    lessons_by_student: dict[int, list[dict[str, Any]]] = {}
    for ls in lessons:
        lessons_by_student.setdefault(ls["student_id"], []).append(ls)

    row_i = 2
    for sid, st in sorted(students_by_id.items(), key=lambda x: x[1]["name"]):
        if st["status"] != "active" and sid not in lessons_by_student:
            continue
        c = constraints_by_student.get(sid)
        req = int(c["requested_lessons"]) if c else 0
        st_lessons = lessons_by_student.get(sid, [])
        got = sum(2 if l["is_double"] else 1 for l in st_lessons)
        times_str = ", ".join(
            f"{DAY_NAMES[l['day']]} {l['start_time']}" + (" (כפול)" if l["is_double"] else "")
            for l in sorted(st_lessons, key=lambda x: (x["day"], x["start_time"]))
        )
        vals = [st["name"], st["phone"], st["area"], req, got, times_str or "-"]
        for col_idx, val in enumerate(vals, start=1):
            cell = ws2.cell(row=row_i, column=col_idx, value=val)
            cell.border = border
            cell.font = Font(name="Arial", size=10)
            cell.alignment = Alignment(horizontal="center" if col_idx < 6 else "right", vertical="center")
        row_i += 1

    for col_idx, width in enumerate([20, 16, 14, 10, 10, 42], start=1):
        ws2.column_dimensions[get_column_letter(col_idx)].width = width

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
