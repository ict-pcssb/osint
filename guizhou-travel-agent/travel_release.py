"""Release evaluation limits, counters and an allowlisted trace."""
from collections import defaultdict
from datetime import datetime
from math import ceil, isfinite
import re
from threading import RLock
from time import monotonic
from uuid import uuid4


class RunLimit(RuntimeError):
    pass


class RunBudget:
    def __init__(self, *, attempts, seconds, cost_micros, clock=monotonic):
        if type(attempts) is not int or attempts<1 or (not isfinite(seconds) or not 0<seconds) or type(cost_micros) is not int or cost_micros<0:
            raise ValueError("budget must have positive duration/count and a nonnegative cost cap")
        self.limit,self.cap,self.clock=attempts,cost_micros,clock
        self.deadline=clock()+seconds
        self.lock=RLock()
        self.attempts=0
        self.known_cost_micros=0
        self.reserved_cost_micros=0
        self.unknown_cost_count=0
        self.pending={}

    def remaining(self):
        return max(0,self.deadline-self.clock())

    def reserve(self,provider,maximum):
        with self.lock:
            if type(maximum) is not int or maximum<0:
                raise RunLimit("unpriced_attempt")
            if not self.remaining():
                raise RunLimit("deadline")
            if self.attempts>=self.limit:
                raise RunLimit("attempt_budget")
            if self.known_cost_micros+self.reserved_cost_micros+maximum>self.cap:
                raise RunLimit("cost_budget")
            self.attempts+=1
            self.pending[self.attempts]=maximum
            self.reserved_cost_micros+=maximum
            return self.attempts

    def finish(self,reference,actual):
        with self.lock:
            with_budget=self.pending.pop(reference)
            if actual is None:
                self.unknown_cost_count+=1
                return
            if type(actual) is not int or not 0<=actual<=with_budget:
                self.unknown_cost_count+=1
                self.deadline=self.clock()
                raise RunLimit("provider_exceeded_reservation")
            self.reserved_cost_micros-=with_budget
            self.known_cost_micros+=actual


def check_policy(policy,now,model):
    keys={"authorized","model","max_attempts","max_seconds","cap_micros",
          "input_micros_per_million","output_micros_per_million","max_output_tokens","expires_at"}
    if not isinstance(policy,dict) or set(policy)!=keys or policy["authorized"] is not True or policy["model"]!=model:
        raise RunLimit("unapproved_model_policy")
    for name in keys-{"authorized","model","expires_at"}:
        if type(policy[name]) is not int or not 0<policy[name]<=10**12:
            raise RunLimit("invalid_policy_limit")
    try:
        expiry=datetime.fromisoformat(policy["expires_at"])
        if expiry.utcoffset() is None or now>=expiry:
            raise RunLimit("expired_policy")
    except (TypeError,ValueError):
        raise RunLimit("invalid_policy_period") from None
    return policy


class RunTrace:
    fields={"intent","tool","skill","version","provider","status","reason","check_state","run_id","attempt",
            "retry","duration_ms","evidence_ids","trip_writes","write_count","base_revision","current_revision",
            "revision","rule_status","rule_codes","proposal_hash","operation_hash","checked_at","as_of",
            "stage","attempted","kind","role"}
    param_fields={"place_id","from_id","to_id","date","start_date","scope_date","target_date","check_in",
                  "adults","children","rooms","nights","days","mode","format","candidate_kind","hourly"}

    def __init__(self):
        self.trace_id=uuid4().hex
        self.origin=monotonic()
        self.events=[]
        self.lock=RLock()

    @staticmethod
    def clean(value):
        if type(value) in (int,bool) or value is None:
            return value
        if isinstance(value,str) and len(value)<=120 and re.fullmatch(r"[\w.:+/-]+",value):
            if re.fullmatch(r"(?i)(?:sk-|bearer|token|secret|https?://).*",value):
                return "[redacted]"
            if re.fullmatch(r"(?:86)?1[3-9]\d{9}",value):
                return "[redacted]"
            return value
        if isinstance(value,(tuple,list)):
            return [RunTrace.clean(item) for item in value[:60]]
        return "[redacted]"

    def record(self,event,**details):
        safe={key:self.clean(value) for key,value in details.items() if key in self.fields}
        if isinstance(details.get("arguments"),dict):
            safe["arguments"]={key:self.clean(value) for key,value in details["arguments"].items() if key in self.param_fields}
        with self.lock:
            self.events.append(dict(event=self.clean(event),trace_id=self.trace_id,
                                    elapsed_ms=round((monotonic()-self.origin)*1000),**safe))


class Measure:
    @staticmethod
    def summary(rows):
        grouped=defaultdict(list)
        for row in rows:
            grouped[row["family"]].append(row)
        summary={}
        for family,items in grouped.items():
            times=sorted(row["elapsed_ms"] for row in items)
            known=sum(row["cost_micros"] for row in items if row["cost_micros"] is not None)
            unknown=sum(row["cost_micros"] is None for row in items)
            summary[family]=dict(count=len(items),p50_ms=times[ceil(len(times)*.5)-1],
                                 p95_ms=times[ceil(len(times)*.95)-1],known_cost_micros=known,
                                 cost_micros=None if unknown else known,unknown_cost_count=unknown,
                                 attempts=sum(row["attempts"] for row in items),
                                 retries=sum(row.get("retries",0) for row in items),
                                 cache_hits=sum(row.get("cache_hits",0) for row in items))
        return summary
