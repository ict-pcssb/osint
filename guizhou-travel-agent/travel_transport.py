"""Normalize read-only forecasts and intercity quotes at the provider boundary."""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import math
import re

from travel_evidence import digest
from travel_rules import clock_on, format_time, instant

WEATHER_FIELDS = ("temperature_2m_min", "temperature_2m_max", "precipitation_probability_max")
HOUR_FIELDS = ("temperature_2m", "precipitation_probability")
PROVIDERS = {"open-meteo":("https://open-meteo.com/","Open-Meteo","open-meteo"),
             "12306":("https://www.12306.cn/","铁路 12306","12306"),
             "flights":("https://www.google.com/travel/flights","Google Flights","google-flights"),
             "fli":("https://www.google.com/travel/flights","Google Flights","google-flights"),
             "osm":("https://www.openstreetmap.org/copyright","OpenStreetMap","openstreetmap")}
STATES = {"success", "empty", "timeout", "auth_failed", "rate_limited", "quota_exhausted",
          "invalid_response", "provider_error", "unbound"}


@dataclass(frozen=True)
class Result:
    status: str
    data: dict | None = None
    reason: str | None = None

    def __post_init__(self):
        if self.status not in STATES or (self.status == "success") != (self.data is not None):
            raise ValueError("工具状态与业务数据不一致")


class ProviderFailure(ValueError):
    def __init__(self, status, reason=None):
        if status not in STATES or status == "success":
            raise ValueError("无效的工具故障状态")
        self.status, self.reason = status, reason or status
        super().__init__(self.reason)


def event(trace, key, **values):
    if hasattr(trace, "record"):
        trace.record(key, **values)
    elif trace is not None:
        trace.append(dict(event=key, **values))


def hash_payload(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def source(provider, day, checked, mode, raw, note, *, scope=""):
    url, publisher, group = PROVIDERS[provider]
    raw_hash = hash_payload(raw)
    key = digest(f"{provider}|{day}|{scope}|{checked}|{raw_hash}")[:22]
    return dict(evidence_id="m7-"+key,source_url=url,publisher=publisher,source_group_id=group,
                collected_at=checked,applies_to=[day],source_kind="fixture" if mode=="fixture" else "provider",
                data_mode=mode,excerpt=note,provider=provider,content_hash=raw_hash)


async def read(call, tool, args, provider, trace):
    if call is None:
        return Result("unbound", reason="unbound")
    try:
        raw = await call(tool, args)
        if raw is None:
            result = Result("empty", reason="empty")
        else:
            raw_hash = hash_payload(raw)
            event(trace, "m07_provider", provider=provider, tool=tool, arguments=args, response_hash=raw_hash)
            return Result("success", {"raw": raw})
    except ProviderFailure as exc:
        result = Result(exc.status, reason=exc.reason)
    except TimeoutError:
        result = Result("timeout", reason="timeout")
    except ConnectionError:
        result = Result("provider_error", reason="connection_error")
    except (KeyError, TypeError, ValueError):
        result = Result("invalid_response", reason="invalid_response")
    except OSError:
        result = Result("provider_error", reason="connection_error")
    event(trace, "m07_provider", provider=provider, tool=tool, arguments=args,
          status=result.status, reason=result.reason)
    return result


def invalid():
    return Result("invalid_response", reason="invalid_response")


def numeric(value):
    return type(value) in (int,float) and math.isfinite(value)


def money_fen(value):
    if not isinstance(value, (int,float,str)) or isinstance(value,bool):
        return None
    try:
        units = Decimal(str(value)) * 100
        if units.is_finite() and 0 < units <= 10**9 and units == units.to_integral_value():
            return int(units)
    except InvalidOperation:
        pass
    return None


def aligns(section, fields, size):
    return isinstance(section, dict) and all(isinstance(section.get(f), list) and len(section[f]) == size for f in fields)


class WeatherAdapter:
    def __init__(self, catalog, call, clock, *, mode):
        self.catalog, self.call, self.clock, self.mode = catalog, call, clock, mode
        self.cache = {}

    async def query(self, location_id, day, *, hourly=False, trace=None):
        now = self.clock()
        point = self.catalog.get(location_id)
        coords = point.coordinates if point else None
        if not coords or coords["system"] != "WGS84" or point.kind not in {"poi", "entrance"}:
            return Result("provider_error", reason="unresolved_location")
        if not 0 <= (date.fromisoformat(day)-now.date()).days < 16:
            return Result("provider_error", reason="out_of_window")
        key = (point.id, coords["system"], coords["latitude"], coords["longitude"], day, hourly)
        cached = self.cache.get(key)
        if cached is not None and timedelta(0) <= now-cached[0] < timedelta(minutes=15):
            event(trace,"m07_weather_cache",location_id=point.id,target_date=day,as_of=cached[1]["as_of"])
            return Result("success", cached[1])
        args = dict(latitude=coords["latitude"],longitude=coords["longitude"],timezone="Asia/Shanghai",
                    start_date=day,end_date=day,daily=list(WEATHER_FIELDS))
        if hourly:
            args["hourly"] = list(HOUR_FIELDS)
        response = await read(self.call,"weather_forecast",args,"open-meteo",trace)
        if response.status != "success":
            return response
        raw = response.data["raw"]
        if raw in ({}, []):
            return Result("empty", reason="empty")
        try:
            data = parse_weather(raw,day,hourly)
        except (KeyError, TypeError, ValueError):
            return invalid()
        as_of = format_time(self.clock())
        data.update(as_of=as_of, updated_at=None,
                    evidence=[source("open-meteo",day,as_of,self.mode,raw,"气象预报覆盖请求地点周边。",scope=point.id)])
        self.cache = {k:v for k,v in self.cache.items() if now-v[0] < timedelta(minutes=15)}
        if len(self.cache) >= 128:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = now,data
        return Result("success", data)


def parse_weather(raw, day, hourly):
    daily, units = raw["daily"], raw["daily_units"]
    if raw["timezone"] != "Asia/Shanghai" or daily["time"] != [day] or not aligns(daily, WEATHER_FIELDS, 1):
        raise ValueError("找不到对齐的目标日预报")
    if tuple(units[f] for f in WEATHER_FIELDS) != ("°C","°C","%"):
        raise ValueError("预报单位不匹配")
    low, high, risk = (daily[f][0] for f in WEATHER_FIELDS)
    if not all(numeric(v) for v in (low,high,risk)) or not -90 <= low <= high <= 65 or not 0 <= risk <= 100:
        raise ValueError("预报数值无效")
    hours = []
    if hourly:
        values, hour_units = raw["hourly"], raw["hourly_units"]
        stamps = values["time"]
        if (not isinstance(stamps,list) or not 1 <= len(stamps) <= 24 or stamps != sorted(set(stamps))
                or not aligns(values,HOUR_FIELDS,len(stamps))
                or tuple(hour_units[f] for f in HOUR_FIELDS) != ("°C","%")):
            raise ValueError("逐时数据未对齐")
        for stamp, temp, chance in zip(stamps,values[HOUR_FIELDS[0]],values[HOUR_FIELDS[1]],strict=True):
            when, _ = instant(stamp)
            if (when is None or when.date().isoformat()!=day or not numeric(temp) or not -90 <= temp <= 65
                    or not numeric(chance) or not 0 <= chance <= 100):
                raise ValueError("逐时指标无效")
            hours.append(dict(at=format_time(when),temperature_c=temp,rain_probability_pct=round(chance)))
    return dict(rain_probability_pct=round(risk),temperature_c=[low,high],hourly=hours,
                units={"temperature":"°C","probability":"%"})


def rail_option(raw, names, station_codes, day, adults, children):
    if (raw.get("start_date"), str(raw.get("from_station")).removesuffix("站"), str(raw.get("to_station")).removesuffix("站")) != (day,*names):
        return None
    if any(raw.get(field, code) != code for field,code in zip(("from_station_telecode","to_station_telecode"),station_codes)):
        return None
    number = raw["start_train_code"]
    depart, arrive = clock_on(day,raw["start_time"]), clock_on(raw["arrive_date"],raw["arrive_time"])
    if not re.fullmatch(r"[A-Z0-9]{1,12}", number) or depart is None or arrive is None or arrive <= depart:
        return None
    seats = []
    for item in raw.get("prices") or []:
        state = item.get("num")
        remaining = int(state) if type(state) in (str,int) and re.fullmatch(r"[0-9]{1,6}",str(state)) else None
        availability = ("available" if state=="有" or remaining else "unavailable" if state=="无" or remaining==0 else "unknown")
        if isinstance(item.get("seat_name"),str) and item["seat_name"]:
            seats.append(dict(label=item["seat_name"],price_fen=money_fen(item.get("price")),
                              remaining=remaining,availability=availability))
    # Compare the same advertised seat class. Child eligibility needs a separate verified policy.
    comparable = next((row for row in seats if row["label"] == "二等座"),None)
    fare = comparable["price_fen"] if comparable and children == 0 else None
    return dict(id=f"train-{number}-{day}", number=number, start_date=day, arrive_date=arrive.date().isoformat(),
                depart_at=format_time(depart),arrive_at=format_time(arrive),seats=seats,
                reference_fen=fare*adults if fare is not None else None, currency="CNY")


class RailAdapter:
    def __init__(self,catalog,call,clock,*,mode):
        self.catalog,self.call,self.clock,self.mode=catalog,call,clock,mode

    async def query(self, from_id, to_id, day, *, adults, children=0, trace=None):
        stations = [self.catalog.get(key) for key in (from_id,to_id)]
        if from_id == to_id or any(not s or s.kind!="station" for s in stations):
            return Result("provider_error",reason="unresolved_station")
        if type(adults) is not int or adults<1 or type(children) is not int or children<0:
            return invalid()
        names = tuple(s.label.removesuffix("站") for s in stations)
        lookup = await read(self.call,"get-station-code-by-names",{"stationNames":"|".join(names)},"12306",trace)
        if lookup.status in {"empty","success"}:
            raw = lookup.data["raw"] if lookup.data else {}
            if not isinstance(raw,dict):
                return invalid()
            codes = tuple(raw.get(n,{}).get("station_code") for n in names if isinstance(raw.get(n,{}),dict))
            if len(codes)!=2 or any(not isinstance(c,str) or not re.fullmatch(r"[A-Z]{3}",c) for c in codes):
                return Result("provider_error",reason="unresolved_station")
        else:
            return lookup
        args = dict(date=day,fromStation=codes[0],toStation=codes[1],limitedNum=8,format="json")
        response = await read(self.call,"get-tickets",args,"12306",trace)
        if response.status != "success":
            return response
        raw = response.data["raw"]
        if not isinstance(raw,list):
            return invalid()
        if not raw:
            return Result("empty",reason="empty")
        options = {}
        for item in raw[:40]:
            try:
                option = rail_option(item,names,codes,day,adults,children)
            except (AttributeError, KeyError, ValueError, TypeError):
                continue
            if option is not None:
                options.setdefault(option["id"],option)
        if not options:
            return invalid()
        checked = format_time(self.clock())
        evidence = source("12306",day,checked,self.mode,raw,"指定站点和日期的查询结果。席别价格和余量以购票页为准。",
                          scope=f"{from_id}/{to_id}/{adults}/{children}")
        return Result("success", dict(options=list(options.values()),evidence=[evidence],provider_status={"rail":"success"}))


def airport_code(value):
    if not isinstance(value,str):
        return None
    text=value.strip()
    if text in CHINA_AIRPORTS:
        return text
    suffix=re.search(r"\(([A-Z]{3})\)$",text)
    if suffix and suffix[1] in CHINA_AIRPORTS:
        return suffix[1]
    return {"Shanghai Pudong International Airport":"PVG",
            "Shanghai Hongqiao International Airport":"SHA",
            "Longdongbao Airport":"KWE",
            "Guiyang Longdongbao International Airport":"KWE"}.get(text)


def flight_option(row, day, origin, destination, currency, party_known, backup):
    if row.get("currency") != currency or not isinstance(row.get("legs"),list) or not 1 <= len(row["legs"]) <= 6:
        return None
    legs = []
    for leg in row["legs"]:
        if backup:
            num = leg.get("flight_number")
            if isinstance(num,str) and num.isdecimal():
                num = str(leg.get("airline_code") or "") + num
            start,end = (airport_code(leg[k]) for k in ("departure_airport","arrival_airport"))
            departure,arrival=leg["departure_time"],leg["arrival_time"]
        else:
            start,end = leg["origin"],leg["destination"]
            departure,arrival,num=leg["depart_local"],leg["arrive_local"],leg.get("flight_number")
        # All accepted endpoints and intermediate legs for this first slice are mainland China domestic.
        if start not in CHINA_AIRPORTS or end not in CHINA_AIRPORTS:
            return None
        depart,_ = instant(str(departure).replace(" ", "T", 1))
        arrive,_ = instant(str(arrival).replace(" ", "T", 1))
        if (not depart or not arrive or arrive<=depart or (legs and (legs[-1]["to_id"]!=start
                or instant(legs[-1]["arrive_at"])[0]>depart))):
            return None
        legs.append(dict(from_id=start,to_id=end,depart_at=format_time(depart),arrive_at=format_time(arrive),
                         flight_number=num if isinstance(num,str) and re.fullmatch(r"[A-Z0-9]{2,12}",num) else None))
    first,last=legs[0],legs[-1]
    if (first["from_id"],last["to_id"],first["depart_at"][:10]) != (origin,destination,day):
        return None
    codes = [leg["flight_number"] for leg in legs if leg["flight_number"]]
    key = digest(str([(leg["from_id"],leg["to_id"],leg["depart_at"],leg["flight_number"]) for leg in legs]))[:18]
    return dict(id="flight-"+key,number=" / ".join(codes) or "航班号待查",depart_at=first["depart_at"],
                arrive_at=last["arrive_at"],legs=legs,currency=currency,observed_fen=money_fen(row.get("price")),
                reference_fen=money_fen(row.get("price")) if party_known else None,
                party_status="known" if party_known else "unknown",tax_status="unknown")


CHINA_AIRPORTS = frozenset({"KWE","PVG","SHA","PEK","PKX","CAN","CTU","TFU","CKG","SZX","KMG","HGH","WUH",
                            "XIY","NKG","CSX","CGO","FOC","XMN","TAO","HAK","SYX","TSN","NGB","NNG","LJG"})


class FlightAdapter:
    def __init__(self,catalog,primary,backup,clock,*,mode,allow_backup=False):
        self.catalog,self.primary,self.backup,self.clock,self.mode=catalog,primary,backup,clock,mode
        self.allow_backup=allow_backup

    async def query(self,from_id,to_id,day,*,adults,currency="CNY",children=0,trace=None,backup_only=False):
        points=[self.catalog.get(key) for key in (from_id,to_id)]
        names=[point.id.upper() for point in points if point and point.kind=="airport"]
        if len(names)!=2 or from_id==to_id or any(name not in CHINA_AIRPORTS for name in names):
            return Result("provider_error",reason="unresolved_airport")
        if type(adults) is not int or not 1<=adults<=9 or type(children) is not int or children<0 or not re.fullmatch(r"[A-Z]{3}",currency):
            return invalid()
        origin,destination=names
        base=dict(origin=origin,destination=destination,currency=currency)
        status={"primary":"not_attempted","backup":"not_attempted"}
        primary = Result("unbound", reason="unbound")
        if not backup_only:
            args=base|dict(depart_date=day,adults=adults,max_results=3)
            primary = await self._call(self.primary,"flights",args,day,adults,children,trace)
            status["primary"]=primary.status
            if primary.status=="success":
                return Result("success",primary.data|{"provider_status":status})
        if not self.allow_backup or self.backup is None:
            return primary
        backup_args=base|dict(departure_date=day,top_n=3)
        if getattr(self.backup,"request_passengers",False) is True:
            backup_args["passengers"]=adults
        backup = await self._call(self.backup,"fli",backup_args,day,adults,children,trace)
        status["backup"]=backup.status
        event(trace,"m07_flight_attempts",provider_status=status,target_date=day)
        if backup.status=="success":
            return Result("success",backup.data|{"provider_status":status})
        return backup if backup_only or primary.status=="unbound" else primary

    async def _call(self,call,provider,args,day,adults,children,trace):
        response=await read(call,"search_flights",args,provider,trace)
        if response.status!="success":
            return response
        raw=response.data["raw"]
        backup=provider=="fli"
        try:
            if backup:
                if raw.get("success") is not True:
                    state={"timeout":"timeout","parse_error":"invalid_response","rate_limit":"rate_limited",
                           "rate_limited":"rate_limited","validation_error":"invalid_response"}.get(
                               raw.get("error_type"),"provider_error")
                    return Result(state,reason="upstream_rejected")
                # The known backup has no response field confirming the party or taxes.
                party_known=False
            else:
                if (raw.get("origin"),raw.get("destination"),raw.get("currency")) != tuple(args[k] for k in ("origin","destination","currency")):
                    return invalid()
                day_status=next((row["status"] for row in raw["days"] if row["date"]==day),None)
                if day in raw.get("dates_not_searched",[]) or day_status != "ok":
                    state={"timeout":"timeout","no_results":"empty","unpriced":"empty"}.get(day_status,"provider_error")
                    return Result(state,reason="date_not_searched" if day in raw.get("dates_not_searched",[]) else (day_status or "missing_target_date"))
                party_known=raw.get("adults")==adults and children==0
            flights=raw["flights"]
            if not isinstance(flights,list):
                return invalid()
            if not flights:
                return Result("empty",reason="empty")
            options={}
            for row in flights[:12]:
                try:
                    option=flight_option(row,day,args["origin"],args["destination"],args["currency"],party_known,backup)
                except (AttributeError, KeyError, ValueError, TypeError):
                    continue
                if option is not None:
                    options.setdefault(option["id"],option)
            if not options:
                return invalid()
        except (AttributeError, KeyError, ValueError, TypeError):
            return invalid()
        evidence=source(provider,day,format_time(self.clock()),self.mode,raw,
                        "Google Flights 该方向和日期的参考报价。税费、库存和门到门费用需单独核实。",
                        scope=f"{args['origin']}/{args['destination']}/{adults}/{children}/{args['currency']}")
        return Result("success",dict(options=list(options.values()),evidence=[evidence]))

