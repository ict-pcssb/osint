import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from support.guards import Trace, isolated_database
from travel_agent import Conversation
from travel_evidence import digest
from travel_candidate_context import ChoiceError
from travel_candidates import CandidateSkills, FoodAdapter, FoodOriginal, HotelAdapter, TaxiAdapter
from travel_candidates_live import classify_response
from travel_state_app import TravelApplication
from travel_transport import ProviderFailure

FX=json.loads((Path(__file__).parent.parent/"fixtures/m08/candidates.json").read_text(encoding="utf-8"))
NOW=datetime.fromisoformat(FX["clock"])
TRACE=Trace("m08-fixture")
TAXI=dict(candidate_kind="taxi",from_id="guiyang-north",to_id="qingyan-north-parking",vehicle_class="经济")
STAY=dict(candidate_kind="hotel",place_id="guiyang",check_in="2026-11-28",nights=2,adults=2,rooms=1,children=0)
FOOD=dict(candidate_kind="dining",place_id="qingyan",meal_at="2026-11-28T19:00:00+08:00")


def run(coro):
    return asyncio.run(coro)


def make_factory(fields):
    async def intent(*_):
        return json.dumps(dict(intent="candidates",confidence_band="high",requested_outputs=["candidates"],parameters=fields))
    return lambda:Conversation(typed_output=True,clock=lambda:NOW,intent_model=intent,fixture_mode=True)


def redacted(packet):
    clean=deepcopy(packet)
    result=clean["result"]
    result["tool_context_hash"]=digest(result.pop("tool_context_id"))
    for choice in result["options"]:
        choice["id"]=digest(choice["id"])
    return clean


def count(app):
    with app.repository.connection() as db:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                     for name in ("trips","trip_versions","save_keys"))


class Module08(unittest.TestCase):
    def setUp(self):
        self.now=NOW
        self.clock=lambda:self.now
        self.tmp=self.enterContext(TemporaryDirectory(prefix="travel-m08-"))
        self.root=Path(self.tmp)
        self.db=self.enterContext(isolated_database(self.root/"travel_m08.sqlite3",self.root))

    def application(self,**providers):
        app=TravelApplication(self.db,clock=self.clock,candidate_skills=CandidateSkills(self.clock,mode="fixture",**providers))
        for name in ("m08a","m08b"):
            app.create_account(name,"fixture-password-only")
        a,b=(app.login(name,"fixture-password-only") for name in ("m08a","m08b"))
        return app,a,b,app.open_session(a)

    def ask(self,app,token,session,fields,trip_id=None):
        return run(app.turn(token,"请查询可选方案？",session_id=session,trip_id=trip_id,
                            conversation_factory=make_factory(fields),trace=TRACE))

    def test_01_real_entry_then_explicit_choice(self):
        call=AsyncMock(side_effect=[FX["point"],FX["quote"]])
        app,a,_,s=self.application(taxi=TaxiAdapter(call,self.clock,mode="fixture"))
        p=self.ask(app,a,s,TAXI)
        r=p["result"]
        self.assertEqual(r["kind"],"pickup_choices")
        self.assertEqual(len(r["options"]),2)
        self.assertEqual(call.await_count,1)
        self.assertNotIn("businessData",json.dumps(p))
        q=run(app.choose_candidate(a,s,r["tool_context_id"],r["options"][1]["id"],trace=TRACE))
        self.assertEqual(call.await_count,2)
        args=call.await_args.args[1]
        self.assertEqual(args["businessData"].encode(),FX["point"]["entities"][0]["businessData"].encode())
        self.assertEqual(args["origin"],r["options"][1]["label"])
        self.assertEqual((q["result"]["options"][0]["min_fen"],q["result"]["options"][0]["max_fen"]),(8000,11000))
        self.assertEqual(q["result"]["options"][0]["unit"],"vehicle")
        self.assertEqual(q["result"]["basis"]["from_label"],args["origin"])
        self.assertEqual(count(app),(0,0,0))
        TRACE.record("m08_taxi_cards",first=redacted(p),second=q,write_count=0)

    def test_02_context_rejects_owner_session_choice_and_expiry(self):
        call=AsyncMock(return_value=FX["point"])
        app,a,b,s=self.application(taxi=TaxiAdapter(call,self.clock,mode="fixture"))
        r=self.ask(app,a,s,TAXI)["result"]
        ctx,choice=r["tool_context_id"],r["options"][0]["id"]
        for who,sid,ch,reason in [(b,app.open_session(b),choice,"forbidden"),(a,app.open_session(a),choice,"forbidden"),
                                  (a,s,"forged","invalid_choice")]:
            with self.assertRaises(ChoiceError) as err:
                run(app.choose_candidate(who,sid,ctx,ch,trace=TRACE))
            self.assertEqual(err.exception.reason,reason)
            self.assertNotIn("businessData",str(err.exception))
        self.now+=timedelta(minutes=11)
        with self.assertRaises(ChoiceError) as err:
            run(app.choose_candidate(a,s,ctx,choice,trace=TRACE))
        self.assertEqual(err.exception.reason,"expired")
        self.assertEqual(call.await_count,1)
        self.assertEqual(count(app),(0,0,0))

    def test_03_vehicle_groups_and_unknown_scenes(self):
        call=AsyncMock(return_value=FX["quote"])
        app,a,_,s=self.application(taxi=TaxiAdapter(call,self.clock,mode="fixture"))
        r=self.ask(app,a,s,TAXI)["result"]
        self.assertEqual([(x["vehicle_class"],x["min_fen"],x["max_fen"]) for x in r["options"]],[("经济",8000,11000)])
        for scene in ("amap_car_abnormal","new_unknown","amap_car_order"):
            call.return_value={"entities":[{"cardScene":scene,"modelInput":{}}]}
            p=self.ask(app,a,s,TAXI)
            self.assertEqual((p["run_status"],p["result"]["kind"]),("partial","unavailable"))

    def test_04_missing_hotel_total_and_no_price(self):
        call=AsyncMock(side_effect=[FX["hotels"],FX["rate"],{"success":True,"roomRatePlans":[]}])
        app,a,_,s=self.application(hotel=HotelAdapter(call,self.clock,mode="fixture"))
        r=self.ask(app,a,s,STAY)["result"]
        cards={i["id"]:i for i in r["options"]}
        self.assertEqual(cards["hotel-101"]["nightly_reference_fen"],26000)
        self.assertIsNone(cards["hotel-101"]["stay_total_fen"])
        self.assertIn("入住总价",cards["hotel-101"]["missing_costs"])
        self.assertIsNone(cards["hotel-102"]["nightly_reference_fen"])
        self.assertEqual(r["booking_status"],"not_booked")

    def test_05_search_to_detail_scope_currency_inventory(self):
        usd=deepcopy(FX["rate"])
        usd["roomRatePlans"][0].update(currency="USD",averagePrice=65,totalPrice=130)
        call=AsyncMock(side_effect=[FX["hotels"],FX["rate"],usd])
        app,a,_,s=self.application(hotel=HotelAdapter(call,self.clock,mode="fixture"))
        p=self.ask(app,a,s,STAY)
        self.assertEqual([i.args[0] for i in call.await_args_list],["searchHotels","getHotelDetail","getHotelDetail"])
        request=call.await_args_list[1].args[1]
        self.assertEqual((request["hotelId"],request["dateParam"],request["occupancyParam"]),
                         (101,{"checkInDate":"2026-11-28","checkOutDate":"2026-11-30"},{"adultCount":2,"roomCount":1}))
        r=p["result"]
        self.assertEqual({i["currency"] for i in r["options"]},{"CNY","USD"})
        self.assertEqual(r["inventory_status"],"unverified")
        self.assertEqual(r["options"][0]["inventory_detail"],"on_request")
        self.assertEqual(r["options"][0]["cancellation"]["status"],"unconfirmed")
        self.assertIn("币种",r["price_meaning"])
        TRACE.record("m08_hotel_currency",packet=p)
        call=AsyncMock(side_effect=[FX["hotels"],FX["rate"],{"success":True,"roomRatePlans":[]}])
        adapter=HotelAdapter(call,self.clock,mode="fixture")
        with_child=run(adapter.query(STAY|{"children":1,"child_ages":[8]}))
        self.assertNotIn("childCount",call.await_args_list[0].args[1]["checkInParam"])
        self.assertEqual(call.await_args_list[1].args[1]["occupancyParam"],
                         {"adultCount":2,"roomCount":1,"childCount":1,"childAgeDetails":[8]})
        self.assertEqual(with_child["result"]["basis"]["children"],1)
        self.assertIsNone(with_child["result"]["options"][1]["nightly_reference_fen"])

    def test_06_old_food_snapshot_and_promotional_reprints(self):
        poi=AsyncMock(return_value=FX["foods"])
        async def originals(*_):
            return [FoodOriginal(f"https://site{i}.example/post","甲餐馆 广告合作 赞助推广。",
                                 self.now,canonical_url="https://source.example/ad") for i in range(3)]
        app,a,_,s=self.application(food=FoodAdapter(poi,originals,self.clock,mode="fixture"))
        p=self.ask(app,a,s,FOOD)
        r=p["result"]; first=r["options"][0]
        self.assertEqual(r["primary_id"],first["id"])
        self.assertEqual(first["label"],"甲餐馆")
        self.assertEqual((len(first["independent_reviews"]),len(first["sponsored_claims"])),(0,1))
        self.assertEqual(first["opening_status"],"unconfirmed")
        self.assertIn("当晚营业",first["uncertainties"])
        self.assertEqual(len(r["alternative_ids"]),1)
        TRACE.record("m08_dining_packet",packet=p)
        snapshot=deepcopy(FX["foods"])
        snapshot["results"][0]["collected_at"]="2026-11-20T18:00:00+08:00"
        async def visited(*_):
            return [FoodOriginal("https://source.example/review","我去甲餐馆到访。",self.now,
                                 author="公开署名",visit_date=self.now.date()-timedelta(days=4))]
        newer=run(FoodAdapter(AsyncMock(return_value=snapshot),visited,self.clock,mode="fixture").query(FOOD))
        row=newer["result"]["options"][0]
        self.assertEqual(len(row["independent_reviews"]),1)
        self.assertEqual(row["snapshot_at"],"2026-11-20T18:00:00+08:00")
        self.assertEqual(row["opening_status"],"unconfirmed")

    def test_07_locked_trip_and_route_price_tradeoff(self):
        row=deepcopy(FX["hotels"])
        def plan(total):
            return {"success":True,"roomRatePlans":[{"roomName":"标准间","currency":"CNY","totalPrice":total}]}
        call=AsyncMock(side_effect=[row,plan(400),plan(550)])
        async def route(hotel_id, target, hotel):
            return {"to_id":target,"travel_min":80 if hotel_id==101 else 10,"status":"estimate"}
        app,a,_,s=self.application(hotel=HotelAdapter(call,self.clock,mode="fixture",route=route))
        draft=app.preview_trip(a,{"destination":"贵阳","start_date":"2026-11-28","adults":2,"days":2,
                                  "budget_fen":80000,"budget_scope":"group"},arrangements=[
            {"kind":"hotel","fields":{"check_in":"2026-11-28","place":"原酒店"}},
            {"kind":"rail","fields":{"departure_at":"2026-11-29T18:00:00+08:00","purpose":"return"}}],confirm_arrangements=True)
        version=app.create_trip(a,draft.draft_id,save=True,idempotency_key="m08-locked")
        r=self.ask(app,a,s,STAY|{"next_stop_id":"jiaxiu-entry"},version.trip_id)["result"]
        self.assertEqual(r["primary_id"],"hotel-102")
        self.assertTrue(any("70" in item["reason"] for item in r["options"]))
        again=app.read_trip(a,version.trip_id)
        self.assertEqual((again.revision,again.locked_items),(version.revision,version.locked_items))
        self.assertEqual(count(app),(1,1,1))

    def test_08_fault_categories_and_partial_hotel(self):
        for err,status in [(401,"auth_failed"),(429,"rate_limited"),("quota","quota_exhausted"),
                           ("bad_json","invalid_response"),("empty","empty")]:
            self.assertEqual(classify_response(err),status)
            call=AsyncMock(side_effect=ProviderFailure(status))
            r=run(HotelAdapter(call,self.clock,mode="fixture").query(STAY,trace=TRACE))
            self.assertEqual(r["result"]["reason"],status)
            self.assertEqual(call.await_count,1)
        call=AsyncMock(side_effect=[FX["hotels"],FX["rate"],ProviderFailure("invalid_response")])
        p=run(HotelAdapter(call,self.clock,mode="fixture").query(STAY,trace=TRACE))
        self.assertEqual((p["run_status"],len(p["result"]["options"])),("partial",2))

    def test_09_duplicate_and_stale_original_collection(self):
        call=AsyncMock(side_effect=[FX["point"],FX["quote"]])
        app,a,_,s=self.application(taxi=TaxiAdapter(call,self.clock,mode="fixture"))
        r=self.ask(app,a,s,TAXI)["result"]
        ctx,ch=r["tool_context_id"],r["options"][1]["id"]
        async def twice():
            return await asyncio.gather(*(app.choose_candidate(a,s,ctx,ch,trace=TRACE) for _ in range(2)))
        first,second=run(twice())
        self.assertEqual(first,second)
        self.now+=timedelta(minutes=6)
        stale=run(app.choose_candidate(a,s,ctx,ch,trace=TRACE))
        self.assertEqual(stale["result"]["quote_status"],"stale")
        self.assertEqual(stale["evidence"][0]["collected_at"],first["evidence"][0]["collected_at"])
        self.assertEqual(call.await_count,2)
        self.assertEqual(count(app),(0,0,0))

    def test_10_provider_shape_and_public_original_gate(self):
        from travel_candidates import taxi_entities
        r=[{"type":"text","text":"some descriptive text"}, {"type":"text","text":json.dumps(FX["point"])}]
        self.assertEqual(taxi_entities(r)[0]["cardScene"],"amap_car_point")
        call=AsyncMock(return_value=FX["foods"])
        async def reader(*_):
            return [FoodOriginal("https://city.example/article","只提及另一家餐馆，未提及目标。",
                                 self.now)]
        p=run(FoodAdapter(call,reader,self.clock,mode="fixture").query(FOOD))
        self.assertFalse(p["result"]["options"][0]["independent_reviews"])
        self.assertTrue(p["result"]["options"][0]["uncertainties"])


if __name__=="__main__":
    unittest.main()

