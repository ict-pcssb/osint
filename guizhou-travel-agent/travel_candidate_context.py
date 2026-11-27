"""Bound, short-lived server state for one explicit taxi pickup choice."""
import asyncio
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from threading import RLock
from uuid import uuid4

from travel_evidence import digest
from travel_transport import event

CONTEXT_TTL = timedelta(minutes=10)
QUOTE_TTL = timedelta(minutes=5)


class ChoiceError(PermissionError):
    def __init__(self, reason):
        self.reason=reason
        self.status={"forbidden":403,"expired":410,"invalid_choice":400,"already_chosen":409}[reason]
        super().__init__({"forbidden":"该选点不可用","expired":"选点已过期，请重新查询",
                          "invalid_choice":"请从该轮的候选点选择","already_chosen":"该轮已选过点，请重新查询"}[reason])


@dataclass(frozen=True)
class CandidateScope:
    owner: str
    conversation: str


@dataclass
class ChoiceContext:
    scope: CandidateScope
    turn: str
    expires: object
    application_args: dict
    options: dict
    choice: str | None = None
    response: Future = field(default_factory=Future)


class ToolContextStore:
    def __init__(self,clock):
        self.clock=clock
        self.records={}
        self.guard=RLock()

    def issue(self,scope,turn,app_args,choices,trace):
        now=self.clock()
        expires=now+CONTEXT_TTL
        context_id=uuid4().hex
        options={}
        for choice in choices:
            key=uuid4().hex
            options[key]=choice.copy()
        with self.guard:
            self.records={k:v for k,v in self.records.items() if v.expires>now}
            if len(self.records)>=256:
                self.records.pop(next(iter(self.records)))
            self.records[context_id]=ChoiceContext(scope,turn,expires,dict(app_args),options)
        public=[dict(id=key,label=value["name"],role=value["role"]) for key,value in options.items()]
        event(trace,"m08_context_created",turn_id=turn,context_hash=digest(context_id),
              business_data_hashes=sorted({digest(c["businessData"]) for c in choices}),choices=len(public),
              expires_at=expires.isoformat(),write_count=0)
        return context_id,expires,public

    async def choose(self,scope,context_id,choice_id,perform,trace):
        try:
            if not isinstance(context_id,str) or not isinstance(choice_id,str) or len(context_id)>80 or len(choice_id)>80:
                raise ChoiceError("forbidden")
            with self.guard:
                state=self.records.get(context_id)
                if not state or state.scope!=scope:
                    raise ChoiceError("forbidden")
                if self.clock()>=state.expires:
                    # Keep a short owner-bound tombstone. Vendor data is no longer needed.
                    state.options.clear()
                    state.application_args.clear()
                    raise ChoiceError("expired")
                if choice_id not in state.options:
                    raise ChoiceError("invalid_choice")
                if state.choice not in (None,choice_id):
                    raise ChoiceError("already_chosen")
                initiate=state.choice is None
                state.choice=choice_id
                selected=state.options[choice_id].copy()
                args=dict(state.application_args)
        except ChoiceError as exc:
            event(trace,"m08_choice_rejected",reason=exc.reason,write_count=0)
            raise
        event(trace,"m08_choice_validated",turn_id=state.turn,context_hash=digest(context_id),
              business_data_hash=digest(selected["businessData"]),reused=not initiate,write_count=0)
        if initiate:
            try:
                packet=await perform(args,selected)
                state.response.set_result(deepcopy(packet))
            except BaseException as exc:
                state.response.set_exception(exc)
                raise
        else:
            packet=deepcopy(await asyncio.shield(asyncio.wrap_future(state.response)))
            result=packet["result"]
            if result.get("candidate_kind")=="taxi":
                age=self.clock().timestamp()-packet_collection(packet).timestamp()
                if age>=QUOTE_TTL.total_seconds():
                    result["quote_status"]="stale"
                    result["price_meaning"]="上次整车预估已过期，请重新查询。"
        return packet


def packet_collection(packet):
    from datetime import datetime

    return datetime.fromisoformat(packet["evidence"][0]["collected_at"])

