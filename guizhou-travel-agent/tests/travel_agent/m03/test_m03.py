import asyncio
from contextlib import closing, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import sys
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from support.guards import Trace, offline_guard, isolated_database
from travel_agent import Conversation
from travel_state import SessionContext, build_context, lock_arrangement, merge_arrangements, resolve_date
from travel_state_app import AuthRequired, Conflict, NotFound, TravelApplication

NOW = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
PLAN = {"destination":"贵阳", "adults":2, "days":2, "budget_scope":"group", "budget_fen":80000}
DATES = {"start_date":"2026-11-28","end_date":"2026-11-29"}
RETURN = {"kind":"rail", "fields":{"purpose":"return","departure_at":"2026-11-29T18:30:00+08:00"}}


def model_decision(kind, **params):
    return json.dumps(dict(intent=kind, confidence_band="high", requested_outputs=[kind], parameters=params))


class ContextTests(unittest.TestCase):
    def test_m3_01_field_priority_and_provenance(self):
        c = build_context({"adults":3}, {"adults":2},
            SessionContext({"origin":"上海","intended_region":"贵阳","current_location":"贵阳北"}), {}, now=NOW)
        self.assertEqual({key:c.known_fields[key] for key in ("origin","destination","current_location","adults")},
                         {"origin":"上海","destination":"贵阳","current_location":"贵阳北","adults":3})
        self.assertEqual(c.provenance["adults"].source, "current_message")
        self.assertEqual(c.provenance["destination"].input_field, "intended_region")
        self.assertEqual(c.provenance["adults"].updated_at, NOW.isoformat())
        self.assertNotIn("adults", c.missing_fields)

    def test_m3_02_single_question_at_existing_turn(self):
        model = AsyncMock(return_value=model_decision("itinerary", adults=2, days=2, group_cap_fen=80000))
        agent = Conversation(homepage=PLAN, typed_output=True, intent_model=model, clock=lambda: NOW)
        missing = asyncio.run(agent.ask("帮我安排贵阳两人两天，全团800元。"))
        self.assertEqual(missing["result"]["field"], "start_date")
        self.assertEqual(missing["result"]["question"].count("？"), 1)
        agent.homepage.update(DATES)
        ready = asyncio.run(agent.ask("帮我安排贵阳两人两天，全团800元。"))
        self.assertNotEqual(ready["result"]["kind"], "clarification")
        self.assertEqual(agent.typed.last_context.known_fields["group_cap_fen"], 80000)
        per_person = build_context({"budget_scope":"person","budget_fen":80000}, {"adults":2}, now=NOW)
        self.assertEqual(per_person.known_fields["group_cap_fen"], 160000)
        revised = build_context({"group_cap_fen":90000}, PLAN, now=NOW)
        self.assertEqual(revised.known_fields["group_cap_fen"], 90000)

    def test_m3_03_date_reference_and_timezone(self):
        for host in (timezone.utc, timezone(timedelta(hours=8))):
            with self.subTest(host=host):
                clock = NOW.astimezone(host)
                self.assertEqual(resolve_date("那天", clock, "2026-11-28"), "2026-11-28")
                self.assertEqual(resolve_date("明天", clock), "2026-11-27")
                self.assertIsNone(resolve_date("那天", clock))
                unresolved = build_context({"date":"那天"}, now=clock, required=("date",))
                self.assertEqual((unresolved.timezone, unresolved.next_field), ("Asia/Shanghai","date"))
        model = AsyncMock(return_value=model_decision("weather", date="那天"))
        agent = Conversation(homepage={"place_id":"qingyan"}, typed_output=True, intent_model=model, clock=lambda: NOW)
        missing = asyncio.run(agent.ask("那天下雨吗？"))
        self.assertEqual(missing["result"]["field"], "date")
        asyncio.run(agent.ask("我周六准备去青岩。"))
        answered = asyncio.run(agent.ask("那天下雨吗？"))
        self.assertEqual(answered["result"]["target_date"], "2026-11-28")

    def test_m3_05_lock_conflict_preserves_hash(self):
        lock = lock_arrangement(RETURN, confirmed=True, now=NOW)
        self.assertEqual((lock.confirmed_by, lock.confirmation, lock.locked), ("user","user_confirmed",True))
        untouched, question = merge_arrangements((lock,), {})
        self.assertEqual(untouched[0].fields_hash, lock.fields_hash)
        self.assertIsNone(question)
        blocked, question = merge_arrangements((lock,), {lock.stable_id:"晚点走"})
        self.assertEqual(blocked[0].fields_hash, lock.fields_hash)
        self.assertEqual(question.count("？"), 1)
        with self.assertRaises(ValueError):
            lock_arrangement({"kind":"rail","fields":{"ticket_number":"private"}}, confirmed=True, now=NOW)
        with self.assertRaises(ValueError):
            lock_arrangement(RETURN, confirmed=False, now=NOW)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.trace = Trace("m03")
        self.enterContext(offline_guard(self.trace))
        directory = TemporaryDirectory(prefix="travel-m03-")
        dir_path = Path(directory.name)
        self.addCleanup(self.assert_database_removed, dir_path)
        self.enterContext(directory)
        self.path = dir_path / "travel_m03.sqlite3"
        self.enterContext(isolated_database(self.path, dir_path))
        self.app = TravelApplication(self.path, clock=lambda: NOW)
        self.login_specs = [("account_a", "local-test-password-a"), ("account_b", "local-test-password-b")]
        self.accounts = [self.app.create_account(*identity) for identity in self.login_specs]
        self.a, self.b = (self.app.login(*identity) for identity in self.login_specs)

    def assert_database_removed(self, directory):
        self.assertFalse(directory.exists())
        self.trace.record("isolated_test_database_removed", removed=True)

    def rows(self, sql, params=()):
        with closing(sqlite3.connect(str(self.path))) as db, db:
            return db.execute(sql, params).fetchall()

    def snapshot(self):
        with closing(sqlite3.connect(str(self.path))) as db:
            return tuple(db.iterdump())

    def counts(self):
        return tuple(self.rows(f"SELECT count(*) FROM {table}")[0][0] for table in
                     ("trips", "trip_versions", "save_keys", "preferences", "consents", "auth_sessions"))

    def draft(self, token=None, **kwargs):
        return self.app.preview_trip(token or self.a, PLAN, DATES, **kwargs)

    def test_m3_04_authorization_and_switch(self):
        self.rows("INSERT INTO preferences VALUES (?, ?)", (self.accounts[0], json.dumps({"origin":"上海","walking_preference":"少步行"})))
        real_read = self.app.repository.read_preferences
        spy = Mock(wraps=real_read)
        self.app.repository.read_preferences = spy
        baseline = self.counts()
        self.assertNotIn("origin", self.app.preview_trip(self.a,PLAN,DATES,use_preferences=True).fields)
        spy.assert_not_called()
        for i, (authorized, enabled) in enumerate(((False,False),(True,False),(False,True),(True,True))):
            self.app.set_consent(self.a, authorized=authorized, enabled=enabled, fields=("origin",))
            result = self.app.preview_trip(self.a, PLAN, DATES, use_preferences=True)
            self.assertEqual(spy.call_count, int(i == 3))
            self.assertEqual(result.fields.get("origin"), "上海" if i == 3 else None)
            self.assertNotIn("walking_preference", result.fields)
        self.app.set_consent(self.a, authorized=False, enabled=True, fields=())
        self.assertNotIn("origin", self.app.preview_trip(self.a,PLAN,DATES,use_preferences=True).fields)
        self.assertEqual(spy.call_count, 1)
        self.assertNotIn("origin", self.app.preview_trip(self.b,PLAN,DATES,use_preferences=True).fields)
        self.assertEqual(self.counts()[:4], baseline[:4])
        with self.assertRaises(AuthRequired):
            self.app.update_preferences(self.a, {"origin":"贵阳"})
        with self.assertRaises(AuthRequired):
            self.app.update_preferences(self.b, {"origin":"贵阳"})
        self.app.set_consent(self.a, authorized=True, enabled=True, fields=("origin",))
        self.app.update_preferences(self.a, {"origin":"贵阳"})
        self.assertEqual(self.app.preview_trip(self.a,PLAN,DATES,use_preferences=True).fields["origin"], "贵阳")

    def test_m3_06_real_authentication_and_cross_account_rejection(self):
        with self.assertRaises(AuthRequired):
            self.app.login("account_a", "wrong-password")
        with self.assertRaises(AuthRequired):
            self.app.read_trip(self.accounts[0], "missing")
        session_id = self.app.open_session(self.a, {"current_location":"贵阳北"})
        draft = self.draft(session_id=session_id)
        saved = self.app.create_trip(self.a, draft.draft_id, save=True, idempotency_key="save-a")
        ref = self.app.export_reference(self.a, saved.trip_id)
        checks = [
            lambda: self.app.read_trip(self.b, saved.trip_id),
            lambda: self.app.open_export(self.b, ref),
            lambda: self.app.preview_trip(self.b, PLAN, DATES, session_id=session_id),
            lambda: self.app.create_trip(self.b, draft.draft_id, save=True, idempotency_key="save-a"),
            lambda: self.app.preview_trip(self.b, PLAN, DATES, trip_id=saved.trip_id),
        ]
        before = self.counts()
        for call in checks:
            with self.assertRaises(NotFound):
                call()
        self.assertEqual(self.counts(), before)
        self.trace.record("owner_isolation", status=404, denied_count=len(checks), rows_unchanged=True)
        self.assertEqual(self.app.read_trip(self.a, saved.trip_id).fields["current_location"], "贵阳北")
        self.assertEqual(self.app.open_export(self.a, ref), saved)
        self.rows("INSERT INTO trips (trip_id,owner_id) VALUES (?,NULL)", ("orphan",))
        with self.assertRaises(NotFound):
            self.app.read_trip(self.a, "orphan")
        from travel_state_cli import dispatch
        b_trip = dispatch(self.app, self.b, {"action":"save","draft_id":self.draft(self.b).draft_id,
                                             "save":True,"idempotency_key":"save-a"})
        self.assertNotEqual(b_trip.trip_id, saved.trip_id)
        from travel_agent import main
        console_output = io.StringIO()
        read_command = json.dumps({"action":"read","trip_id":saved.trip_id})
        before_console = self.counts()
        with patch.object(sys, "argv", ["travel_agent.py", "--state-db", str(self.path)]), \
                patch("builtins.input", side_effect=["account_a", read_command, ""]), \
                patch("travel_state_cli.getpass", return_value=self.login_specs[0][1]), redirect_stdout(console_output):
            asyncio.run(main())
        self.assertIn(saved.trip_id, console_output.getvalue())
        self.assertNotIn(self.login_specs[0][1], console_output.getvalue())
        self.assertEqual(self.counts(), before_console)
        self.trace.record("real_console_login_read_logout", authenticated=True, row_counts_unchanged=True)

    def test_m3_07_idempotency_after_lost_response_and_real_rollback(self):
        before = self.counts()
        draft = self.draft()
        with self.assertRaises(ValueError):
            self.app.create_trip(self.a, draft.draft_id, save=False, idempotency_key="no")
        self.assertEqual(self.counts(), before)
        self.rows("""CREATE TRIGGER reject_test_save BEFORE INSERT ON save_keys
                  BEGIN SELECT RAISE(ABORT, 'test rollback'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.app.create_trip(self.a, draft.draft_id, save=True, idempotency_key="same")
        self.assertEqual(self.counts(), before)
        self.trace.record("database_rollback", after=list(self.counts()[:3]), engine=sqlite3.sqlite_version)
        self.rows("DROP TRIGGER reject_test_save")

        def lose_response():
            self.app.create_trip(self.a, draft.draft_id, save=True, idempotency_key="same")
            raise TimeoutError("fixture: lost response after database commit")
        with self.assertRaises(TimeoutError):
            lose_response()
        with ThreadPoolExecutor(max_workers=2) as executor:
            def retry(_):
                app = TravelApplication(self.path, clock=lambda: NOW)
                return app.create_trip(self.a, draft.draft_id, save=True, idempotency_key="same")
            versions = list(executor.map(retry, range(2)))
        self.assertEqual(versions[0], versions[1])
        self.assertEqual(versions[0].revision, 1)
        self.assertEqual(self.counts()[:3], (1,1,1))
        self.trace.record("idempotency_verified", response_lost=True, retries=2, rows=list(self.counts()[:3]), revision=1)
        with self.assertRaises(Conflict):
            self.app.create_trip(self.a, self.draft().draft_id, save=True, idempotency_key="same")

    def test_m3_08_read_only_turn_keeps_saved_revision_and_lock(self):
        draft = self.draft(arrangements=(RETURN,), confirm_arrangements=True)
        saved = self.app.create_trip(self.a,draft.draft_id,save=True,idempotency_key="save")
        before = self.snapshot()
        weather = AsyncMock(side_effect=TimeoutError)
        model = AsyncMock(return_value=model_decision("weather", date="明天"))
        trace = Trace("m03-readonly")
        inputs = {"place_id":"qingyan"}
        def conversation():
            return Conversation(typed_output=True, intent_model=model, handlers={"get_destination_weather":weather},
                                clock=lambda: NOW, fixture_mode=True)
        rain = asyncio.run(self.app.turn(self.a,"明天青岩下雨吗？",inputs,trip_id=saved.trip_id,
                                         conversation_factory=conversation, trace=trace))
        spoken = asyncio.run(self.app.turn(self.a,"我周六准备去青岩。",inputs,trip_id=saved.trip_id,
                                           conversation_factory=conversation, trace=trace))
        self.assertEqual((rain["run_status"], spoken["result"]["kind"]), ("partial","ack"))
        weather.assert_awaited_once()
        reread = self.app.read_trip(self.a, saved.trip_id)
        self.assertEqual((reread.revision, reread.locked_items[0].fields_hash),
                         (saved.revision, saved.locked_items[0].fields_hash))
        self.assertEqual(self.snapshot(), before)
        self.trace.record("read_only_db_unchanged", revision=reread.revision, lock_hash_matches=True)
        self.assertTrue(all(e["write_count"]==0 for e in trace.events if e["event"]=="turn_result"))


    
    def test_m3_saved_version_lock_and_stale_draft(self):
        base = self.draft(arrangements=(RETURN,), confirm_arrangements=True)
        saved = self.app.create_trip(self.a, base.draft_id, save=True, idempotency_key="first")
        previous = self.snapshot()
        self.rows("""CREATE TRIGGER reject_test_save BEFORE INSERT ON save_keys
                  BEGIN SELECT RAISE(ABORT, 'test rollback'); END""")
        next_draft = self.app.preview_trip(self.a, {"group_cap_fen":90000}, trip_id=saved.trip_id)
        stale = self.app.preview_trip(self.a, {"group_cap_fen":95000}, trip_id=saved.trip_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.app.create_trip(self.a, next_draft.draft_id, save=True, idempotency_key="second")
        self.rows("DROP TRIGGER reject_test_save")
        self.assertEqual(self.snapshot(), previous)
        unchanged, = self.app.read_trip(self.a, saved.trip_id).locked_items
        lock_change = self.app.preview_trip(self.a, {}, trip_id=saved.trip_id,
                                            arrangement_changes={unchanged.stable_id:"晚点走"})
        self.assertIsNotNone(lock_change.next_question)
        response = asyncio.run(self.app.turn(self.a, "我想晚点走。", trip_id=saved.trip_id,
            conversation_factory=lambda: Conversation(typed_output=True, clock=lambda: NOW,
                                                      intent_model=AsyncMock(return_value=model_decision("itinerary")))))
        self.assertEqual((response["result"]["kind"], response["result"]["field"]), ("clarification", "locked_ids"))
        with self.assertRaises(ValueError):
            self.app.create_trip(self.a, lock_change.draft_id, save=True, idempotency_key="change-lock")
        second = self.app.create_trip(self.a, next_draft.draft_id, save=True, idempotency_key="second")
        with self.assertRaises(Conflict):
            self.app.create_trip(self.a, stale.draft_id, save=True, idempotency_key="stale")
        self.assertEqual((second.revision, second.fields["group_cap_fen"]), (2,90000))
        self.assertEqual(second.locked_items[0].fields_hash, unchanged.fields_hash)
        self.assertEqual(self.app.create_trip(self.a, base.draft_id, save=True,idempotency_key="first"), saved)
        self.trace.record("version_and_lock_verified", revision=second.revision, stale_status=409, lock_hash_matches=True)
