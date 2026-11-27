"""Local M3 account entry points and durable trip storage for the single agent."""
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import pbkdf2_hmac, sha256
from hmac import compare_digest
import json
import os
from pathlib import Path
import re
from secrets import token_bytes, token_urlsafe
import sqlite3
from threading import RLock
from uuid import uuid4

from travel_state import (LockedArrangement, PLAN_FIELDS, PREFERENCE_FIELDS, PreferenceConsent, Provenance, SessionContext,
                          TripDraft, TripVersion, build_context, lock_arrangement, merge_arrangements)

SCHEMA = Path(__file__).resolve().parent / "contracts/travel_agent/v1/state.sql"
VERSION_FIELDS = frozenset(PLAN_FIELDS)
DUMMY_SALT = bytes(16)


class AuthRequired(PermissionError):
    status = 401


class NotFound(LookupError):
    status = 404

    def __init__(self):
        super().__init__("记录不存在")


class Conflict(RuntimeError):
    status = 409


class TripRepository:
    """One file-backed SQLite database, shared across local application instances."""

    def __init__(self, path):
        path = Path(path)
        if path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"} or str(path).startswith(("\\\\", "//")):
            raise ValueError("请选择本地 SQLite 文件")
        self.path = path.resolve()
        with self.connection() as db:
            db.executescript(SCHEMA.read_text(encoding="utf-8"))

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(str(self.path), timeout=8, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            yield db

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def version(db, owner, trip_id, revision=None):
        row = db.execute(
            """SELECT v.* FROM trip_versions AS v JOIN trips AS t USING (trip_id)
               WHERE t.owner_id=? AND v.trip_id=? AND (? IS NULL OR v.revision=?)
               ORDER BY v.revision DESC LIMIT 1""",
            (owner, trip_id, revision, revision),
        ).fetchone()
        if row is None:
            raise NotFound()
        payload = json.loads(row["payload_json"])
        return TripVersion(trip_id, owner, row["revision"], row["schema_version"],
                           payload.get("fields", {}),
                           {name: Provenance(**item) for name, item in payload.get("provenance", {}).items()},
                           tuple(LockedArrangement(**item) for item in payload.get("locked_items", [])), payload.get("timeline"))

    @staticmethod
    def insert_version(db, source, revision, *, timeline, trip_id=None):
        target=trip_id or source.trip_id
        payload=json.dumps({"fields":source.fields,
                            "provenance":{key:asdict(item) for key,item in source.provenance.items()},
                            "locked_items":[asdict(item) for item in source.locked_items],
                            "timeline":timeline},ensure_ascii=False)
        db.execute("INSERT INTO trip_versions VALUES (?,?,?,?)",(target,revision,source.schema_version,payload))

    def read_preferences(self, db, owner):
        row = db.execute("SELECT fields_json FROM preferences WHERE owner_id=?", (owner,)).fetchone()
        return json.loads(row["fields_json"]) if row is not None else {}


class TravelApplication:
    """Authenticated Python entry points for the local app. HTTP transport is separate."""

    def __init__(self, path, *, clock=None, candidate_skills=None):
        self.repository = TripRepository(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if candidate_skills is None and os.getenv("TRAVEL_M08_NETWORK")=="1":
            from travel_candidates_live import configured_skills

            candidate_skills=configured_skills(self.clock)
        self.candidate_skills=candidate_skills
        self.patch_service=None
        self.recheck_service=None
        self.guidebook_service=None
        self._sessions = {}
        self._drafts = {}
        self._exports = {}
        self._memory_guard = RLock()

    @staticmethod
    def _secret_hash(password, salt):
        return pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)

    def create_account(self, username, password):
        if not isinstance(username, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{3,64}", username):
            raise ValueError("账号名需为 3–64 位字母、数字或 ._-")
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError("密码需为 12–256 位")
        owner, salt = uuid4().hex, token_bytes(16)
        try:
            with self.repository.transaction() as db:
                db.execute("INSERT INTO accounts VALUES (?,?,?,?)",
                           (owner, username, salt, self._secret_hash(password, salt)))
        except sqlite3.IntegrityError as exc:
            raise Conflict("账号已存在") from exc
        return owner

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 256:
            raise AuthRequired("账号或密码错误")
        with self.repository.connection() as db:
            row = db.execute("SELECT owner_id,salt,password_hash FROM accounts WHERE username=?", (username,)).fetchone()
        candidate = self._secret_hash(password, row["salt"] if row else DUMMY_SALT)
        if not compare_digest(candidate, row["password_hash"] if row else bytes(32)) or row is None:
            raise AuthRequired("账号或密码错误")
        token = token_urlsafe(32)
        with self.repository.transaction() as db:
            db.execute("INSERT INTO auth_sessions VALUES (?,?,?)",
                       (self._token_hash(token), row["owner_id"], int(self.clock().timestamp()) + 8 * 3600))
        return token

    @staticmethod
    def _token_hash(token):
        return sha256(token.encode("utf-8")).hexdigest()

    def _owner(self, db, token):
        if not isinstance(token, str) or not 30 <= len(token) <= 80:
            raise AuthRequired("请先登录")
        row = db.execute("SELECT owner_id FROM auth_sessions WHERE token_hash=? AND expires_at>?",
                         (self._token_hash(token), int(self.clock().timestamp()))).fetchone()
        if row is None:
            raise AuthRequired("请先登录")
        return row["owner_id"]

    def logout(self, token):
        with self.repository.transaction() as db:
            self._owner(db, token)
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (self._token_hash(token),))

    def set_consent(self, token, *, authorized, enabled, fields):
        if type(authorized) is not bool or type(enabled) is not bool or not set(fields) <= PREFERENCE_FIELDS:
            raise ValueError("授权字段无效")
        allowed = sorted(set(fields)) if authorized else []
        with self.repository.transaction() as db:
            owner = self._owner(db, token)
            db.execute("""INSERT INTO consents VALUES (?,?,?,?,?)
                       ON CONFLICT(owner_id) DO UPDATE SET authorized=excluded.authorized,
                       enabled=excluded.enabled, fields_json=excluded.fields_json, updated_at=excluded.updated_at""",
                       (owner, int(authorized), int(enabled), json.dumps(allowed), self.clock().isoformat()))

    @staticmethod
    def _consent(db, owner):
        row = db.execute("SELECT * FROM consents WHERE owner_id=?", (owner,)).fetchone()
        if row is None:
            return PreferenceConsent()
        return PreferenceConsent(bool(row["authorized"]), bool(row["enabled"]),
                                 tuple(json.loads(row["fields_json"])), row["updated_at"])

    def update_preferences(self, token, fields):
        checked = build_context(fields, now=self.clock(), required=()).known_fields
        if set(fields) != set(checked) or not set(checked) <= PREFERENCE_FIELDS:
            raise ValueError("偏好字段无效")
        with self.repository.transaction() as db:
            owner = self._owner(db, token)
            consent = self._consent(db, owner)
            if not consent.can_read or not checked.keys() <= set(consent.fields):
                raise AuthRequired("请先授权并开启对应偏好")
            existing = self.repository.read_preferences(db, owner)
            db.execute("""INSERT INTO preferences VALUES (?,?)
                       ON CONFLICT(owner_id) DO UPDATE SET fields_json=excluded.fields_json""",
                       (owner, json.dumps({**existing, **checked}, ensure_ascii=False)))

    def _remembered(self, db, owner, use_preferences):
        if not use_preferences:
            return {}
        consent = self._consent(db, owner)
        if not consent.can_read:
            return {}
        stored = self.repository.read_preferences(db, owner)
        return {name: stored[name] for name in consent.fields if name in stored}

    @staticmethod
    def _scoped(records, owner, ref):
        item = records.get(ref)
        if item is None or item[0] != owner:
            raise NotFound()
        return item[1]

    def open_session(self, token, fields=None):
        with self.repository.connection() as db:
            owner = self._owner(db, token)
        context = build_context({}, fields, now=self.clock(), required=())
        ref = uuid4().hex
        with self._memory_guard:
            self._sessions[ref] = (owner, SessionContext(context.known_fields, context.provenance,
                                                       context.known_fields.get("date")))
        return ref

    def preview_trip(self, token, current, form=None, *, session_id=None, trip_id=None, use_preferences=False,
                     arrangements=(), confirm_arrangements=False, arrangement_changes=None, store_draft=True):
        from travel_routing import QUESTIONS

        now = self.clock()
        with self.repository.connection() as db:
            owner = self._owner(db, token)
            saved = self.repository.version(db, owner, trip_id) if trip_id is not None else None
            preferences = self._remembered(db, owner, use_preferences)
        with self._memory_guard:
            session = self._scoped(self._sessions, owner, session_id) if session_id is not None else SessionContext()
        if saved is not None:
            session = SessionContext({**saved.fields, **session.fields}, {**saved.provenance, **session.provenance},
                                     session.recent_explicit_date)
        context = build_context(current, form, session, preferences, now=now)
        locked, lock_question = merge_arrangements(saved.locked_items if saved else (), arrangement_changes or {})
        if saved and arrangements:
            # Changing an existing fixed arrangement requires a later proposal flow.
            lock_question = "你是要变更已经确认的安排吗？"
        elif arrangements:
            locked = (*locked, *(lock_arrangement(item, confirmed=confirm_arrangements, now=now) for item in arrangements))
        question = lock_question or (QUESTIONS[context.next_field] if context.next_field else None)
        draft = TripDraft(uuid4().hex, context.known_fields, context.provenance, locked, context.missing_fields,
                          question, saved.trip_id if saved else None, saved.revision if saved else None)
        if store_draft:
            with self._memory_guard:
                self._drafts[draft.draft_id] = (owner, deepcopy(draft))
        return draft

    def get_draft(self, token, draft_id):
        with self.repository.connection() as db:
            owner = self._owner(db, token)
        with self._memory_guard:
            return deepcopy(self._scoped(self._drafts, owner, draft_id))

    def discard_draft(self, token, draft_id):
        with self.repository.connection() as db:
            owner = self._owner(db, token)
        with self._memory_guard:
            self._scoped(self._drafts, owner, draft_id)
            del self._drafts[draft_id]

    def attach_timeline(self, token, draft_id, packet, *, fixture_mode, clone=False):
        from travel_contract import validate_reply

        packet = deepcopy(packet)
        with self.repository.connection() as db:
            owner = self._owner(db, token)
        with self._memory_guard:
            draft = self._scoped(self._drafts, owner, draft_id)
            if clone:
                draft = replace(draft, draft_id=uuid4().hex)
                packet["result"]["draft_id"] = draft.draft_id
            validate_reply(packet, fixture_mode=fixture_mode)
            result = packet["result"]
            dates = [draft.fields["start_date"], draft.fields["end_date"]]
            if (result["kind"] != "itinerary" or result["draft_id"] != draft.draft_id
                    or [day["date"] for day in result["days"]] != dates
                    or result["budget"]["cap_fen"] != draft.fields["group_cap_fen"]
                    or [(item["lock_id"], item["kind"], item["departure_at"], item.get("lock_revision", 1))
                        for item in result["locked_items"]]
                    != [(item.stable_id, item.kind, item.fields["departure_at"], item.lock_revision)
                        for item in draft.locked_items]):
                raise ValueError("草稿身份、日期、预算或固定安排发生变化")
            self._drafts[draft.draft_id] = (owner, replace(draft, timeline=packet))
        return deepcopy(packet)

    def create_trip(self, token, draft_id, *, save, idempotency_key, require_timeline=False):
        if save is not True or not isinstance(idempotency_key, str) or not re.fullmatch(r"[\w.-]{1,128}", idempotency_key):
            raise ValueError("请明确保存并提供幂等键")
        with self.repository.transaction() as db:
            owner = self._owner(db, token)
            existing = db.execute("SELECT * FROM save_keys WHERE owner_id=? AND request_key=?",
                                  (owner, idempotency_key)).fetchone()
            if existing:
                if existing["draft_id"] != draft_id:
                    raise Conflict("此幂等键已用于其他草稿")
                version = self.repository.version(db, owner, existing["trip_id"], existing["revision"])
                if require_timeline and version.timeline is None:
                    raise ValueError("该版本没有时间轴")
                return version
            with self._memory_guard:
                draft = self._scoped(self._drafts, owner, draft_id)
            if (require_timeline and draft.timeline is None) or (
                    draft.timeline and draft.timeline["result"]["feasibility"] == "conflict"):
                raise ValueError("需要完整、无已发现冲突的时间轴草稿")
            if draft.timeline:
                from travel_rules import POLICY_VERSION

                report = draft.timeline["result"].get("validation_report")
                if os.environ.get("TRAVEL_RULES_POLICY_VERSION", POLICY_VERSION) != POLICY_VERSION:
                    raise ValueError("行程提交暂停，等待启用已验收的规则版本")
                if require_timeline and (report is None or report["policy_version"] != POLICY_VERSION):
                    raise ValueError("请先用当前规则版本重新生成时间轴草稿")
            if draft.next_question or not VERSION_FIELDS <= draft.fields.keys():
                raise ValueError("请先完成草稿确认")
            if draft.base_trip_id is None:
                trip_id, revision = uuid4().hex, 1
                db.execute("INSERT INTO trips VALUES (?,?)", (trip_id, owner))
            else:
                trip_id = draft.base_trip_id
                current = self.repository.version(db, owner, trip_id)
                if current.revision != draft.base_revision or current.locked_items != draft.locked_items:
                    raise Conflict("原行程版本或锁定安排已变化")
                if current.timeline is not None:
                    raise Conflict("已保存的时间轴须通过局部修改预览提交")
                revision = current.revision + 1
            self.repository.insert_version(db,draft,revision,trip_id=trip_id,timeline=draft.timeline)
            db.execute("INSERT INTO save_keys VALUES (?,?,?,?,?)", (owner, idempotency_key, draft_id, trip_id, revision))
            return self.repository.version(db, owner, trip_id, revision)

    def read_trip(self, token, trip_id):
        with self.repository.connection() as db:
            return self.repository.version(db, self._owner(db, token), trip_id)

    def export_reference(self, token, trip_id):
        """Scope a version reference. Creating actual export files is a later module."""
        version = self.read_trip(token, trip_id)
        ref = uuid4().hex
        with self._memory_guard:
            self._exports[ref] = (version.owner_id, (version.trip_id, version.revision))
        return ref

    def open_export(self, token, ref):
        with self.repository.connection() as db:
            owner = self._owner(db, token)
            with self._memory_guard:
                trip_id, revision = self._scoped(self._exports, owner, ref)
            return self.repository.version(db, owner, trip_id, revision)

    async def turn(self, token, message, form=None, *, trip_id=None, session_id=None, conversation_factory=None, trace=None):
        from travel_agent import Conversation

        with self.repository.connection() as db:
            owner = self._owner(db, token)
            saved = self.repository.version(db, owner, trip_id) if trip_id is not None else None
        with self._memory_guard:
            session = self._scoped(self._sessions, owner, session_id) if session_id is not None else SessionContext()
        values = build_context({}, form, session, now=self.clock(), required=()).known_fields
        if saved:
            values = {**saved.fields, **values, "trip_id":saved.trip_id, "base_revision":saved.revision,
                      "locked_ids":[item.stable_id for item in saved.locked_items]}
        agent = conversation_factory() if conversation_factory else Conversation(typed_output=True, clock=self.clock)
        if saved and saved.timeline and len(saved.timeline["result"].get("days",[]))>1:
            tomorrow=saved.timeline["result"]["days"][1]["stops"]
            if tomorrow:
                values["next_stop_id"]=tomorrow[0]["entrance_id"]
        agent.homepage.update(values)
        options={}
        if self.candidate_skills is not None:
            from travel_candidate_context import CandidateScope

            scope=CandidateScope(owner,session_id) if session_id else None
            options.update(candidate_skills=self.candidate_skills,candidate_scope=scope)
        if saved and saved.timeline:
            async def patch_handler(arguments):
                date_scope=arguments.get("scope_date")
                if date_scope is None:
                    return None
                return await self.get_patch_service().preview(
                    token,saved.trip_id,scope_dates=[date_scope],instruction=message,
                    base_revision=saved.revision,trace=trace)
            options["patch_handler"]=patch_handler
        if saved:
            async def recheck_handler(_):
                return await self.get_recheck_service().agent_check(token,saved.trip_id,saved.revision,trace=trace)
            options["recheck_handler"]=recheck_handler
        if saved:
            async def export_handler(arguments):
                import asyncio
                return await asyncio.to_thread(self.get_guidebook_service().create,token,saved.trip_id,
                                      revision=saved.revision,format=arguments["format"],trace=trace)
            options["export_handler"] = export_handler
        return await agent.ask(message,trace,**options)

    def get_guidebook_service(self):
        with self._memory_guard:
            if self.guidebook_service is None:
                from travel_guidebook import ChromePdf, GuidebookService
                browser = os.getenv("TRAVEL_M11_PDF_BROWSER")
                self.guidebook_service = GuidebookService(self,pdf=ChromePdf(browser) if browser else None)
            return self.guidebook_service

    def get_recheck_service(self):
        with self._memory_guard:
            if self.recheck_service is None:
                from travel_recheck import RecheckService
                from travel_recheck_live import configured_providers
                self.recheck_service = RecheckService(self,configured_providers(self.clock),
                                                        auto_open=os.getenv("TRAVEL_M10_AUTO_OPEN","1")=="1")
            return self.recheck_service

    def get_patch_service(self):
        with self._memory_guard:
            if self.patch_service is None:
                from travel_patch import PatchService
                from travel_patch_indoor import indoor_plan

                self.patch_service=PatchService(self,indoor_plan)
            return self.patch_service

    async def choose_candidate(self,token,session_id,context_id,choice_id,*,trace=None):
        from travel_candidate_context import CandidateScope, ChoiceError

        with self.repository.connection() as db:
            owner=self._owner(db,token)
        try:
            with self._memory_guard:
                self._scoped(self._sessions,owner,session_id)
        except (NotFound,TypeError) as exc:
            raise ChoiceError("forbidden") from exc
        if self.candidate_skills is None:
            raise ChoiceError("forbidden")
        return await self.candidate_skills.choose(CandidateScope(owner,session_id),context_id,choice_id,trace)

