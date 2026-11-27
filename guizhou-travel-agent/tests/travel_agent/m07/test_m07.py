import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from mcp.types import CallToolResult, TextContent

from support.guards import Trace, isolated_database
from travel_agent import Conversation
from travel_catalog import Catalog
from travel_state_app import TravelApplication
from travel_transport import FlightAdapter, RailAdapter, WeatherAdapter
from travel_transport_skills import TransportSkills, route_waypoints, transfer_packet, transport_card
from travel_transport_live import transport_status, unbox

FX = json.loads((Path(__file__).parent.parent/"fixtures/m07/transport.json").read_text(encoding="utf-8"))
CLOCK = datetime.fromisoformat(FX["clock"])
DAY = "2026-11-28"
TRACE = Trace("m07-fixture")


def run(action):
    return asyncio.run(action)


def decision(kind, parameters):
    async def model(*_):
        return json.dumps(dict(intent=kind, parameters=parameters, requested_outputs=[kind], confidence_band="high"))
    return model


def flight_raw(*, backup=False, status="ok"):
    # Fixed shapes based on the registered primary and documented backup tool.
    legs = ([dict(departure_airport="PVG",arrival_airport="KWE",departure_time=DAY+" 09:00",
                  arrival_time=DAY+" 12:00",airline_code="MU",flight_number="5500")] if backup else
            [dict(origin="PVG",destination="KWE",depart_local=DAY+"T09:00",
                  arrive_local=DAY+"T12:00",flight_number="MU5500")])
    row = dict(price=500,currency="CNY",legs=legs)
    return (dict(success=True,count=1,flights=[row]) if backup else
            dict(origin="PVG",destination="KWE",currency="CNY",adults=2,dates_not_searched=[],
                 days=[dict(date=DAY,status=status)],flights=[row] if status=="ok" else []))


class Module07(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(FX["entities"])
        self.clock = lambda:CLOCK

    def weather(self, call):
        return WeatherAdapter(self.catalog,call,self.clock,mode="fixture")

    def agent(self, kind, params, skills):
        return Conversation(typed_output=True,clock=self.clock,fixture_mode=True,
                            intent_model=decision(kind,params),transport_skills=skills)

    def test_01_direct_weather_zero_writes(self):
        call = AsyncMock(return_value=FX["weather"])
        skills = TransportSkills(self.catalog,self.clock,weather=self.weather(call),mode="fixture")
        with TemporaryDirectory(prefix="travel-m07-") as tmp:
            root=Path(tmp)
            with isolated_database(root/"travel_m07.sqlite3",root) as path:
                app=TravelApplication(path,clock=self.clock)
                app.create_account("m07_local","only-fixture-account")
                token=app.login("m07_local","only-fixture-account")
                agent=self.agent("weather",{"place_id":"qingyan"},skills)
                packet=run(app.turn(token,"明天青岩下雨吗？",conversation_factory=lambda:agent,trace=TRACE))
                with app.repository.connection() as db:
                    self.assertEqual([db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                                      for t in ("trips","trip_versions","save_keys")],[0,0,0])
        card=packet["result"]
        self.assertEqual((card["kind"],card["target_date"],card["forecast"]["rain_probability_pct"]),
                         ("weather","2026-11-27",70))
        self.assertEqual((card["place"]["coverage"],card["forecast"]["units"]),("花溪区预报",{"temperature":"°C","probability":"%"}))
        self.assertTrue(card["as_of"] and card["action"])
        self.assertNotIn("trip_id",card)
        TRACE.record("m07_weather_packet",packet=packet,write_count=0)
        call.assert_awaited_once()
        tool,args=call.await_args.args
        self.assertEqual((tool,args["start_date"],args["end_date"]),("weather_forecast","2026-11-27","2026-11-27"))
        self.assertNotIn("hourly",args)

    def test_02_weather_error_cards(self):
        for value,target,status,reason in [(None,"2026-11-27","empty","empty"),(TimeoutError(),"2026-11-27","timeout","timeout"),
                                           (None,"2026-12-30","provider_error","out_of_window")]:
            with self.subTest(reason=reason):
                call=AsyncMock(side_effect=value if isinstance(value,Exception) else None,return_value=None)
                skills=TransportSkills(self.catalog,self.clock,weather=self.weather(call),mode="fixture")
                pack=run(self.agent("weather",{"place_id":"qingyan","date":target},skills).ask("查青岩天气？",TRACE))
                result=pack["result"]
                self.assertEqual((pack["run_status"],result["kind"],result["status"],result["reason"]),
                                 ("partial","weather_unavailable",status,reason))
                self.assertEqual(result["target_date"],target)
                self.assertNotIn("forecast",result)
                self.assertEqual(call.await_count,0 if reason=="out_of_window" else 1)

    def test_03_weather_arrays_unknown_place_and_hourly(self):
        malformed=deepcopy(FX["weather"])
        malformed["daily"]["time"].append(DAY)
        call=AsyncMock(return_value=malformed)
        self.assertEqual(run(self.weather(call).query("qingyan","2026-11-27")).status,"invalid_response")
        call.reset_mock()
        skills=TransportSkills(self.catalog,self.clock,weather=self.weather(call),mode="fixture")
        p=run(self.agent("weather",{"place_id":"unknown","date":DAY},skills).ask("查一个不存在的地方天气？",TRACE))
        self.assertEqual((p["run_status"],p["result"]["field"]),("needs_input","place_id"))
        call.assert_not_called()
        valid=deepcopy(FX["weather"])
        valid.update(hourly={"time":["2026-11-27T08:00"],"temperature_2m":[9.0],"precipitation_probability":[43]},
                     hourly_units={"temperature_2m":"°C","precipitation_probability":"%"})
        call.return_value=valid
        weather=self.weather(call)
        result=run(weather.query("qingyan","2026-11-27",hourly=True))
        self.assertEqual(result.data["hourly"][0]["rain_probability_pct"],43)
        run(weather.query("qingyan","2026-11-27",hourly=True))
        self.assertEqual(call.await_count,1)
        self.assertIn("hourly",call.await_args.args[1])

    def test_04_exact_endpoints_and_coordinate_order(self):
        first=self.catalog.get("tongren")
        second=self.catalog.get("fanjing-east")
        self.assertEqual(route_waypoints(first,second,"osm"),["27.74332,109.1997","27.9,108.7"])
        amap = route_waypoints(first,second,"amap")
        lon,lat=map(float,amap[0].split(","))
        self.assertAlmostEqual(lon,109.1997,places=2)
        self.assertAlmostEqual(lat,27.74332,places=2)
        self.assertNotEqual(lon,109.1997)
        call=AsyncMock(return_value=FX["route"])
        skills=TransportSkills(self.catalog,self.clock,route=call,mode="fixture")
        base=dict(from_id="tongren",to_id="fanjing-east",date=DAY)
        for edit,field in [({"from_id":"铜仁"},"from_id"),({"to_id":"fanjingshan"},"to_id"),
                           ({"to_id":"fanjing-parking"},"to_id")]:
            pack=run(self.agent("transfer",base|edit,skills).ask("怎么接驳？",TRACE))
            self.assertEqual((pack["run_status"],pack["result"]["field"]),("needs_input",field))
        call.assert_not_called()
        p=run(self.agent("transfer",base,skills).ask("铜仁站怎么去梵净山东门？",TRACE))
        self.assertEqual((p["result"]["kind"],p["result"]["from_id"],p["result"]["to_id"]),
                         ("transfer","tongren","fanjing-east"))
        self.assertEqual(call.await_args.args[1]["waypoints"],route_waypoints(first,second,"osm"))

    def test_05_map_is_not_last_service(self):
        call=AsyncMock(return_value=FX["route"])
        skills=TransportSkills(self.catalog,self.clock,route=call,mode="fixture")
        pack=run(self.agent("transfer",dict(from_id="tongren",to_id="fanjing-east",date=DAY,
                                            mode="bus",arrival_at=DAY+"T16:00:00+08:00"),skills).ask("怎么坐公交？",TRACE))
        r=pack["result"]
        self.assertEqual((r["last_service"]["state"],r["feasibility"]),("unknown","needs_verification"))
        self.assertEqual((r["segments"][0]["mode"],r["segments"][0]["travel_min"]),("car",40))
        self.assertTrue(r["alternative"]["requires_choice"])
        reverse={"claim":{"claim_type":"last_service","subject_id":"fixture-line","target_date":DAY,
                          "line_direction":"back","status":"confirmed","confidence_basis":"official_original","value":"23:00",
                          "evidence_ids":["reverse"]},"evidence":[{"evidence_id":"reverse","data_mode":"live",
                          "source_kind":"operator","effective_from":DAY,"effective_to":DAY,"freshness":"current",
                          "retrieved_at":CLOCK.isoformat(),"expires_at":"2026-11-26T10:30:00+08:00","excerpt":"铜仁站 反向末班车23:00"}]}
        p=transfer_packet(self.catalog.get("tongren"),self.catalog.get("fanjing-east"),DAY,self.clock(),
                          mode="fixture",map_minutes=40,arrival_at=DAY+"T16:00:00+08:00",
                          last_service={"subject_id":"fixture-line","direction":"out","boarding_id":"tongren"},
                          evidence_packets=[reverse])
        self.assertEqual((p["result"]["last_service"]["state"],p["evidence"]),("unknown",[]))

    def test_06_midnight_appointment_conflict(self):
        p=transfer_packet(self.catalog.get("tongren"),self.catalog.get("fanjing-east"),DAY,self.clock(),
                          mode="fixture",arrival_at=DAY+"T23:40:00+08:00",
                          schedule=FX["schedule"],reservation=FX["booking"])
        r=p["result"]
        self.assertEqual(r["arrival_check"]["arrival_at"],"2026-11-29T01:20:00+08:00")
        self.assertEqual((r["transfer_buffer_min"],r["wait_min"],r["feasibility"]),(15,30,"conflict"))
        self.assertIn("RESERVATION_WINDOW_MISSED",r["rule_codes"])
        self.assertEqual(r["last_service"]["state"],"unknown")
        self.assertTrue(r["alternative"]["requires_choice"])
        TRACE.record("m07_midnight_packet",packet=p,write_count=0)

    def test_07_rail_calls_and_unknown_values(self):
        call=AsyncMock(side_effect=[FX["codes"],FX["trains"]])
        rail=RailAdapter(self.catalog,call,self.clock,mode="fixture")
        reply=run(rail.query("guiyang-north","zunyi",DAY,adults=2))
        self.assertEqual(reply.status,"success")
        self.assertEqual([c.args[0] for c in call.await_args_list],["get-station-code-by-names","get-tickets"])
        self.assertEqual(call.await_args.args[1]["fromStation"],"KQW")
        self.assertEqual(reply.data["options"][0]["arrive_at"],"2026-11-29T00:20:00+08:00")
        self.assertIsNone(reply.data["options"][0]["reference_fen"])
        self.assertIsNone(reply.data["options"][0]["seats"][0]["remaining"])
        self.assertEqual(reply.data["options"][1]["reference_fen"],9900)
        for raw, expected in [([{},[]],("provider_error","unresolved_station")),
                               ([FX["codes"],[]],("empty","empty")),
                               ([ConnectionError()],("provider_error","connection_error"))]:
            with self.subTest(expected=expected):
                ret=run(RailAdapter(self.catalog,AsyncMock(side_effect=raw),self.clock,mode="fixture")
                        .query("guiyang-north","zunyi",DAY,adults=2))
                self.assertEqual((ret.status,ret.reason),expected)

    def test_08_flight_parameter_maps_and_fallback_party(self):
        main=AsyncMock(return_value=flight_raw(status="error"))
        backup=AsyncMock(return_value=flight_raw(backup=True))
        svc=FlightAdapter(self.catalog,main,backup,self.clock,mode="fixture",allow_backup=True)
        r=run(svc.query("pvg","kwe",DAY,adults=2,currency="CNY"))
        self.assertEqual((r.status,r.data["provider_status"]["primary"],r.data["provider_status"]["backup"]),
                         ("success","provider_error","success"))
        self.assertEqual(main.await_args.args[1],dict(origin="PVG",destination="KWE",depart_date=DAY,adults=2,currency="CNY",max_results=3))
        self.assertEqual(backup.await_args.args[1],dict(origin="PVG",destination="KWE",departure_date=DAY,currency="CNY",top_n=3))
        self.assertIsNone(r.data["options"][0]["reference_fen"])
        self.assertEqual((r.data["options"][0]["party_status"],r.data["options"][0]["tax_status"]),("unknown","unknown"))
        backup.request_passengers=True
        raw=flight_raw(backup=True)
        raw["flights"][0]["legs"][0].update(departure_airport="Shanghai Pudong International Airport",
                                            arrival_airport="Longdongbao Airport")
        backup.return_value=raw
        r=run(svc.query("pvg","kwe",DAY,adults=2,currency="CNY",backup_only=True))
        self.assertEqual(backup.await_args.args[1]["passengers"],2)
        self.assertEqual(r.data["options"][0]["party_status"],"unknown")

    def test_09_google_source_and_incomplete_door_to_door(self):
        svc=FlightAdapter(self.catalog,AsyncMock(return_value=flight_raw()),AsyncMock(return_value=flight_raw(backup=True)),
                          self.clock,mode="fixture",allow_backup=True)
        main=run(svc.query("pvg","kwe",DAY,adults=2,currency="CNY"))
        alt=run(svc.query("pvg","kwe",DAY,adults=2,currency="CNY",backup_only=True))
        self.assertEqual({row["source_group_id"] for r in (main,alt) for row in r.data["evidence"]},{"google-flights"})
        rail=RailAdapter(self.catalog,AsyncMock(side_effect=[FX["codes"],FX["trains"]]),self.clock,mode="fixture")
        result=run(rail.query("guiyang-north","zunyi",DAY,adults=2))
        card=transport_card(result, "train", {"date":DAY,"from_id":"guiyang-north","to_id":"zunyi","adults":2}, self.clock(), "fixture")
        self.assertIsNone(card["result"]["options"][1]["total_fen"])
        self.assertIsNone(card["result"]["options"][1]["door_to_door_min"])
        self.assertIn("待核实",card["result"]["price_meaning"])
        self.assertEqual(card["result"]["booking_status"],"not_booked")
        full=transport_card(result,"train",{"date":DAY,"from_id":"guiyang-north","to_id":"zunyi","adults":2},
                            self.clock(),"fixture",
                            connection=dict(access_min=10,egress_min=20,boarding_buffer_min=15,access_fen=2000,egress_fen=3000))
        self.assertEqual((full["result"]["options"][1]["total_fen"],full["result"]["options"][1]["door_to_door_min"]),
                         (14900,95))
        TRACE.record("m07_door_to_door_packet",incomplete=card,complete=full,write_count=0)
        entities=FX["entities"]+[
            dict(id="shanghai-hongqiao",kind="station",label="上海虹桥站"),
            dict(id="shanghai",kind="city",label="上海",station_id="shanghai-hongqiao",airport_id="pvg"),
            dict(id="guiyang",kind="city",label="贵阳",station_id="guiyang-north",airport_id="kwe"),
        ]
        directory=Catalog(entities)
        rail_codes={"上海虹桥":{"station_code":"AOH"},"贵阳北":{"station_code":"KQW"}}
        trains=[dict(FX["trains"][1],from_station="上海虹桥",to_station="贵阳北")]
        for upstream, expected in [(flight_raw(),"completed"),([], "partial")]:
            rail=RailAdapter(directory,AsyncMock(side_effect=[rail_codes,trains]),self.clock,mode="fixture")
            flight=FlightAdapter(directory,AsyncMock(return_value=upstream),None,self.clock,mode="fixture")
            skills=TransportSkills(directory,self.clock,rail=rail,flight=flight,mode="fixture")
            pack=run(self.agent("candidates",{"candidate_kind":"intercity","from_id":"上海","to_id":"贵阳",
                                            "date":DAY,"adults":2},skills).ask("比较上海到贵阳的铁路和航班？",TRACE))
            self.assertEqual((pack["result"]["kind"],pack["run_status"]),("bundle",expected))
            self.assertEqual(pack["result"]["primary"]["basis"]["origin_label"],"上海虹桥站")
            self.assertEqual(pack["result"]["primary"]["options"][0]["known_lower_fen"],9900)
            if expected=="completed":
                self.assertEqual(pack["result"]["supplements"][0]["candidate_kind"],"flight")
            else:
                self.assertEqual(pack["result"]["supplements"][0]["kind"],"unavailable")

    def test_10_proxy_failures_and_basic_card_contract(self):
        for err, expected in [(401,"auth_failed"),(429,"rate_limited"),("bad_json","invalid_response")]:
            self.assertEqual(transport_status(err),expected)
            proxy=CallToolResult(is_error=type(err) is int,content=[TextContent(type="text",text=str(err))])
            async def fixture(_tool,_args):
                return unbox(proxy)
            call=AsyncMock(side_effect=fixture)
            w=run(self.weather(call).query("qingyan","2026-11-27"))
            self.assertEqual(w.status,expected)
            self.assertEqual(call.await_count,1)


if __name__ == "__main__":
    unittest.main()

