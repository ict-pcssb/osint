"""Deterministic fare eligibility and group-budget arithmetic in integer minor units."""
from datetime import date
import operator
import re

from travel_contract import ContractError, _validator, check

FARE_POLICY = _validator("fare_policy")
COMPARE = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "=": operator.eq}
UNITS = {"person", "person_day", "group", "vehicle", "room_night"}
MODES = {"fixture", "snapshot", "live"}


def nonnegative(value):
    return type(value) is int and value >= 0


def positive(value):
    return nonnegative(value) and value > 0


def cost_line(item, *, people=1, traveler_group=None, data_mode="fixture"):
    unit = item["unit"]
    if unit not in UNITS:
        raise ValueError("费用计价单位无效")
    group = traveler_group or item.get("traveler_group")
    quantity = item.get("quantity")
    if quantity is None:
        factors = {"person": (people,), "person_day": (people, item.get("days")),
                   "vehicle": (item.get("vehicles", 1),), "room_night": (item.get("rooms"), item.get("nights")),
                   "group": (1,)}[unit]
        quantity = 1
        for factor in factors:
            if not positive(factor):
                raise ValueError("需要明确的正整数计价数量")
            quantity *= factor
    if not positive(quantity):
        raise ValueError("需要明确的正整数计价数量")
    amount = item.get("unit_fen")
    if amount is not None and not nonnegative(amount):
        raise ValueError("费用使用非负整数分")
    policy_id = item.get("policy_id")
    eligibility = {"status": ("legacy_grouping_unknown" if not group and "quantity" in item
                              else "unknown" if amount is None else "eligible")}
    price_status = "unknown" if amount is None else "known"
    if policy_id:
        eligibility["policy_id"] = policy_id
        if amount == 0:
            price_status = "free_by_policy"
    if data_mode not in MODES or not re.fullmatch(r"[A-Z]{3}", item.get("currency", "CNY")):
        raise ValueError("币种或数据来源无效")
    return dict(item_id=item["item_id"], category=item["category"], traveler_group=group or "group",
                unit=unit, quantity=quantity, mandatory=item.get("mandatory", True), unit_fen=amount,
                subtotal_fen=None if amount is None else amount * quantity, currency=item.get("currency", "CNY"),
                price_status=price_status, eligibility=eligibility, evidence_ids=list(item.get("evidence_ids", ())),
                data_mode=data_mode)


def _policy_in_effect(policy, target_date):
    start, end = policy.get("effective_from"), policy.get("effective_to")
    if not start or not end or not target_date:
        return policy["data_mode"] == "fixture" and not start and not end
    return date.fromisoformat(start) <= date.fromisoformat(target_date) <= date.fromisoformat(end)


def _child_fare(bands, height_mm, age_years):
    for band in bands:
        op = band["op"]
        if op == "otherwise":
            return band.get("unit_fen"), None
        field = "age_years" if "age_years" in band else "height_mm"
        measure = age_years if field == "age_years" else height_mm
        if measure is None:
            return None, "child_" + field
        if not nonnegative(measure):
            raise ValueError("儿童身高用整数毫米，年龄用整数岁")
        if COMPARE[op](measure, band[field]):
            return band.get("unit_fen"), None
    return None, None


def quote_family(adults, child_heights, policy, shuttle_fen, *, child_ages=None, target_date=None,
                 data_mode="fixture", shuttle_evidence_ids=()):
    """Apply ordered, explicit policy bands. Their fallback and shuttle fare are independent."""
    if policy is not None:
        check(FARE_POLICY, policy)
        if any(band["op"] == "otherwise" for band in policy.get("child_bands", ())[:-1]):
            raise ContractError("fare_policy_fallback_order")
        if not _policy_in_effect(policy, target_date):
            policy = None
    if policy:
        data_mode = policy["data_mode"]
    if not nonnegative(adults) or adults + len(child_heights) == 0:
        raise ValueError("旅客人数无效")
    if child_ages is None:
        child_ages = [None] * len(child_heights)
    lines, question = [], None
    terms = dict(category="ticket", unit="person")
    if policy:
        terms.update(policy_id=policy["policy_id"], evidence_ids=policy["evidence_ids"], currency=policy["currency"])
    if adults:
        adult_fen = policy.get("adult_fen") if policy else None
        lines.append(cost_line(dict(terms, item_id="adults", unit_fen=adult_fen), people=adults,
                               traveler_group="adult", data_mode=data_mode))
    for index, (height, age) in enumerate(zip(child_heights, child_ages, strict=True), 1):
        amount, ask = _child_fare(policy.get("child_bands", ()), height, age) if policy else (None, None)
        question = question or ask
        lines.append(cost_line(dict(terms, item_id=f"child-{index}", unit_fen=amount), traveler_group="child",
                               data_mode=data_mode))
    lines.append(cost_line(dict(item_id="shuttle", category="transport", unit="person", unit_fen=shuttle_fen,
                                evidence_ids=shuttle_evidence_ids), people=adults+len(child_heights),
                           traveler_group="group", data_mode=data_mode))
    return lines, question


def summarize_costs(lines, budget_fen=None, scope="group", people=None):
    if scope not in {"group", "per_person"} or (budget_fen is not None and not nonnegative(budget_fen)):
        raise ValueError("预算口径或金额无效")
    if scope == "per_person" and not positive(people):
        raise ValueError("按人预算要求明确人数")
    cap = None if budget_fen is None else budget_fen * (people if scope == "per_person" else 1)
    totals, missing, item_ids = {}, [], set()
    for line in lines:
        key, amount, quantity = line["item_id"], line["unit_fen"], line["quantity"]
        if key in item_ids or not positive(quantity) or (amount is not None and not nonnegative(amount)):
            raise ValueError("费用项重复或金额无效")
        item_ids.add(key)
        subtotal = None if amount is None else amount * quantity
        if subtotal != line["subtotal_fen"]:
            raise ValueError("费用小计与单价和数量不一致")
        if subtotal is None:
            if line["mandatory"]:
                missing.append(key)
        else:
            currency = line["currency"]
            totals[currency] = totals.get(currency, 0) + subtotal
    lower = totals.get("CNY", 0)
    foreign = sorted(set(totals) - {"CNY"})
    total = None if missing or foreign else lower
    status = "over" if cap is not None and lower > cap else "within" if cap is not None and total is not None else "unknown"
    return dict(scope="group", original_budget_fen=budget_fen, original_budget_scope=scope, cap_fen=cap,
                totals_by_currency=totals, unconverted_currencies=foreign, known_lower_fen=lower,
                total_fen=total, missing_costs=missing, budget_status=status, items=list(lines))

