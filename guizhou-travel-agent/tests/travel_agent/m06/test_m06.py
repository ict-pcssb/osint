import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from support.guards import Trace, isolated_database
from travel_catalog import Catalog
from travel_contract import ContractError, validate_reply
from travel_evidence import EvidenceService, Material, ToolResult
from travel_rules import POLICY_VERSION, validate_itinerary, validate_report
from travel_fares import cost_line, quote_family, summarize_costs
from travel_state_app import TravelApplication
from travel_timeline import TimelineWorkflow, load_fixture
from travel_timeline_route import RouteAdapter

FIXTURE = json.loads((Path(__file__).parent.parent / "fixtures/m06/rules.json").read_text(encoding="utf-8"))
DAY = FIXTURE["date"]
CLOCK = FIXTURE["checked_at"]
TRACE = Trace("m06-fixture")
A, B, EAST, WEST, PARK = FIXTURE["entities"]


def at(wall):
    return f"{DAY}T{wall}:00+08:00"


def make_trip(**stop_fields):
    stop = dict(stop_id="stop-1", date=DAY, entrance_id=EAST, arrival_at=at("16:25"),
                incoming_leg=dict(from_id=A, to_id=EAST, expected_from_id=A, coordinate_system="WGS84"))
    stop.update(stop_fields)
    return dict(stops=[stop], data_mode="fixture", checked_at=CLOCK)


def validate(trip, evidence=(), locks=(), **kwargs):
    result = validate_itinerary(trip, evidence, locks, POLICY_VERSION, **kwargs)
    validate_report(result)
    TRACE.record("m06_rule_validation", fixture_inputs=trip, validation_report=result)
    return result


def rule(report, code):
    return next(row for row in report["rule_results"] if row["reason_code"] == code)


def ticket(**change):
    row = dict(item_id="tickets", category="ticket", unit="person", unit_fen=5000)
    return dict(row, **change)


def transit(**changes):
    return dict(boarding_id=A, alighting_id=EAST, direction="outbound", boarding_at=at("16:25"),
                estimate_min=40, estimate_mode="car", **changes)


def m5_packet(kind, value, direction=None, *, simulated=True, expired=False, subject=EAST):
    # Fixed replay of the M5 Claim/Evidence pair, including real-shaped metadata for trust boundary tests.
    claim = dict(claim_type=kind, subject_id=subject, target_date=DAY, line_direction=direction,
                 status="unconfirmed" if simulated else "confirmed", simulation_status="confirmed" if simulated else None,
                 value=None if simulated else value, evidence_ids=["test-" + kind], confidence_basis="unconfirmed" if simulated else "official_original")
    row = dict(evidence_id="test-" + kind, data_mode="fixture" if simulated else "live",
               source_kind="operator", freshness="current", expires_at=CLOCK if expired else "2026-11-26T10:30:00+08:00",
               effective_from=DAY, effective_to=DAY, retrieved_at=CLOCK, excerpt=f"2026-11-28 {value}")
    return dict(claim=claim, evidence=[row])


class Module06(unittest.TestCase):
    def test_01_entity_ids_and_coordinate_order(self):
        for dest, entrance, source in [(WEST,EAST,A),(PARK,EAST,A),(EAST,EAST,B)]:
            with self.subTest(dest=dest, source=source):
                trip = make_trip()
                trip["stops"][0]["incoming_leg"].update(to_id=dest, from_id=source)
                self.assertEqual(rule(validate(trip), "ENTITY_MISMATCH")["status"], "fail")
        trip = make_trip(reservation={"entrance_id":WEST})
        self.assertEqual(rule(validate(trip), "ENTITY_MISMATCH")["path"], "/stops/stop-1/reservation/entrance_id")
        self.assertEqual(rule(validate(make_trip(entrance_id=None)), "ENTRANCE_REQUIRED")["status"], "pending")
        trip = make_trip()
        trip["stops"][0]["incoming_leg"].update(coordinate_system="GCJ02", starts_at=at("15:00"), travel_min=40)
        result = validate(trip)
        self.assertEqual(rule(result, "COORDINATE_SYSTEM_UNKNOWN")["status"], "pending")
        self.assertNotIn("arrival_at", result["time_details"]["stop-1"])
        trip["stops"][0]["incoming_leg"].update(coordinate_system="WGS84", from_coordinates=[106,26])
        self.assertEqual(rule(validate(trip), "COORDINATE_SYSTEM_UNKNOWN")["status"], "pending")

    def test_02_midnight_and_legacy_date(self):
        trip = make_trip(arrival_at=None)
        leg = trip["stops"][0]["incoming_leg"]
        leg.update(starts_at=f"{DAY}T23:40:00", travel_min=50)
        result = validate(trip)
        self.assertEqual(result["time_details"]["stop-1"]["arrival_at"], "2026-11-29T00:30:00+08:00")
        self.assertTrue(result["time_details"]["stop-1"]["legacy_beijing_time"])
        trip["stops"][0]["arrival_at"] = at("00:30")
        self.assertEqual(rule(validate(trip), "TIME_CONFLICT")["status"], "fail")
        self.assertEqual(trip["stops"][0]["arrival_at"], at("00:30"))
        leg["starts_at"] = "23:40"
        self.assertEqual(rule(validate(trip), "TIME_UNVERIFIED")["status"], "pending")
        leg.update(starts_at=at("23:40"), travel_min=-1)
        self.assertEqual(rule(validate(trip), "INVALID_DURATION")["status"], "fail")

    def test_03_reservation_cutoff_includes_buffer(self):
        trip = make_trip(reservation=dict(entrance_id=EAST, cutoff_at=at("16:30"), buffer_min=15,
                                          buffer_source="user_confirmed", cutoff_confirmed=True))
        result = validate(trip)
        self.assertEqual(result["time_details"]["stop-1"]["latest_safe_arrival"], at("16:15"))
        self.assertEqual(result["time_details"]["stop-1"]["reservation_gap_min"], 10)
        self.assertIn("请选择", rule(result, "RESERVATION_WINDOW_MISSED")["suggested_action"])
        trip["stops"][0]["reservation"]["cutoff_at"] = "2026-11-29T16:30:00+08:00"
        self.assertEqual(rule(validate(trip), "RESERVATION_UNVERIFIED")["status"], "pending")
        trip["stops"][0]["reservation"].pop("buffer_source")
        self.assertEqual(rule(validate(trip), "RESERVATION_UNVERIFIED")["status"], "pending")

    def test_04_map_estimate_cannot_confirm_last_service(self):
        trip = make_trip(last_service=transit())
        for packets in [[], [m5_packet("last_service","23:00","inbound",simulated=False)],
                        [m5_packet("last_service","23:00","outbound",simulated=False,expired=True)],
                        [m5_packet("last_service","23:00","outbound",simulated=False,subject=WEST)]]:
            result = validate(trip, packets)
            self.assertEqual(rule(result, "LAST_SERVICE_UNVERIFIED")["status"], "pending")
            self.assertEqual(result["time_details"]["stop-1"]["last_service"]["boarding_id"], A)
            self.assertEqual(result["time_details"]["stop-1"]["last_service"]["estimate_min"], 40)

        trip["stops"][0]["last_service"]["boarding_id"] = B
        result = validate(trip, [m5_packet("last_service","23:00","outbound",simulated=False)])
        self.assertEqual(rule(result, "LAST_SERVICE_UNVERIFIED")["status"], "pending")

    def test_05_buffers_once_unknown_wait(self):
        trip = make_trip()
        leg = trip["stops"][0]["incoming_leg"]
        leg.update(starts_at=at("15:00"), segments_min=deepcopy(FIXTURE["segments"]))
        details = validate(trip)["time_details"]["stop-1"]
        self.assertEqual(details["arrival_at"], at("16:35"))
        self.assertEqual(details["segments_min"], FIXTURE["segments"])
        leg["segments_min"].pop("wait")
        self.assertEqual(rule(validate(trip), "WAIT_TIME_UNVERIFIED")["status"], "pending")
        leg["wait_estimate_min"] = [10,25]
        details = validate(trip)["time_details"]["stop-1"]
        self.assertEqual(details["arrival_at"], at("16:40"))
        self.assertEqual(details["arrival_range"], [at("16:25"),at("16:40")])
        self.assertEqual(details["timing_status"], "estimate")
        trip["stops"][0]["arrival_at"] = at("16:30")
        self.assertEqual(validate(trip)["rule_summary"],"pending")

    def test_06_fare_boundaries(self):
        for mm, expected in [(1199,29000),(1200,34000),(1500,39000)]:
            with self.subTest(height=mm):
                lines, question = quote_family(2, [mm], FIXTURE["fare_policy"], FIXTURE["shuttle_fen"])
                self.assertEqual(summarize_costs(lines)["total_fen"], expected)
                self.assertFalse(question)
                self.assertEqual({row["unit"] for row in lines}, {"person"})
                self.assertEqual({row["data_mode"] for row in lines}, {"fixture"})
                self.assertEqual(lines[1]["price_status"], "free_by_policy" if mm==1199 else "known")
                self.assertEqual(lines[-1]["subtotal_fen"], 9000)
                TRACE.record("m06_family_fare", height_mm=mm, policy=FIXTURE["fare_policy"], cost=summarize_costs(lines))

    def test_07_optional_terms_or_mandatory_unknown(self):
        policy = deepcopy(FIXTURE["fare_policy"])
        policy["child_bands"][0]["op"] = "<="
        for h, p, shuttle, total, lower, question in [
            (1200,policy,3000,29000,29000,None), (1200,None,3000,None,9000,None),
            (1200,policy,None,None,20000,None), (None,policy,3000,None,29000,"child_height_mm"),
        ]:
            with self.subTest(h=h, policy=p is not None, shuttle=shuttle):
                lines, ask = quote_family(2,[h],p,shuttle)
                summary = summarize_costs(lines)
                self.assertEqual((summary["total_fen"],summary["known_lower_fen"],ask),(total,lower,question))
                self.assertTrue(all(row["unit_fen"] is None for row in lines if row["price_status"] == "unknown"))
                TRACE.record("m06_fare_gap", height_mm=h, policy=p, shuttle_fen=shuttle, cost=summary, question=ask)

        age_policy = deepcopy(policy)
        age_policy["child_bands"] = [{"op":"<","age_years":7,"unit_fen":0},{"op":"otherwise","unit_fen":10000}]
        lines, ask = quote_family(2,[None],age_policy,3000,child_ages=[6])
        self.assertEqual((summarize_costs(lines)["total_fen"],ask),(29000,None))
        bad = dict(age_policy, child_bands=[{"op":"<","height_mm":1200,"age_years":7,"unit_fen":0}])
        with self.assertRaises(ContractError):
            quote_family(2,[1199],bad,3000,child_ages=[6])

    def test_08_budget_scope_units_and_currency(self):
        rows = [cost_line(ticket(item_id="car",unit="vehicle",unit_fen=10000), people=2),
                cost_line(ticket(item_id="hotel",unit="room_night",unit_fen=20000,rooms=2,nights=1), people=2),
                cost_line(ticket(), people=2)]
        for scope, cap in [("group",80000),("per_person",160000)]:
            budget = summarize_costs(rows,80000,scope,2)
            self.assertEqual((budget["cap_fen"],budget["total_fen"],budget["original_budget_fen"]),(cap,60000,80000))
            TRACE.record("m06_budget_summary", summary=budget)
        foreign = rows + [cost_line(ticket(item_id="extra",currency="USD"), people=1)]
        summary = summarize_costs(foreign,80000,"group",2)
        self.assertIsNone(summary["total_fen"])
        self.assertEqual(summary["totals_by_currency"], {"CNY":60000,"USD":5000})
        TRACE.record("m06_budget_summary", summary=summary)
        incomplete = summarize_costs(rows+[cost_line(ticket(item_id="missing",unit_fen=None))],50000,"group",2)
        self.assertEqual((incomplete["budget_status"],incomplete["total_fen"]),("over",None))
        old_plan = make_trip()
        old_plan.update(budget_fen=80000,budget_scope="person",adults=1,children=1,cost_lines=rows)
        old_budget = validate(old_plan)["cost_summary"]
        self.assertEqual((old_budget["cap_fen"],old_budget["original_budget_fen"],old_budget["original_budget_scope"]),
                         (160000,80000,"per_person"))
        legacy = cost_line(ticket(quantity=3), people=3)
        self.assertEqual(legacy["eligibility"]["status"],"legacy_grouping_unknown")

    def test_09_locks_and_affected_scope(self):
        locked = FIXTURE["locked"]
        trip = make_trip()
        for candidate in [[], [dict(locked, fields={"departure_at":"2026-11-29T19:00:00+08:00"})]]:
            trip["locked_items"] = candidate
            self.assertEqual(rule(validate(trip, locks=[locked]), "LOCKED_ITEM_CHANGED")["status"], "fail")
        f = load_fixture()
        with TemporaryDirectory(prefix="travel-m06-") as tmp:
            root = Path(tmp)
            db = self.enterContext(isolated_database(root/"travel_m06.sqlite3", root))
            app = TravelApplication(db, clock=lambda:datetime.fromisoformat(CLOCK))
            app.create_account("test_a","module06-pass")
            token = app.login("test_a","module06-pass")
            wf = TimelineWorkflow(app,f.catalog,RouteAdapter(f.route,app.clock,fixture=True),f.costs)
            packet = asyncio.run(wf.preview(token,f.form))
            original = packet["result"]
            before = deepcopy(original["locked_items"])
            draft_id = original["draft_id"]
            candidate = deepcopy(packet)
            candidate["result"]["locked_items"][0]["departure_at"] = "2026-11-29T19:00:00+08:00"
            with self.assertRaises(ValueError):
                app.attach_timeline(token,draft_id,candidate,fixture_mode=True)
            candidate["result"]["locked_items"] = []
            with self.assertRaises(ValueError):
                app.attach_timeline(token,draft_id,candidate,fixture_mode=True)
            changed = wf.edit(token,draft_id,"d2-park","13:05","14:20")["result"]
            invalid = wf.edit(token,draft_id,"d2-park","18:00","18:25")["result"]
            with self.assertRaises(ValueError):
                wf.save(token,invalid["draft_id"],"conflict",confirmed=True)
            self.assertEqual(changed["validation_report"]["checked_scope"],"affected")
            self.assertEqual(changed["validation_report"]["affected_stop_ids"],["d2-park"])
            self.assertEqual(changed["locked_items"],before)
            self.assertEqual(changed["days"][0],original["days"][0])
            with app.repository.connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM trip_versions").fetchone()[0],0)
            with patch.dict("os.environ", {"TRAVEL_RULES_POLICY_VERSION":"disabled"}):
                with self.assertRaises(ValueError):
                    wf.save(token,changed["draft_id"],"paused",confirmed=True)
            saved = wf.save(token,changed["draft_id"],"good",confirmed=True)
            self.assertEqual(saved.revision,1)
            self.assertEqual(saved.timeline["result"]["validation_report"],changed["validation_report"])
            self.assertEqual(saved.locked_items[0].lock_revision,before[0]["lock_revision"])
            class WrongEndpointAdapter(RouteAdapter):
                async def route(self, start, end, mode):
                    result = await super().route(start,end,mode)
                    return replace(result, to_id="museum-entry") if end.id == "wenchang-entry" else result

            missing_costs = [dict(item, unit_fen=None) if item["item_id"] == "lodging" else item for item in f.costs]
            mixed_flow = TimelineWorkflow(app,f.catalog,WrongEndpointAdapter(f.route,app.clock,fixture=True),missing_costs)
            mixed = asyncio.run(mixed_flow.preview(token,f.form))
            validate_reply(mixed,fixture_mode=True)
            self.assertEqual(mixed["run_status"],"completed")
            mixed_result = mixed["result"]
            mixed_report = mixed_result["validation_report"]
            self.assertEqual(mixed_result["feasibility"],"conflict")
            self.assertIn("d1-wenchang",rule(mixed_report,"ENTITY_MISMATCH")["path"])
            self.assertIsNone(mixed_result["days"][0]["stops"][1]["incoming_leg"]["duration_s"])
            self.assertIsNone(mixed_result["budget"]["total_fen"])
            self.assertEqual(rule(mixed_report,"COST_UNVERIFIED")["status"],"pending")
            with self.assertRaises(ValueError):
                mixed_flow.save(token,mixed_result["draft_id"],"mixed",confirmed=True)
            TRACE.record("m06_mixed_timeline_api", validation_report=mixed_report,
                         rejected_adapter_end_id="museum-entry", expected_end_id="wenchang-entry")

            lock = saved.locked_items[0]
            TRACE.record("m06_lock_snapshot", before=before[0], after=dict(lock_id=lock.stable_id,
                         lock_revision=lock.lock_revision, departure_at=lock.fields["departure_at"]),
                         saved_revision=saved.revision, affected_stop_ids=changed["validation_report"]["affected_stop_ids"],
                         validation_report=changed["validation_report"], rejected_conflict=invalid["validation_report"])
            forged = deepcopy(packet)
            forged["result"]["validation_report"]["feasibility"] = "verified"
            with self.assertRaises(ContractError):
                validate_reply(forged, fixture_mode=True)

    def test_10_m5_replay_and_independent_feasibility(self):
        trip = make_trip(last_service=transit(fixture_last_departure="23:00"), departure_at=at("17:00"))
        trip["stops"][0]["incoming_leg"].update(starts_at=at("15:15"), travel_min=40)
        for key, start, arrival, leave in [("stop-2","17:00","17:20","17:35"),("stop-3","17:35","17:55","18:05")]:
            trip["stops"].append(dict(stop_id=key, date=DAY, entrance_id=EAST, arrival_at=at(arrival),
                                      departure_at=at(leave), incoming_leg={"from_id":EAST,"to_id":EAST,
                                      "expected_from_id":EAST,"starts_at":at(start),"travel_min":15,
                                      "coordinate_system":"WGS84"}))
        trip["cost_lines"] = [cost_line(ticket(),people=2,data_mode="fixture")]
        catalog = Catalog([dict(id=EAST, kind="line_direction", label="甲线",
                               directions={"outbound":"去东门"}, authority_hosts=["operator.example"])])
        url = "https://operator.example/notice"
        async def reader(_url):
            material = Material(url=url, text="2026-11-28 甲线去东门末班车为23:00。", retrieved_at=CLOCK,
                                source_kind="operator", data_mode="fixture")
            return ToolResult("success", material, provider="test-fixture")
        service = EvidenceService(catalog, clock=lambda:datetime.fromisoformat(CLOCK), reader=reader)
        packet = asyncio.run(service.verify_claim(EAST,"last_service",DAY,line_direction="outbound",known_urls=[url]))
        self.assertEqual(packet["claim"]["simulation_status"],"confirmed")
        good = validate(trip,[packet])
        self.assertEqual(rule(good,"LAST_SERVICE_CHECKED")["evidence_ids"], packet["claim"]["evidence_ids"])
        self.assertEqual(good["affected_stop_ids"],["stop-1","stop-2","stop-3"])
        self.assertEqual(good["time_details"]["stop-3"]["arrival_at"],at("17:50"))
        self.assertEqual(packet["claim"]["value"], None)
        TRACE.record("m06_m5_evidence_replay", claim=packet["claim"], evidence=packet["evidence"])
        trip["cost_lines"][0] = cost_line(ticket(unit_fen=None),people=2,data_mode="fixture")
        missing = validate(trip)
        trip["stops"][0]["incoming_leg"]["to_id"] = WEST
        wrong = validate(trip)
        self.assertEqual([row["run_status"] for row in (good,missing,wrong)],["completed"]*3)
        self.assertEqual([row["rule_summary"] for row in (good,missing,wrong)],["pass","pending","fail"])
        self.assertEqual([row["feasibility"] for row in (good,missing,wrong)],["needs_verification"]*2+["conflict"])
        self.assertIsNone(wrong["cost_summary"]["total_fen"])
        self.assertEqual(rule(wrong,"ENTITY_MISMATCH")["path"],"/stops/stop-1/incoming_leg/to_id")
        self.assertEqual(rule(wrong,"COST_UNVERIFIED")["status"],"pending")
        trip = make_trip(last_service=transit())
        trip["data_mode"] = "snapshot"
        trip["cost_lines"] = []
        self.assertEqual(validate(trip)["feasibility"],"needs_verification")
        self.assertEqual(validate(trip,[m5_packet("last_service","23:00","outbound",simulated=False)])["feasibility"],"verified")
        trip["cost_lines"] = [cost_line(ticket(),people=2,data_mode="snapshot")]
        uncertain = validate(trip,[m5_packet("last_service","23:00","outbound",simulated=False)])
        self.assertIsNone(uncertain["cost_summary"]["total_fen"])
        self.assertEqual(rule(uncertain,"COST_SOURCE_UNVERIFIED")["status"],"pending")
        edited = deepcopy(good)
        edited["feasibility"] = "verified"
        with self.assertRaises(ContractError):
            validate_report(edited)


if __name__ == "__main__":
    unittest.main()
