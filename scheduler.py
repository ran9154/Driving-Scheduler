import math
import random
from dataclasses import dataclass
from typing import Any


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def proximity_score(st_a: dict[str, Any], st_b: dict[str, Any]) -> float:
    """Score consecutive lessons by geographic distance if coordinates exist, else by area name."""
    if st_a.get("lat") is not None and st_a.get("lng") is not None and st_b.get("lat") is not None and st_b.get("lng") is not None:
        dist = haversine_km(st_a["lat"], st_a["lng"], st_b["lat"], st_b["lng"])
        if dist <= 1.0:
            return 200.0
        if dist <= 3.0:
            return 120.0
        if dist <= 6.0:
            return 20.0
        return -min(100.0, dist * 8.0)
    return 150.0 if st_a.get("area") == st_b.get("area") else -80.0


def time_to_min(t: str) -> int:
    hh, mm = map(int, t.split(":"))
    return hh * 60 + mm


def min_to_time(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


@dataclass(frozen=True)
class Slot:
    index: int
    day: int
    start_time: str
    start_min: int
    end_min: int

    @property
    def key(self) -> str:
        return f"{self.day}:{self.start_time}"


def generate_week_slots(settings: dict[str, Any]) -> list[Slot]:
    duration = int(settings["lesson_duration_min"])
    work_hours = settings["work_hours"]
    slots: list[Slot] = []
    idx = 0
    for day in range(6):
        day_cfg = work_hours.get(str(day), {})
        if not day_cfg.get("enabled", False):
            continue
        start_m = time_to_min(day_cfg["start"])
        end_m = time_to_min(day_cfg["end"])
        cur = start_m
        while cur + duration <= end_m:
            slots.append(
                Slot(
                    index=idx,
                    day=day,
                    start_time=min_to_time(cur),
                    start_min=cur,
                    end_min=cur + duration,
                )
            )
            idx += 1
            cur += duration
    return slots


def intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def has_valid_lunch_gap(
    day: int,
    occupied: list[tuple[int, int]],
    lunch_start: int,
    lunch_end: int,
    required_min: int = 60,
) -> bool:
    """Check if day (Sun-Thu) still has at least `required_min` consecutive free minutes inside lunch window."""
    if day == 5:  # Friday has no lunch requirement
        return True
    relevant = sorted(
        (max(s, lunch_start), min(e, lunch_end))
        for s, e in occupied
        if intervals_overlap(s, e, lunch_start, lunch_end)
    )
    cursor = lunch_start
    for s, e in relevant:
        if s - cursor >= required_min:
            return True
        cursor = max(cursor, e)
    return (lunch_end - cursor) >= required_min


def run_smart_schedule(
    settings: dict[str, Any],
    students: list[dict[str, Any]],
    constraints_by_student: dict[int, dict[str, Any]],
    instructor_blocks: list[dict[str, Any]],
    locked_lessons: list[dict[str, Any]],
    seed: int = 42,
) -> dict[str, Any]:
    rng = random.Random(seed)
    slots = generate_week_slots(settings)
    slot_by_key = {s.key: s for s in slots}
    duration = int(settings["lesson_duration_min"])

    lunch_mode = settings.get("lunch_mode", "flexible")
    lunch_win_start = time_to_min(settings.get("lunch_window_start", "12:30"))
    lunch_win_end = time_to_min(settings.get("lunch_window_end", "15:00"))
    lunch_fixed_start = time_to_min(settings.get("lunch_fixed_start", "13:00"))

    # Precompute blocked slot indices from instructor_blocks (+ fixed lunch)
    instructor_blocked_slots: set[int] = set()
    day_block_intervals: dict[int, list[tuple[int, int]]] = {d: [] for d in range(6)}

    for blk in instructor_blocks:
        d = int(blk["day"])
        bs = time_to_min(blk["start_time"])
        be = time_to_min(blk["end_time"])
        day_block_intervals[d].append((bs, be))
        for sl in slots:
            if sl.day == d and intervals_overlap(sl.start_min, sl.end_min, bs, be):
                instructor_blocked_slots.add(sl.index)

    if lunch_mode == "fixed":
        for d in range(5):
            ls, le = lunch_fixed_start, lunch_fixed_start + 60
            day_block_intervals[d].append((ls, le))
            for sl in slots:
                if sl.day == d and intervals_overlap(sl.start_min, sl.end_min, ls, le):
                    instructor_blocked_slots.add(sl.index)

    # Active students map
    student_map = {s["id"]: s for s in students if s["status"] == "active"}

    # Track occupied slots: slot_index -> assignment_id
    # Assignment: {id, student_id, slot_indices: tuple[int, ...], is_double: bool, is_locked: bool}
    assignments: list[dict[str, Any]] = []
    slot_owner: dict[int, int] = {}  # slot_idx -> assignment idx

    # Place locked lessons first
    student_lesson_count: dict[int, int] = {sid: 0 for sid in student_map}
    for locked in locked_lessons:
        sid = locked["student_id"]
        if sid not in student_map:
            continue
        key = f"{locked['day']}:{locked['start_time']}"
        sl = slot_by_key.get(key)
        if not sl:
            continue
        is_double = bool(locked.get("is_double", False))
        sl_indices = [sl.index]
        if is_double:
            next_idx = sl.index + 1
            if next_idx < len(slots) and slots[next_idx].day == sl.day and slots[next_idx].start_min == sl.end_min:
                sl_indices.append(next_idx)
        a_idx = len(assignments)
        assignments.append(
            {
                "student_id": sid,
                "slot_indices": tuple(sl_indices),
                "is_double": len(sl_indices) == 2,
                "is_locked": True,
            }
        )
        for idx in sl_indices:
            slot_owner[idx] = a_idx
        student_lesson_count[sid] += len(sl_indices)

    # Build student blocked slot sets and requests
    student_blocked: dict[int, set[str]] = {}
    requests: list[dict[str, Any]] = []

    for sid, st in student_map.items():
        c = constraints_by_student.get(sid)
        if not c:
            # Student hasn't submitted yet -> default 1 lesson, no blocked slots? Or skip?
            # If they haven't submitted, don't auto-guess unless they submitted; wait:
            # Better: if no submission, skip (or instructor can add manually).
            continue
        req_count = int(c.get("requested_lessons", 1))
        if req_count <= 0:
            continue
        already = student_lesson_count.get(sid, 0)
        remaining = max(0, req_count - already)
        if remaining == 0:
            continue
        blocked = set(c.get("blocked_slots", []))
        student_blocked[sid] = blocked
        prefer_double = bool(c.get("prefer_double", False))

        # Count available slots for MRV ordering
        avail_count = sum(
            1 for sl in slots
            if sl.index not in instructor_blocked_slots
            and sl.index not in slot_owner
            and sl.key not in blocked
        )

        if prefer_double and remaining >= 2:
            requests.append({
                "student_id": sid,
                "units": 2,
                "is_double": True,
                "order": 1,
                "avail_count": avail_count,
            })
            remaining -= 2

        for k in range(remaining):
            requests.append({
                "student_id": sid,
                "units": 1,
                "is_double": False,
                "order": already + k + 1,
                "avail_count": avail_count,
            })

    # Sort: 1st lesson for each student first, then tightest availability first
    requests.sort(key=lambda r: (r["order"] > 1, r["avail_count"], r["student_id"]))

    def day_occupied_intervals(day: int, exclude_a_idx: int | None = None) -> list[tuple[int, int]]:
        occ = list(day_block_intervals[day])
        for i, a in enumerate(assignments):
            if i == exclude_a_idx or a is None:
                continue
            for s_idx in a["slot_indices"]:
                sl = slots[s_idx]
                if sl.day == day:
                    occ.append((sl.start_min, sl.end_min))
        return occ

    def can_place(sid: int, slot_indices: tuple[int, ...], exclude_a_idx: int | None = None) -> bool:
        blocked = student_blocked.get(sid, set())
        day = slots[slot_indices[0]].day
        for s_idx in slot_indices:
            if s_idx in instructor_blocked_slots:
                return False
            owner = slot_owner.get(s_idx)
            if owner is not None and owner != exclude_a_idx:
                return False
            if slots[s_idx].key in blocked:
                return False
        if lunch_mode == "flexible" and day < 5:
            occ = day_occupied_intervals(day, exclude_a_idx=exclude_a_idx)
            for s_idx in slot_indices:
                occ.append((slots[s_idx].start_min, slots[s_idx].end_min))
            if not has_valid_lunch_gap(day, occ, lunch_win_start, lunch_win_end, 60):
                return False

        # Never allow a student to have more than 2 lesson units on the same day
        student_day_slots = set(slot_indices)
        for i, a in enumerate(assignments):
            if i == exclude_a_idx or a is None or a["student_id"] != sid:
                continue
            for s_idx in a["slot_indices"]:
                if slots[s_idx].day == day:
                    student_day_slots.add(s_idx)
        if len(student_day_slots) > 2:
            return False
        return True

    def score_current() -> float:
        score = 0.0
        per_student_units: dict[int, int] = {}
        per_student_days: dict[int, list[int]] = {}
        per_day_lessons: dict[int, list[tuple[int, int, int]]] = {d: [] for d in range(6)}

        for a in assignments:
            if a is None:
                continue
            sid = a["student_id"]
            units = len(a["slot_indices"])
            per_student_units[sid] = per_student_units.get(sid, 0) + units
            day = slots[a["slot_indices"][0]].day
            per_student_days.setdefault(sid, []).append(day)
            if a["is_double"]:
                score += 120.0
            for s_idx in a["slot_indices"]:
                sl = slots[s_idx]
                per_day_lessons[day].append((sl.start_min, sl.end_min, sid))

        for sid, units in per_student_units.items():
            if units >= 1:
                score += 1000.0
                score += (units - 1) * 600.0

        # Penalize same student having multiple separate lessons on the same day
        for sid, days in per_student_days.items():
            if len(days) != len(set(days)):
                score -= 100.0 * (len(days) - len(set(days)))

        # Day-level scoring: area clustering + minimizing dead holes
        for d in range(6):
            items = sorted(per_day_lessons[d], key=lambda x: x[0])
            if len(items) <= 1:
                continue
            for i in range(len(items) - 1):
                cur_start, cur_end, cur_sid = items[i]
                nxt_start, nxt_end, nxt_sid = items[i + 1]
                gap = nxt_start - cur_end
                if gap == 0:
                    score += proximity_score(student_map[cur_sid], student_map[nxt_sid])
                else:
                    # Allow 1 lunch gap up to 80 min in flexible mode without heavy penalty
                    if d < 5 and lunch_mode == "flexible" and gap <= 80 and cur_end >= lunch_win_start and nxt_start <= lunch_win_end:
                        score -= 10.0
                    else:
                        holes = max(1, gap // duration)
                        score -= 120.0 * holes
        return score

    # Candidate slot pairs for double lessons
    double_pairs: list[tuple[int, int]] = []
    for i in range(len(slots) - 1):
        if slots[i].day == slots[i + 1].day and slots[i].end_min == slots[i + 1].start_min:
            double_pairs.append((i, i + 1))

    # Phase 1: Greedy placement
    unplaced_requests: list[dict[str, Any]] = []
    for req in requests:
        sid = req["student_id"]
        best_choice: tuple[int, ...] | None = None
        best_score = float("-inf")

        candidates: list[tuple[int, ...]] = (
            double_pairs if req["is_double"] else [(sl.index,) for sl in slots]
        )

        for cand in candidates:
            if not can_place(sid, cand):
                continue
            a_idx = len(assignments)
            assignments.append(
                {
                    "student_id": sid,
                    "slot_indices": cand,
                    "is_double": req["is_double"],
                    "is_locked": False,
                }
            )
            for idx in cand:
                slot_owner[idx] = a_idx
            sc = score_current()
            if sc > best_score:
                best_score = sc
                best_choice = cand
            # rollback
            for idx in cand:
                del slot_owner[idx]
            assignments.pop()

        if best_choice is not None:
            a_idx = len(assignments)
            assignments.append(
                {
                    "student_id": sid,
                    "slot_indices": best_choice,
                    "is_double": req["is_double"],
                    "is_locked": False,
                }
            )
            for idx in best_choice:
                slot_owner[idx] = a_idx
        else:
            # If double failed, try splitting into 2 singles
            if req["is_double"]:
                unplaced_requests.append({**req, "is_double": False, "units": 1})
                unplaced_requests.append({**req, "is_double": False, "units": 1})
            else:
                unplaced_requests.append(req)

    # Try placing any split singles
    still_unplaced: list[dict[str, Any]] = []
    for req in unplaced_requests:
        sid = req["student_id"]
        best_choice = None
        best_score = float("-inf")
        for sl in slots:
            cand = (sl.index,)
            if not can_place(sid, cand):
                continue
            a_idx = len(assignments)
            assignments.append({"student_id": sid, "slot_indices": cand, "is_double": False, "is_locked": False})
            slot_owner[sl.index] = a_idx
            sc = score_current()
            if sc > best_score:
                best_score = sc
                best_choice = cand
            del slot_owner[sl.index]
            assignments.pop()
        if best_choice is not None:
            a_idx = len(assignments)
            assignments.append({"student_id": sid, "slot_indices": best_choice, "is_double": False, "is_locked": False})
            slot_owner[best_choice[0]] = a_idx
        else:
            still_unplaced.append(req)

    # Phase 2: Local Search (Swap & Move)
    current_best = score_current()
    movable_indices = [i for i, a in enumerate(assignments) if a is not None and not a["is_locked"]]
    if movable_indices:
        for _ in range(1200):
            if rng.random() < 0.5 or len(movable_indices) < 2:
                # Try moving one assignment
                a_idx = rng.choice(movable_indices)
                a = assignments[a_idx]
                old_slots = a["slot_indices"]
                cand = rng.choice(double_pairs) if a["is_double"] else (rng.randrange(len(slots)),)
                if cand == old_slots or not can_place(a["student_id"], cand, exclude_a_idx=a_idx):
                    continue
                for idx in old_slots:
                    del slot_owner[idx]
                a["slot_indices"] = cand
                for idx in cand:
                    slot_owner[idx] = a_idx
                new_sc = score_current()
                if new_sc > current_best:
                    current_best = new_sc
                else:
                    for idx in cand:
                        del slot_owner[idx]
                    a["slot_indices"] = old_slots
                    for idx in old_slots:
                        slot_owner[idx] = a_idx
            else:
                # Try swapping two single-slot assignments
                i1, i2 = rng.sample(movable_indices, 2)
                a1, a2 = assignments[i1], assignments[i2]
                if a1["is_double"] or a2["is_double"] or a1["student_id"] == a2["student_id"]:
                    continue
                s1, s2 = a1["slot_indices"], a2["slot_indices"]
                # Temporarily free both
                del slot_owner[s1[0]]
                del slot_owner[s2[0]]
                ok1 = can_place(a1["student_id"], s2, exclude_a_idx=i1)
                ok2 = can_place(a2["student_id"], s1, exclude_a_idx=i2)
                if ok1 and ok2:
                    a1["slot_indices"], a2["slot_indices"] = s2, s1
                    slot_owner[s2[0]] = i1
                    slot_owner[s1[0]] = i2
                    new_sc = score_current()
                    if new_sc > current_best:
                        current_best = new_sc
                    else:
                        a1["slot_indices"], a2["slot_indices"] = s1, s2
                        slot_owner[s1[0]] = i1
                        slot_owner[s2[0]] = i2
                else:
                    slot_owner[s1[0]] = i1
                    slot_owner[s2[0]] = i2

    # Build output lessons
    result_lessons: list[dict[str, Any]] = []
    scheduled_counts: dict[int, int] = {}
    for a in assignments:
        if a is None:
            continue
        sl = slots[a["slot_indices"][0]]
        sid = a["student_id"]
        units = len(a["slot_indices"])
        scheduled_counts[sid] = scheduled_counts.get(sid, 0) + units
        result_lessons.append(
            {
                "day": sl.day,
                "start_time": sl.start_time,
                "duration_min": duration * units,
                "student_id": sid,
                "is_locked": a["is_locked"],
                "is_double": a["is_double"],
            }
        )

    result_lessons.sort(key=lambda x: (x["day"], x["start_time"]))

    # Build unscheduled summary
    unscheduled: list[dict[str, Any]] = []
    for sid, st in student_map.items():
        c = constraints_by_student.get(sid)
        if not c:
            continue
        req = int(c.get("requested_lessons", 1))
        got = scheduled_counts.get(sid, 0)
        if got < req:
            unscheduled.append(
                {
                    "student_id": sid,
                    "name": st["name"],
                    "area": st["area"],
                    "requested": req,
                    "scheduled": got,
                    "reason": "אילוצים חופפים או עומס בשעות הפנויות",
                }
            )

    # Compute clustering & hole stats
    same_area_transitions = 0
    total_transitions = 0
    for d in range(6):
        day_ls = [l for l in result_lessons if l["day"] == d]
        for i in range(len(day_ls) - 1):
            total_transitions += 1
            if student_map[day_ls[i]["student_id"]]["area"] == student_map[day_ls[i + 1]["student_id"]]["area"]:
                same_area_transitions += 1

    return {
        "lessons": result_lessons,
        "unscheduled": unscheduled,
        "stats": {
            "total_lessons": sum(2 if l["is_double"] else 1 for l in result_lessons),
            "students_scheduled": len(scheduled_counts),
            "same_area_transitions": same_area_transitions,
            "total_transitions": total_transitions,
            "score": round(current_best, 1),
        },
    }
