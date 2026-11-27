"""M3 travel context, draft, and user-confirmed arrangements."""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from uuid import uuid4

from travel_contract import INTENT, FORMATS
from jsonschema import Draft202012Validator

BEIJING = timezone(timedelta(hours=8))
TZ = "Asia/Shanghai"
PLAN_FIELDS = ("destination", "start_date", "adults", "days", "group_cap_fen")
PREFERENCE_FIELDS = frozenset({"origin", "destination", "walking_preference", "transport_preference"})
STRING_FIELDS = {"origin", "current_location", "walking_preference", "transport_preference", "intended_region"}
INTEGERS = {"budget_fen": (0, 10**10), "travelers": (1, 99)}
ARRANGEMENT_FIELDS = {
    "rail": {"purpose", "departure_at", "arrival_at", "from_label", "to_label"},
    "flight": {"purpose", "departure_at", "arrival_at", "from_label", "to_label"},
    "hotel": {"purpose", "check_in", "check_out", "place"},
    "booking": {"purpose", "starts_at", "place"},
    "return": {"purpose", "departure_at", "from_label", "to_label"},
}
PARAMETER_CHECKS = {
    key: Draft202012Validator(value, format_checker=FORMATS)
    for key, value in INTENT.schema["properties"]["parameters"]["properties"].items()
}


@dataclass(frozen=True)
class Provenance:
    source: str
    input_field: str
    updated_at: str
    confirmation: str = "stated"


@dataclass
class SessionContext:
    fields: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    recent_explicit_date: str | None = None


@dataclass(frozen=True)
class Context:
    known_fields: dict
    missing_fields: tuple
    provenance: dict
    timezone: str = TZ

    @property
    def next_field(self):
        return next(iter(self.missing_fields), None)


@dataclass(frozen=True)
class PreferenceConsent:
    authorized: bool = False
    enabled: bool = False
    fields: tuple = ()
    updated_at: str | None = None

    @property
    def can_read(self):
        return self.authorized and self.enabled and bool(self.fields)


@dataclass(frozen=True)
class LockedArrangement:
    kind: str
    stable_id: str
    confirmed_by: str
    confirmed_at: str
    fields: dict
    fields_hash: str
    confirmation: str = "user_confirmed"
    locked: bool = True
    lock_revision: int = 1


@dataclass(frozen=True)
class TripDraft:
    draft_id: str
    fields: dict
    provenance: dict
    locked_items: tuple
    missing_fields: tuple
    next_question: str | None
    base_trip_id: str | None = None
    base_revision: int | None = None
    schema_version: str = "1.0"
    timeline: dict | None = None


@dataclass(frozen=True)
class TripVersion:
    trip_id: str
    owner_id: str
    revision: int
    schema_version: str
    fields: dict
    provenance: dict
    locked_items: tuple
    timeline: dict | None = None


def resolve_date(value, now, recent_explicit_date=None):
    if not isinstance(value, str) or now.utcoffset() is None:
        raise ValueError("日期必须使用带时区的时钟")
    today = now.astimezone(BEIJING).date()
    days = {"今天": 0, "明天": 1, "周六": (5 - today.weekday()) % 7, "这周六": (5 - today.weekday()) % 7}
    if value in days:
        return (today + timedelta(days=days[value])).isoformat()
    if value == "那天":
        return date.fromisoformat(recent_explicit_date).isoformat() if recent_explicit_date else None
    return date.fromisoformat(value).isoformat()


def _ordinary_fields(values, now, recent, allow_refs=False):
    # Internal trip/version references are added by the authenticated service.
    for original, value in values.items():
        key = {"intended_region": "destination", "travelers": "adults"}.get(original, original)
        if key in {"trip_id", "base_revision", "locked_ids", "scope_date"} and not allow_refs:
            continue
        if key in values and key != original:
            continue
        if key in {"date", "start_date", "end_date"}:
            if value is not None:
                yield key, resolve_date(value, now, recent), original
        elif key in STRING_FIELDS or original == "intended_region":
            if isinstance(value, str) and value.strip() and len(value) <= 120:
                yield key, value.strip(), original
        elif original in INTEGERS:
            low, high = INTEGERS[original]
            if type(value) is int and low <= value <= high:
                yield key, value, original
        elif key == "budget_scope":
            if value in ("person", "per_person", "group"):
                yield key, "person" if value == "per_person" else value, original
        elif key in PARAMETER_CHECKS and PARAMETER_CHECKS[key].is_valid(value):
            yield key, value, original


def build_context(current_message, form=None, session=None, consented_preferences=None, *, now,
                  required=PLAN_FIELDS, allow_refs=False):
    session = session or SessionContext()
    fields = {}
    provenance = {}
    precedence = {}
    for level, (source, values) in enumerate((("authorized_preferences", consented_preferences or {}),
                           ("session", session.fields), ("homepage", form or {}),
                           ("current_message", current_message))):
        for key, value, original in _ordinary_fields(values, now, session.recent_explicit_date, allow_refs):
            if value is None:
                fields.pop(key, None)
                provenance.pop(key, None)
                precedence.pop(key, None)
                continue
            fields[key] = value
            precedence[key] = level
            previous = session.provenance.get(key) if source == "session" else None
            provenance[key] = previous or Provenance(source, original, now.astimezone(BEIJING).isoformat())
    cap_level = precedence.get("group_cap_fen", -1)
    budget_level = max(precedence.get("budget_fen", -1), precedence.get("budget_scope", -1))
    if "group_cap_fen" in fields and cap_level > budget_level:
        fields.update(budget_scope="group", budget_fen=fields["group_cap_fen"])
        for key in ("budget_scope", "budget_fen"):
            provenance[key] = provenance["group_cap_fen"]
    elif "budget_fen" in fields and "budget_scope" in fields:
        count = (fields.get("adults", 0) + fields.get("children", 0)) if fields["budget_scope"] == "person" else 1
        if count:
            fields["group_cap_fen"] = fields["budget_fen"] * count
            provenance["group_cap_fen"] = provenance["budget_fen"]
        else:
            fields.pop("group_cap_fen", None)
            provenance.pop("group_cap_fen", None)

    if "start_date" in fields:
        start = date.fromisoformat(fields["start_date"])
        if ("days" in fields and ("end_date" not in fields or precedence.get("days", -1) > precedence["end_date"])):
            fields["end_date"] = (start + timedelta(days=fields["days"] - 1)).isoformat()
            provenance["end_date"] = provenance["days"]
        elif "end_date" in fields:
            span = (date.fromisoformat(fields["end_date"]) - start).days + 1
            if not 1 <= span <= 30:
                raise ValueError("行程日期范围无效")
            if "days" not in fields or precedence["days"] < precedence["end_date"]:
                fields["days"] = span
                provenance["days"] = provenance["end_date"]
            elif fields["days"] != span:
                raise ValueError("行程天数与起止日期不一致")
    missing = tuple(key for key in required if key not in fields)
    return Context(fields, missing, provenance)


def lock_arrangement(raw, *, confirmed, now):
    if confirmed is not True or set(raw) != {"kind", "fields"}:
        raise ValueError("请明确确认一项旅行安排")
    kind = raw["kind"]
    fields = raw["fields"]
    if kind not in ARRANGEMENT_FIELDS or not isinstance(fields, dict) or not fields or not fields.keys() <= ARRANGEMENT_FIELDS[kind]:
        raise ValueError("安排字段无效")
    if not all(isinstance(v, str) and 0 < len(v.strip()) <= 120 for v in fields.values()):
        raise ValueError("安排字段无效")
    if not ({"departure_at", "check_in", "starts_at"} & fields.keys()):
        raise ValueError("请提供安排时刻或入住日期")
    for key, value in fields.items():
        if key.endswith("_at") and datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("安排时刻必须带时区")
        if key in {"check_in", "check_out"}:
            date.fromisoformat(value)
    canonical = json.dumps({"kind": kind, "fields": fields}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return LockedArrangement(kind, uuid4().hex, "user", now.astimezone(BEIJING).isoformat(),
                             dict(fields), sha256(canonical.encode()).hexdigest())


def merge_arrangements(existing, changes):
    if any(item.stable_id in changes for item in existing):
        return existing, "你是要变更已经确认的安排吗？"
    return existing, None

