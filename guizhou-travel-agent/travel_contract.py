"""Strict v1 travel wire contracts shared by the existing chat and preview."""
from datetime import datetime
import json
from math import ceil
import re
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parent / "contracts/travel_agent/v1"
FORMATS = FormatChecker()


@FORMATS.checks("date-time", raises=ValueError)
def _instant(value):
    if not isinstance(value, str):
        return True
    return "T" in value and datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None


def _validator(name):
    schema = json.loads((ROOT / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FORMATS)


INTENT, TOOLS, REPLY = map(_validator, ("intent", "tools", "reply"))


class ContractError(ValueError):
    """Untrusted data did not satisfy a wire contract."""


def check(validator, payload):
    try:
        validator.validate(payload)
    except ValidationError as exc:
        raise ContractError("schema_violation") from exc
    return payload


def decode_intent(raw):
    def unique(pairs):
        values = dict(pairs)
        if len(values) != len(pairs):
            raise ContractError("duplicate_json_field")
        return values

    def invalid(_):
        raise ContractError("non_finite_number")

    if not isinstance(raw, str) or len(raw) > 8000:
        raise ContractError("invalid_intent_input")
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ContractError("invalid_intent_json") from exc
    return check(INTENT, value)


def _records(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _records(item)
    elif isinstance(value, list):
        for item in value:
            yield from _records(item)


def _check_url(url, mode):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                and not any(ch.isspace() or ch == "\\" for ch in url)):
            return
        if mode and parsed.scheme == "fixture" and parsed.hostname and not (parsed.query or parsed.fragment):
            return
    except ValueError:
        pass
    raise ContractError("unsafe_url")


def validate_reply(reply, *, fixture_mode=False):
    check(REPLY, reply)
    evidence = {item["evidence_id"]: item for item in reply["evidence"]}
    if len(evidence) != len(reply["evidence"]):
        raise ContractError("duplicate_evidence")
    result = reply["result"]
    children = [result["primary"], *result["supplements"]] if result["kind"] == "bundle" else [result]
    question_nodes = [result, *children] if result["kind"] == "bundle" else children
    questions = [node.get("next_question") or node.get("question") for node in question_nodes]
    if sum(max(1, len(re.findall(r"[?？]", question))) for question in questions if question) > 1:
        raise ContractError("too_many_questions")
    if result["kind"] == "ack" and (reply["run_status"] != "completed" or evidence):
        raise ContractError("invalid_ack")
    if result["kind"] == "clarification" and reply["run_status"] != "needs_input":
        raise ContractError("invalid_clarification")
    if result["kind"] in {"unavailable", "weather_unavailable"} and reply["run_status"] not in {"failed", "partial"}:
        raise ContractError("invalid_failure_status")
    for node in _records(reply):
        if (node.get("data_mode") == "fixture" or node.get("source_kind") == "fixture") and not fixture_mode:
            raise ContractError("fixture_not_permitted")
        for key in ("source_url", "url", "download_url"):
            if node.get(key) is not None:
                _check_url(node[key], fixture_mode)
        references = [*node.get("evidence_ids", []), *node.get("visit_evidence_ids", [])]
        if "policy_evidence_id" in node:
            references.append(node["policy_evidence_id"])
        if any(reference not in evidence for reference in references):
            raise ContractError("missing_evidence")
        if "unit_fen" in node:
            unit, subtotal, status = node["unit_fen"], node["subtotal_fen"], node["price_status"]
            if status == "unknown":
                if unit is not None or subtotal is not None or not node.get("missing_reason"):
                    raise ContractError("unknown_price")
            elif (unit is None or subtotal != unit * node["quantity"]
                  or (unit == 0) != (status == "free_by_policy")
                  or (status == "free_by_policy" and not node["eligibility"].get("policy_evidence_id"))):
                raise ContractError("invalid_subtotal")
        if node.get("kind") == "cost_breakdown":
            if any(item["currency"] != node["currency"] for item in node["items"]):
                raise ContractError("mixed_currency")
            missing = {item["item_id"] for item in node["items"] if item["mandatory"] and item["price_status"] == "unknown"}
            if not missing.issubset(node["missing_costs"]) or (missing and node["total_fen"] is not None):
                raise ContractError("incomplete_total")
            known_lower = sum(item["subtotal_fen"] for item in node["items"]
                              if item["price_status"] in {"known", "free_by_policy"})
            if node["known_lower_fen"] != known_lower:
                raise ContractError("invalid_known_lower")
            if node["total_fen"] is not None and (any(item["subtotal_fen"] is None for item in node["items"])
                    or node["total_fen"] != sum(item["subtotal_fen"] for item in node["items"])):
                raise ContractError("invalid_total")
        if node.get("kind") == "itinerary":
            _validate_timeline(node)
        if node.get("kind") == "weather":
            low, high = node["forecast"]["temperature_c"]
            if low > high or not references:
                raise ContractError("invalid_forecast")
            if "as_of" in node and (node["forecast"].get("units") != {"temperature":"°C","probability":"%"}
                    or not any(node["target_date"] in evidence[ref]["applies_to"] for ref in references)
                    or any(datetime.fromisoformat(hour["at"]).date().isoformat() != node["target_date"]
                           for hour in node["forecast"].get("hourly",[]))):
                raise ContractError("invalid_forecast_scope")
        if node.get("kind") == "weather_unavailable" and "status" in node:
            if node["status"] != ("provider_error" if node["reason"] == "out_of_window" else node["reason"]):
                raise ContractError("invalid_weather_failure")
        if node.get("kind") == "transfer" and "feasibility" in node:
            if (node["arrival_check"]["status"] == "unreachable") != (node["feasibility"] == "conflict"):
                raise ContractError("transfer_feasibility_mismatch")
            last = node["last_service"]
            if last["state"] == "known" and not any(
                    evidence[ref]["source_kind"] in {"operator","official"} and evidence[ref]["data_mode"] != "fixture"
                    for ref in last["evidence_ids"]):
                raise ContractError("last_service_needs_source")
        if node.get("kind") == "verification" and node["status"] in {"confirmed", "contradicted"}:
            if not any(evidence[ref]["source_kind"] in {"official", "operator"} and
                       evidence[ref]["data_mode"] != "fixture" and node["target_date"] in evidence[ref]["applies_to"]
                       for ref in references):
                raise ContractError("confirmation_needs_source")
        if node.get("kind") == "candidates":
            ids = [option["id"] for option in node["options"]]
            if len(ids) != len(set(ids)) or node["primary_id"] in node["alternative_ids"] or not {
                    node["primary_id"], *node["alternative_ids"]}.issubset(ids):
                raise ContractError("invalid_candidate_ids")
            if "provider_status" in node:
                for option in node["options"]:
                    _validate_intercity_option(option,node["basis"]["date"],node["candidate_kind"])
    return reply


def _validate_intercity_option(option, target_date, kind):
    departure, arrival = (datetime.fromisoformat(option[t]) for t in ("depart_at","arrive_at"))
    if departure.date().isoformat() != target_date or arrival <= departure:
        raise ContractError("invalid_intercity_dates")
    if kind == "train" and (option["start_date"],option["arrive_date"]) != (target_date,arrival.date().isoformat()):
        raise ContractError("invalid_rail_dates")
    legs = option.get("legs")
    if kind == "flight" and (not legs or legs[0]["depart_at"] != option["depart_at"] or legs[-1]["arrive_at"] != option["arrive_at"]
                             or (option["party_status"] == "unknown" and option["reference_fen"] is not None)
                             or any(left["to_id"] != right["from_id"] or
                                    datetime.fromisoformat(left["arrive_at"]) > datetime.fromisoformat(right["depart_at"])
                                    for left,right in zip(legs,legs[1:]))):
        raise ContractError("invalid_flight_details")
    connection = option["connection"]
    pieces = [option["reference_fen"],connection["access_fen"],connection["egress_fen"]]
    lower = sum(piece for piece in pieces if piece is not None)
    total = lower if all(piece is not None for piece in pieces) and (kind != "flight" or option["tax_status"] == "known") else None
    if (option["known_lower_fen"],option["total_fen"]) != (lower,total):
        raise ContractError("invalid_intercity_cost")




def _validate_timeline(result):
    if "validation_report" in result:
        from travel_rules import validate_report

        report = validate_report(result["validation_report"])
        if report["feasibility"] != result["feasibility"] or report["data_mode"] != result["data_mode"]:
            raise ContractError("timeline_report_mismatch")
        if any(report["cost_summary"][key] != result["budget"][key] for key in
               ("items", "cap_fen", "total_fen", "known_lower_fen", "budget_status", "missing_costs")):
            raise ContractError("timeline_cost_report_mismatch")
    if "return_leg" in result:
        _validate_timeline_slice(result)
    dates = [day["date"] for day in result["days"]]
    if dates != sorted(set(dates)) or (result["budget"]["missing_costs"] and result["budget"]["total_fen"] is not None):
        raise ContractError("invalid_schedule")
    for day in result["days"]:
        stops = day["stops"]
        ids = [stop["stop_id"] for stop in stops]
        if len(ids) != len(set(ids)):
            raise ContractError("duplicate_stop")
        formal = {stop["stop_id"] for stop in stops if stop["plan_role"] == "primary" and not stop.get("completed")}
        if day["next_stop_id"] is not None and day["next_stop_id"] not in formal:
            raise ContractError("invalid_next_stop")
        previous = None
        for index, stop in enumerate(stops):
            if (stop["arrival_local"] is None) != (stop["departure_local"] is None):
                raise ContractError("incomplete_stop_time")
            if stop["arrival_local"] is None:
                if stop["plan_role"] != "alternative" or stop["next_stop_id"] is not None or stop["incoming_leg"]["status"] != "unknown":
                    raise ContractError("invalid_unknown_stop")
                continue
            arrival = datetime.fromisoformat(stop["arrival_local"])
            departure = datetime.fromisoformat(stop["departure_local"])
            if departure <= arrival or arrival.date().isoformat() != day["date"]:
                raise ContractError("invalid_stop_time")
            if stop["incoming_leg"]["to_id"] != stop["entrance_id"] or (stop["next_stop_id"] is not None
                    and stop["next_stop_id"] not in formal.intersection(ids[index+1:])):
                raise ContractError("invalid_stop_endpoint")
            if stop["plan_role"] == "primary":
                if previous and arrival < previous:
                    raise ContractError("overlapping_stops")
                previous = departure

    
def _validate_timeline_slice(result):
    budget = result["budget"]
    expected_categories = {"hotel", "meal", "transport", "ticket"}
    if (budget.get("scope") != "group" or budget["cap_fen"] is None or
            not {"items", "included_categories", "budget_status", "excluded_locked_return"} <= budget.keys() or
            set(budget["included_categories"]) != expected_categories or
            {item["category"] for item in budget["items"]} != expected_categories):
        raise ContractError("invalid_timeline_budget")
    items = {item["item_id"]:item for item in budget["items"]}
    unknown = {item["item_id"] for item in budget["items"] if item["subtotal_fen"] is None}
    lower = sum(item["subtotal_fen"] for item in budget["items"] if item["subtotal_fen"] is not None)
    if (len(items) != len(budget["items"]) or budget["known_lower_fen"] != lower or
            any(item["currency"] != "CNY" for item in budget["items"]) or set(budget["missing_costs"]) != unknown or
            budget["total_fen"] != (None if unknown else lower) or
            budget["budget_status"] != (
                "over" if "validation_report" in result and lower > budget["cap_fen"] else
                "unknown" if unknown else "within" if lower <= budget["cap_fen"] else "over")):
        raise ContractError("invalid_timeline_totals")

    points = {point["id"] for point in result.get("map_points", [])}
    if len(points) != len(result.get("map_points", [])):
        raise ContractError("invalid_map_points")

    def route(leg):
        if (leg["from_id"] not in points or leg["to_id"] not in points or leg["from_id"] == leg["to_id"]
                or leg.get("coordinate_system") != "WGS84" or leg.get("profile") != {"walk":"foot","car":"car"}.get(leg["mode"])
                or not leg["evidence_ids"] or not leg.get("checked_at")):
            raise ContractError("invalid_timeline_endpoints")
        numbers = (leg.get("distance_m"), leg.get("duration_s"), leg["travel_min"])
        if leg["status"] == "unknown":
            if any(n is not None for n in numbers) or not leg.get("reason"):
                raise ContractError("invalid_unknown_route")
        elif (any(n is None or n <= 0 for n in numbers) or leg.get("reason")
              or leg["travel_min"] != ceil(leg["duration_s"]/60)):
            raise ContractError("invalid_route_units")

    previous = None
    for day in result["days"]:
        previous = None
        for stop in day["stops"]:
            leg = stop["incoming_leg"]
            route(leg)
            if ((previous and leg["from_id"] != previous) or leg["to_id"] != stop["entrance_id"]
                    or any(item != items.get(item["item_id"]) for item in stop["cost"]["items"])):
                raise ContractError("invalid_timeline_chain")
            previous = stop["entrance_id"]
    returning = result["return_leg"]
    route(returning)
    if returning["from_id"] != previous or len(result["locked_items"]) != 1:
        raise ContractError("invalid_return_leg")
    if result["feasibility"] == "verified" and (unknown or any(
            stop["opening"]["status"] != "confirmed" for day in result["days"] for stop in day["stops"])):
        raise ContractError("unverified_timeline")
