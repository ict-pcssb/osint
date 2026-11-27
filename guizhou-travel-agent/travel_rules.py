"""Versioned, read-only itinerary validation. The rule engine performs no I/O."""
from datetime import date, datetime, time, timedelta
from urllib.parse import quote

from travel_contract import ContractError, REPLY, check
from travel_fares import nonnegative, summarize_costs
from travel_state import BEIJING

POLICY_VERSION = "m06.1"
READABLE_VERSIONS = frozenset({"m06.1"})
SEGMENTS = ("exit", "wait", "ride", "transfer", "walk")
REPORT = REPLY.evolve(schema={"$defs":REPLY.schema["$defs"], "$ref":"#/$defs/validation_report"})


def instant(value):
    """Interpret a legacy explicit local date in Beijing; never infer a missing date."""
    if not isinstance(value, str) or "T" not in value:
        return None, False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, False
    legacy = parsed.utcoffset() is None
    return (parsed.replace(tzinfo=BEIJING) if legacy else parsed.astimezone(BEIJING)), legacy


def clock_on(day, wall):
    try:
        parsed = time.fromisoformat(wall)
        if parsed.tzinfo is None:
            return datetime.combine(date.fromisoformat(day), parsed, BEIJING)
    except (TypeError, ValueError):
        pass
    return None


def format_time(when):
    return when.isoformat(timespec="seconds")


def path_id(value):
    return quote(str(value), safe="-_.")


class EvidenceIndex:
    """Consume M5 claim/evidence packets without making fresh claims from search snippets."""

    def __init__(self, packets, checked_at, mode):
        self.packets = tuple(packets)
        self.checked_at = instant(checked_at)[0]
        self.mode = mode

    def fact(self, kind, subject, day, direction=None, fixture_value=None):
        matches = []
        for packet in self.packets:
            claim = packet.get("claim", {})
            if (claim.get("claim_type"), claim.get("subject_id"), claim.get("target_date"),
                    claim.get("line_direction")) != (kind, subject, day, direction):
                continue
            simulated = self.mode == "fixture" and claim.get("simulation_status") == "confirmed"
            genuine = (claim.get("status") in {"confirmed", "contradicted"}
                       and claim.get("confidence_basis") == "official_original")
            if not (simulated or genuine):
                continue
            value = fixture_value if simulated else claim.get("value")
            if value is None:
                continue
            rows = {row["evidence_id"]: row for row in packet.get("evidence", ())}
            refs = [ref for ref in claim.get("evidence_ids", ()) if ref in rows
                    and self._usable(rows[ref], day, simulated)]
            if refs:
                matches.append((value, refs))
        if matches and all(value == matches[0][0] for value, _ in matches):
            return matches[0][0], sorted({ref for _, refs in matches for ref in refs})
        return None, []

    def _usable(self, row, day, simulated):
        expiry = instant(row.get("expires_at"))[0]
        collected = instant(row.get("retrieved_at"))[0]
        starts, ends = row.get("effective_from"), row.get("effective_to")
        return (row.get("data_mode") in ({"fixture"} if simulated else {"live", "snapshot"})
                and row.get("source_kind") in {"official", "operator"}
                and row.get("freshness") == "current" and collected is not None
                and self.checked_at is not None and expiry is not None
                and collected <= self.checked_at < expiry
                and isinstance(starts, str) and starts <= day and (ends is None or day <= ends))


def entity_rule(stop, emit):
    leg = stop.get("incoming_leg", {})
    entrance = stop.get("entrance_id")
    refs = leg.get("evidence_ids", [])
    if leg.get("reason") == "endpoint_mismatch":
        emit("entity", "fail", "ENTITY_MISMATCH", "/incoming_leg", refs, "返回的路段接错站或入口。请重新核对路线。")
        return False
    if not entrance or not leg.get("from_id") or not leg.get("to_id"):
        emit("entity", "pending", "ENTRANCE_REQUIRED", "/entrance_id", refs, "请确认出发站和到访入口。")
        return False
    checks = (("/incoming_leg/to_id", leg["to_id"], entrance),
              ("/incoming_leg/from_id", leg["from_id"], leg.get("expected_from_id")),
              ("/reservation/entrance_id", (stop.get("reservation") or {}).get("entrance_id"), entrance))
    mismatches = [field for field, actual, expected in checks if actual and expected and actual != expected]
    if mismatches:
        for field in mismatches:
            emit("entity", "fail", "ENTITY_MISMATCH", field, refs, "请选择与车站、路线和预约一致的具体入口。")
        return False
    if stop.get("reservation") and not stop["reservation"].get("entrance_id"):
        emit("entity", "pending", "ENTRANCE_REQUIRED", "/reservation/entrance_id", refs, "请核对预约使用的入口。")
        return False
    coordinates_ok = leg.get("coordinate_system") == "WGS84"
    for field in ("from_coordinates", "to_coordinates"):
        if field in leg:
            coords = leg[field]
            coordinates_ok &= (isinstance(coords, (list, tuple)) and len(coords) == 2
                               and all(type(axis) in (int, float) for axis in coords)
                               and -90 <= coords[0] <= 90 and -180 <= coords[1] <= 180)
    if not coordinates_ok:
        emit("entity", "pending", "COORDINATE_SYSTEM_UNKNOWN", "/incoming_leg/coordinate_system", refs,
             "请核对 WGS84 坐标及纬度、经度顺序。")
        return False
    emit("entity", "pass", "ENTITY_MATCHED", "/entrance_id", refs, "路线与入口 ID 一致。")
    return True


def timing_rule(leg, emit, details):
    refs = leg.get("evidence_ids", [])
    start, legacy = instant(leg.get("starts_at"))
    if start is None:
        emit("time", "pending", "TIME_UNVERIFIED", "/incoming_leg/starts_at", refs, "请补充出发日期与时间。")
        return None
    if legacy:
        details["legacy_beijing_time"] = True
    parts = leg.get("segments_min")
    bounds, estimate = None, leg.get("status") == "estimate"
    if parts is not None:
        parts = dict(parts)
        if set(parts) - set(SEGMENTS) or any(val is not None and not nonnegative(val) for val in parts.values()):
            emit("time", "fail", "INVALID_DURATION", "/incoming_leg/segments_min", refs, "请修正无效或重复的分段时长。")
            return None
        if parts.get("wait") is None:
            bounds = leg.get("wait_estimate_min")
            if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                    or not all(nonnegative(v) for v in bounds) or bounds[0] > bounds[1]):
                emit("time", "pending", "WAIT_TIME_UNVERIFIED", "/incoming_leg/segments_min/wait", refs, "请核实候车时长。")
                return None
            parts["wait"] = bounds[1]
            estimate = True
        if not all(nonnegative(parts.get(key)) for key in SEGMENTS):
            emit("time", "pending", "DURATION_UNVERIFIED", "/incoming_leg/segments_min", refs, "请补齐出站、乘车、换乘与步行耗时。")
            return None
        duration = sum(parts.values())
        details["segments_min"] = parts
    else:
        duration = leg.get("travel_min")
    if duration is None:
        emit("time", "pending", "DURATION_UNVERIFIED", "/incoming_leg/travel_min", refs, "请核实路段耗时。")
        return None
    if not nonnegative(duration):
        emit("time", "fail", "INVALID_DURATION", "/incoming_leg/travel_min", refs, "请修正负数或非整数路段时长。")
        return None
    try:
        arrival = start + timedelta(minutes=duration)
    except OverflowError:
        emit("time", "fail", "INVALID_DURATION", "/incoming_leg/travel_min", refs, "路段时长超出可表示范围。")
        return None
    details.update(arrival_at=format_time(arrival), timing_status="estimate" if estimate else "calculated")
    if bounds:
        details["arrival_range"] = [format_time(arrival - timedelta(minutes=bounds[1]-bounds[0])), format_time(arrival)]
        details["wait_estimate_min"] = list(bounds)
        emit("time", "pending", "WAIT_TIME_ESTIMATED", "/incoming_leg/segments_min/wait", refs,
             "候车按配置区间预留，请以实际班次复核。")
    return arrival


def reservation_rule(stop, arrival, evidence, emit, details):
    res = stop["reservation"]
    day = stop["date"]
    cutoff, legacy = instant(res.get("cutoff_at"))
    buffer = res.get("buffer_min")
    refs = res.get("evidence_ids", [])
    if not res.get("cutoff_confirmed"):
        fact, refs = evidence.fact("reservation_cutoff", res.get("entrance_id"), day,
                                   fixture_value=res.get("cutoff_at"))
        cutoff, legacy = instant(fact)
        if cutoff is None:
            cutoff = clock_on(day, fact)
    if legacy:
        details["legacy_beijing_time"] = True
    if not (res.get("entrance_id") == stop.get("entrance_id") and cutoff and arrival and cutoff.date().isoformat() == day
            and nonnegative(buffer) and res.get("buffer_source")):
        emit("reservation", "pending", "RESERVATION_UNVERIFIED", "/reservation", refs, "请核对预约入口、入场截止与预留时间。")
        return
    safe = cutoff - timedelta(minutes=buffer)
    details["latest_safe_arrival"] = format_time(safe)
    if arrival > safe:
        gap = (arrival - safe).total_seconds()
        gap_min = int((gap + 59) // 60)
        details["reservation_gap_min"] = gap_min
        emit("reservation", "fail", "RESERVATION_WINDOW_MISSED", "/reservation/cutoff_at", refs,
             f"预计晚 {gap_min} 分钟。请选择提前出发或自行确认改约。")
    else:
        emit("reservation", "pass", "RESERVATION_WINDOW_MET", "/reservation/cutoff_at", refs, "按现有时段保留入场缓冲。")


def after_last_service(day, boarding, value):
    last = clock_on(day, value)
    return boarding > last if last else None


def opening_status(value):
    return {"open": "pass", "closed": "fail"}.get(value, "pending")


def last_service_rule(stop, evidence, emit, details):
    service = stop["last_service"]
    board, alight, direction = (service.get(key) for key in ("boarding_id", "alighting_id", "direction"))
    details["last_service"] = {key: service.get(key) for key in
                               ("boarding_id", "alighting_id", "direction", "estimate_min", "estimate_mode")}
    boarding, legacy = instant(service.get("boarding_at"))
    if legacy:
        details["legacy_beijing_time"] = True
    expected_board = stop.get("incoming_leg", {}).get("from_id")
    if (board and alight and direction and boarding and alight == stop.get("entrance_id")
            and board == expected_board and boarding.date().isoformat() == stop["date"]):
        value, refs = evidence.fact("last_service", service.get("subject_id", alight), boarding.date().isoformat(),
                                    direction, service.get("fixture_last_departure"))
        last = clock_on(boarding.date().isoformat(), value)
        if last:
            details["last_service"]["last_departure_at"] = format_time(last)
            after = after_last_service(boarding.date().isoformat(), boarding, value)
            emit("last_service", "fail" if after else "pass",
                 "LAST_SERVICE_MISSED" if after else "LAST_SERVICE_CHECKED",
                 "/last_service/boarding_at", refs,
                 "请选择提前到站或改用其他交通方式。" if after else "上车时刻未超过已核对的末班。")
            return
    emit("last_service", "pending", "LAST_SERVICE_UNVERIFIED", "/last_service", [],
         "请核对该上车点和方向的末班。可自行选择早一班车或其他交通方式。")


def check_stop(stop, evidence, put, details):
    prefix = f"/stops/{path_id(stop['stop_id'])}"

    def emit(rule_id, status, code, field, refs, action):
        put(rule_id, status, code, prefix+field, refs, action)

    leg = stop.get("incoming_leg", {})
    usable = entity_rule(stop, emit)
    arrival, legacy = instant(stop.get("arrival_at"))
    departure, leave_legacy = instant(stop.get("departure_at"))
    ends, end_legacy = instant(stop.get("ends_at"))
    if legacy or leave_legacy or end_legacy:
        details["legacy_beijing_time"] = True
    calculate = usable and any(key in leg for key in ("starts_at", "travel_min", "segments_min"))
    if calculate:
        earliest = timing_rule(leg, emit, details)
        minimum = instant(details["arrival_range"][0])[0] if "arrival_range" in details else earliest
        if minimum and arrival and arrival < minimum:
            emit("time", "fail", "TIME_CONFLICT", "/arrival_at", leg.get("evidence_ids", []),
                 f"最早约 {minimum:%m月%d日 %H:%M} 到达，请调整这一站的到达时间。")
        elif earliest:
            emit("time", "pass", "LEG_TIME_CHECKED", "/incoming_leg", leg.get("evidence_ids", []), "已计算这段路的到达时间。")
            arrival = arrival or earliest
    if (arrival and departure and departure <= arrival) or (departure and ends and departure > ends):
        emit("time", "fail", "TIME_CONFLICT", "/departure_at", [], "请调整本站停留时间或当天结束时段。")
    if stop.get("reservation"):
        reservation_rule(stop, arrival if usable else None, evidence, emit, details)
    if "last_service" in stop and usable:
        last_service_rule(stop, evidence, emit, details)
    if stop.get("check_booking"):
        booking, refs = evidence.fact("booking", stop["entrance_id"], stop["date"])
        booking_met = booking == "not_required" or bool(stop.get("reservation", {}).get("cutoff_confirmed"))
        emit("booking", "pass" if booking_met else "pending",
             "BOOKING_CHECKED" if booking_met else "BOOKING_UNVERIFIED", "/reservation", refs,
             "已核对预约条件。" if booking_met else "请核对是否需要预约。")
    if stop.get("check_opening"):
        opening, refs = evidence.fact("opening", stop["entrance_id"], stop["date"])
        status = opening_status(opening)
        emit("opening", status, {"pass":"OPENING_CHECKED", "fail":"OPENING_CLOSED", "pending":"OPENING_UNVERIFIED"}[status],
             "/opening", refs, "请核对当日开放和预约时段。" if status != "pass" else "已核对当日开放。")


def check_return(returning, emit, details):
    if returning is None:
        return
    leave, _ = instant(returning.get("leave_at"))
    departure, _ = instant(returning.get("departure_at"))
    ride, buffer = returning.get("travel_min"), returning.get("buffer_min")
    refs = returning.get("evidence_ids", [])
    if returning.get("reason") == "endpoint_mismatch":
        emit("entity", "fail", "ENTITY_MISMATCH", "/return_leg", refs, "返程路段接错站。请重新核对锁定车站。")
        return
    if leave and departure and nonnegative(buffer) and leave + timedelta(minutes=buffer + (ride if nonnegative(ride) else 0)) > departure:
        emit("return", "fail", "LOCKED_RETURN_TIME_CONFLICT", "/return_leg", refs, "无法保留锁定返程的进站缓冲。请选择提前离开。")
    if leave and nonnegative(ride):
        details["arrival_at"] = format_time(leave + timedelta(minutes=ride))
    if not (leave and departure and nonnegative(ride) and nonnegative(buffer) and returning.get("buffer_source")):
        emit("return", "pending", "RETURN_UNVERIFIED", "/return_leg", refs, "请核实返程路段及进站缓冲。")
    elif leave + timedelta(minutes=ride + buffer) <= departure:
        emit("return", "pass", "RETURN_BUFFER_MET", "/return_leg", refs, "已在返程时间前预留进站缓冲。")


def check_costs(trip, evidence, put):
    budget = trip.get("budget", {})
    verified_lines = []
    for item in trip.get("cost_lines", []):
        line = item.copy()
        key, refs = line["item_id"], line["evidence_ids"]
        if line["unit_fen"] is None and line["mandatory"]:
            put("cost", "pending", "COST_UNVERIFIED", f"/cost_lines/{path_id(key)}/unit_fen", refs,
                "请核实这项必需费用。")
        elif line["unit_fen"] is not None and trip["data_mode"] != "fixture":
            subject, day = line.get("subject_id"), line.get("target_date")
            value, available = (None, [])
            if subject and day and line["category"] == "ticket" and line["traveler_group"] == "adult":
                value, available = evidence.fact("ticket_price", subject, day)
            expected = {"currency":line["currency"], "amount_fen":line["unit_fen"]}
            if value != expected or line.get("data_mode") == "fixture" or not set(refs).intersection(available):
                if line["mandatory"]:
                    put("cost", "pending", "COST_SOURCE_UNVERIFIED", f"/cost_lines/{path_id(key)}/evidence_ids", refs,
                        "请核对价格适用日期和对应原文。")
                line.update(unit_fen=None, subtotal_fen=None, price_status="unknown", eligibility={"status":"unknown"})
        verified_lines.append(line)
    cap = budget.get("original_budget_fen", budget.get("budget_fen", trip.get("budget_fen")))
    scope = budget.get("original_budget_scope", budget.get("budget_scope", trip.get("budget_scope", "group")))
    people = budget.get("people", trip.get("travelers"))
    if people is None and "adults" in trip:
        people = trip["adults"] + trip.get("children", 0)
    summary = summarize_costs(verified_lines, cap, "per_person" if scope == "person" else scope, people)
    if summary["unconverted_currencies"]:
        put("cost", "pending", "FX_RATE_UNVERIFIED", "/cost_lines/currency", [], "请核实适用汇率。各币种金额单列。")
    if summary["budget_status"] == "over":
        put("budget", "fail", "BUDGET_EXCEEDED", "/budget/cap_fen", [], "已知费用超预算，请选择调整项目或预算。")
    return summary


def validate_itinerary(trip, evidence_set, locked_items, policy_version, *, affected_stop_ids=None, previous_report=None):
    if policy_version != POLICY_VERSION:
        raise ValueError("未启用的规则版本")
    mode = trip["data_mode"]
    if mode not in {"fixture", "snapshot", "live"}:
        raise ValueError("未知数据模式")
    evidence = EvidenceIndex(evidence_set, trip.get("checked_at"), mode)
    stops = trip["stops"]
    all_ids = [stop["stop_id"] for stop in stops]
    if len(set(all_ids)) != len(all_ids):
        raise ValueError("行程站点 ID 重复")
    partial = affected_stop_ids is not None
    affected = list(affected_stop_ids) if partial else all_ids
    if not set(affected) <= set(all_ids):
        raise ValueError("校验范围含未知站点")
    if partial:
        if not previous_report or validate_report(previous_report)["policy_version"] != policy_version:
            raise ValueError("局部校验需要同规则版本的原报告")
        if previous_report["data_mode"] != mode:
            raise ValueError("局部校验的数据来源模式不一致")
    retained = tuple(f"/stops/{path_id(key)}/" for key in all_ids if key not in affected)
    carry = previous_report if partial else {}
    results = [row.copy() for row in carry.get("rule_results", []) if row["path"].startswith(retained)]
    details = {key: val.copy() for key, val in carry.get("time_details", {}).items()
               if key in all_ids and key not in affected}

    def put(rule_id, status, code, path, refs, action):
        results.append(dict(rule_id=rule_id, status=status, reason_code=code, path=path,
                            evidence_ids=list(dict.fromkeys(refs)), suggested_action=action))

    for stop in stops:
        key = stop["stop_id"]
        if key in affected:
            details[key] = {}
            check_stop(stop, evidence, put, details[key])
    candidates = trip.get("locked_items", [])
    by_id = {item["stable_id"]: item for item in candidates}
    for locked in locked_items:
        if len(candidates) != len(by_id) or by_id.get(locked["stable_id"]) != locked:
            put("lock", "fail", "LOCKED_ITEM_CHANGED", f"/locked_items/{path_id(locked['stable_id'])}", [],
                "保留已确认安排。调整前须由游客明确确认。")
    if trip.get("return_leg") and (not partial or all_ids[-1] in affected):
        details["_return"] = {}
        check_return(trip["return_leg"], put, details["_return"])
    elif partial and "_return" in carry.get("time_details", {}):
        details["_return"] = carry["time_details"]["_return"].copy()
        results.extend(row.copy() for row in carry["rule_results"] if row["rule_id"] == "return")
    cost = check_costs(trip, evidence, put)
    summary = "fail" if any(row["status"] == "fail" for row in results) else (
        "pending" if not results or any(row["status"] == "pending" for row in results) else "pass")
    # A production result requires positive applicable evidence, even if the selected rules found no conflict.
    sourced = any(row["status"] == "pass" and row["rule_id"] in {"last_service", "opening"} and row["evidence_ids"]
                  for row in results)
    feasibility = ("conflict" if summary == "fail" else
                   "verified" if summary == "pass" and mode != "fixture" and sourced else "needs_verification")
    return validate_report(dict(policy_version=policy_version, run_status="completed", feasibility=feasibility,
                                data_mode=mode, rule_summary=summary, rule_results=results,
                                missing_evidence=sorted({row["path"] for row in results if row["status"] == "pending"}),
                                cost_summary=cost, checked_scope="affected" if partial else "trip",
                                affected_stop_ids=affected, time_details=details))


def validate_report(report):
    check(REPORT, report)
    rows = report["rule_results"]
    expected = "fail" if any(row["status"] == "fail" for row in rows) else (
        "pending" if not rows or any(row["status"] == "pending" for row in rows) else "pass")
    if (report["policy_version"] not in READABLE_VERSIONS or report["run_status"] != "completed"
            or len(set(report["affected_stop_ids"])) != len(report["affected_stop_ids"])
            or report["rule_summary"] != expected
            or report["missing_evidence"] != sorted({row["path"] for row in rows if row["status"] == "pending"})
            or (report["feasibility"] == "conflict") != (expected == "fail")
            or (report["feasibility"] == "verified" and (expected != "pass" or report["data_mode"] == "fixture"))):
        raise ContractError("invalid_validation_report")
    return report

