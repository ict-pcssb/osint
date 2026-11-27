"""Forecast, announcement, timetable and price applicability for M10."""
from copy import deepcopy
from datetime import timedelta
import json
from urllib.parse import urlsplit

from travel_catalog import public_catalog
from travel_contract import ContractError, FORMATS
from travel_evidence import digest, extract_fact, Material, public_url
from travel_rules import after_last_service, clock_on, format_time, instant, opening_status

TTL = {"weather":timedelta(minutes=15), "quote":timedelta(minutes=5),
       "opening":timedelta(hours=24), "booking":timedelta(hours=24), "last_service":timedelta(hours=24)}
REASONS = {"unbound","timeout","rate_limited","auth_failed","quota_exhausted","invalid_response",
           "provider_error","outside_effective_period","empty","stale","source_conflict","cancelled",
           "out_of_window","budget_exhausted","unresolved_location"}
META = ("value","collected_at","valid_from","valid_to","valid_until","published_at","origin_id","source_url","excerpt","data_mode")


def covers(observation, day):
    return bool(observation["valid_from"] and observation["valid_from"]<=day
                and (observation["valid_to"] is None or day<=observation["valid_to"]))


def freshness(fact, now):
    old = fact["previous"]
    collected,_ = instant(old["collected_at"])
    expiry,_ = instant(old["valid_until"])
    if not collected or not expiry or old["value"] is None or not old["source_url"] or not old["origin_id"]:
        return "unknown"
    if collected>now or not covers(old,fact["target_date"]):
        return "stale"
    # An explicit end of validity is authoritative but cannot extend a weather/quote cache.
    if fact["kind"] in {"weather", "quote"}:
        expiry = min(expiry,collected+TTL[fact["kind"]])
    return "current" if now<expiry else "stale"


def identity(row):
    parts = [row["stop_id"],row["kind"],row["entity"],row["target_date"],row["scope"]]
    return "m10-"+digest(json.dumps(parts,ensure_ascii=False,sort_keys=True))[:24]


def find_facts(version, now):
    if not version.timeline:
        return []
    result = version.timeline["result"]
    mode = result["data_mode"]
    stops = {s["stop_id"]:(s,day["date"]) for day in result["days"] for s in day["stops"] if not s.get("completed")}
    configured = result.get("recheck_facts")
    catalog = public_catalog()
    if configured is None:
        configured = []
        evidence = {r["evidence_id"]:r for r in version.timeline["evidence"]}
        weather = set()
        for stop_id,(stop,day) in stops.items():
            entity_id = stop["poi_id"]
            entity = catalog.get(entity_id)
            for kind in ("opening", "booking"):
                metadata = evidence_observation(entity,kind,day,[evidence.get(k) for k in stop["evidence_ids"]],mode)
                configured.append(dict(kind=kind,stop_id=stop_id,entity=entity_id,target_date=day,scope={},**metadata))
            if entity and entity.coordinates and (day,entity_id) not in weather:
                weather.add((day,entity_id))
                configured.append(dict(kind="weather",stop_id=stop_id,entity=entity_id,target_date=day,
                                       scope={},**empty_observation(mode)))
    selected = {}
    for item in configured:
        if item["stop_id"] not in stops or item["kind"] not in TTL:
            continue
        stop, day = stops[item["stop_id"]]
        if day!=item["target_date"] or day<now.date().isoformat():
            continue
        if item["kind"] in {"weather","opening","booking"} and item["entity"] not in {stop["poi_id"],stop["entrance_id"]}:
            continue
        if item["kind"]=="last_service" and not item["scope"].get("direction"):
            continue
        row = {key:deepcopy(item[key]) for key in ("stop_id","kind","entity","target_date","scope")}
        row.update(stop_label=stop["label"],previous={key:item.get(key) for key in META})
        row["fact_id"] = identity(row)
        selected[row["fact_id"]] = row
    return list(selected.values())[:50]


def empty_observation(mode):
    return {**dict.fromkeys(META),"data_mode":mode,"excerpt":""}


def evidence_observation(entity, kind, day, rows, mode):
    if not entity:
        return empty_observation(mode)
    candidates = []
    for row in rows:
        if (not row or row.get("source_kind") not in {"official","operator"}
                or urlsplit(row["source_url"]).hostname not in entity.authority_hosts):
            continue
        stamp = row.get("collected_at")
        start,_ = instant(stamp)
        if start is None:
            continue
        material = Material(row["source_url"],row["excerpt"],stamp,source_kind=row["source_kind"],
                            data_mode=row["data_mode"],published_at=row.get("published_at"))
        for fact in extract_fact(material,entity,kind,None,day):
            if fact["applicable"]:
                candidates.append(dict(value=fact["value"], collected_at=stamp,valid_from=fact["effective_from"],
                                       valid_to=fact["effective_to"],valid_until=row.get("expires_at") or format_time(start+TTL[kind]),
                                       published_at=row.get("published_at"),origin_id=row["evidence_id"],
                                       source_url=row["source_url"],excerpt=fact["excerpt"],data_mode=row["data_mode"]))
    if len({repr(c["value"]) for c in candidates})==1:
        return max(candidates,key=lambda c:c["collected_at"])
    return empty_observation(mode)


def typed_value(kind, value):
    if kind=="opening":
        return value in ("open","closed")
    if kind=="booking":
        return value in ("required","not_required")
    if kind=="last_service":
        return isinstance(value,str) and len(value)==5 and clock_on("2026-11-26",value) is not None
    if not isinstance(value,dict):
        return False
    if kind=="quote":
        return type(value.get("amount_fen")) is int and value["amount_fen"]>=0 and value.get("currency")=="CNY"
    return (set(value)=={"low_c","high_c","rain_pct"} and all(type(v) in (int,float) for v in value.values())
            and -99<=value["low_c"]<=value["high_c"]<=70 and 0<=value["rain_pct"]<=100)


def normalize_observation(raw, fact, now):
    if not isinstance(raw,dict) or set(raw)!=set(META):
        raise ContractError("observation_fields")
    mode = raw["data_mode"]
    old = fact["previous"]
    source = raw["source_url"]
    if mode in {"live","snapshot"}:
        valid_url = public_url(source)
    elif mode=="fixture" and old["data_mode"]=="fixture":
        valid_url = isinstance(source,str) and source.startswith("fixture://")
    else:
        valid_url = False
    if (not valid_url or not isinstance(raw["origin_id"],str) or not raw["origin_id"]
            or not isinstance(raw["excerpt"],str) or not typed_value(fact["kind"],raw["value"])):
        raise ContractError("observation_source")
    for key, fmt in (("collected_at","date-time"),("valid_from","date"),("valid_to","date"),
                     ("valid_until","date-time")):
        if raw[key] is not None and (not isinstance(raw[key],str) or not FORMATS.conforms(raw[key],fmt)):
            raise ContractError("observation_time")
    published=raw["published_at"]
    if published is not None and (not isinstance(published,str) or not any(
            FORMATS.conforms(published,fmt) for fmt in ("date","date-time"))):
        raise ContractError("publication_time")
    if not covers(raw,fact["target_date"]):
        return None,"outside_effective_period"
    collected,_ = instant(raw["collected_at"])
    expiry,_ = instant(raw["valid_until"])
    if not collected or not expiry or collected>now or expiry<=now or (fact["kind"] in {"quote","weather"}
                                                                       and now>=collected+TTL[fact["kind"]]):
        return None,"stale"
    return raw,"rechecked"


def review_item(fact, now, latest, reason):
    prior = fact["previous"]
    freshness_label = freshness(fact,now)
    day = fact["target_date"]
    value = latest["value"] if latest else None
    rules = []
    if fact["kind"]=="opening" and opening_status(value)=="fail":
        rules.append("OPENING_CLOSED")
    if latest and fact["kind"]=="last_service":
        boarding,_ = instant(fact["scope"].get("boarding_at"))
        if boarding and boarding.date().isoformat()==day and after_last_service(day,boarding,value):
            rules.append("LAST_SERVICE_MISSED")
    reason = reason if reason in REASONS|{"reused","rechecked"} else "invalid_response"
    status = ("conflict" if rules else "changed" if value!=prior["value"] else "unchanged") if latest else (
              "unchanged" if reason=="reused" else "pending")
    return dict(fact_id=fact["fact_id"],stop_id=fact["stop_id"],stop_label=fact["stop_label"],kind=fact["kind"],
                target_date=day,effective_date=latest["valid_from"] if latest else prior["valid_from"],
                previous=prior,latest=latest,checked_at=format_time(now),data_freshness=freshness_label,
                status=status,reason=reason,rule_codes=rules)
