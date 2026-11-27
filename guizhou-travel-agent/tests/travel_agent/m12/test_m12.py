"""M12 release tests. No external provider is called from this suite."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from travel_release import Measure, RunBudget, RunLimit, RunTrace, check_policy

ROOT=Path(__file__).resolve().parents[3]


class ReleaseChecks(TestCase):
    def test_finite_budget_reserves_before_call_and_preserves_actual_unknown(self):
        ticks=[0]
        run=RunBudget(attempts=2,seconds=3,cost_micros=80,clock=lambda:ticks[0])
        with self.assertRaises(RunLimit):
            run.reserve("model",81)
        self.assertEqual(run.attempts,0)
        permitted=run.reserve("model",50)
        run.finish(permitted,20)
        self.assertEqual((run.attempts,run.known_cost_micros),(1,20))
        later=run.reserve("model",50)
        run.finish(later,None)
        self.assertEqual(run.unknown_cost_count,1)
        self.assertEqual(run.reserved_cost_micros,50)
        with self.assertRaises(RunLimit):
            run.reserve("model",1)

    def test_deadline_and_no_unbounded_paid_call(self):
        ticks=[0]
        run=RunBudget(attempts=3,seconds=1,cost_micros=100,clock=lambda:ticks[0])
        with self.assertRaises(RunLimit):
            run.reserve("model",None)
        ticks[0]=1
        with self.assertRaises(RunLimit):
            run.reserve("weather",0)
        self.assertEqual(run.attempts,0)

    def test_closed_policy_and_explicit_allowlist(self):
        now=datetime(2026,11,27,tzinfo=timezone.utc)
        sample=dict(authorized=True,model="qwen4-max",max_attempts=720,max_seconds=5400,
                    cap_micros=1000000,input_micros_per_million=3000000,
                    output_micros_per_million=10000000,max_output_tokens=512,
                    expires_at=(now+timedelta(hours=1)).isoformat())
        self.assertEqual(check_policy(sample,now,"qwen4-max")["max_attempts"],720)
        for bad in (dict(sample,authorized=False),dict(sample,model="unapproved"),
                    dict(sample,max_attempts=0),dict(sample,expires_at=now.isoformat())):
            with self.assertRaises(RunLimit):
                check_policy(bad,now,"qwen4-max")

    def test_trace_keeps_correlation_without_raw_credentials_or_body(self):
        import json
        trace=RunTrace()
        trace.record("decision",intent="weather",arguments={"place_id":"qingyan","authorization":"private-1"},
                     raw="private-2",message="private-3",evidence_ids=["approved-source"])
        trace.record("m10_finished",run_id="job-1",status="partial",trip_writes=0)
        wire=json.dumps(trace.events)
        self.assertNotIn("private-",wire)
        self.assertNotIn("authorization",wire)
        self.assertIn("qingyan",wire)
        self.assertIn(trace.trace_id,wire)
        self.assertIn("job-1",wire)
        self.assertIn("elapsed_ms",wire)

    def test_percentile_per_family_and_nonpriced_rows_are_not_zero(self):
        result=Measure.summary([dict(family="天气",elapsed_ms=10,attempts=1,cost_micros=None),
                                dict(family="天气",elapsed_ms=20,attempts=1,cost_micros=13),
                                dict(family="确认",elapsed_ms=0,attempts=0,cost_micros=0)])
        self.assertEqual(result["天气"]["p95_ms"],20)
        self.assertEqual(result["天气"]["cost_micros"],None)
        self.assertEqual(result["天气"]["known_cost_micros"],13)
        self.assertEqual(result["天气"]["unknown_cost_count"],1)
        self.assertEqual(result["确认"]["cost_micros"],0)

    def test_fixed_datasets_keep_denominator_and_negative_slice(self):
        import json
        data=json.loads((ROOT/"tests/travel_agent/fixtures/model_eval/cases.json").read_text(encoding="utf-8"))
        cases=data["cases"]
        self.assertEqual(len(cases),120)
        self.assertEqual(len({row["message"] for row in cases}),120)
        self.assertEqual(sum(row["negative"] for row in cases),30)
        self.assertEqual(data["repeats"],3)
        self.assertTrue(all("expected_tools" in row and "expected_kind" in row for row in cases))
        source=json.loads((ROOT/"tests/travel_agent/fixtures/live_smoke/source_gold.json").read_text(encoding="utf-8"))
        self.assertEqual(source["required_gold_count"],20)
        self.assertTrue(all(row["eligibility"] in {"candidate","preverified"} for row in source["cases"]))

    def test_candidate_discovery_does_not_enter_gold_denominator(self):
        from scripts.travel_agent.evaluate_release_sources import select_cases

        cases = [
            {"case_id": "S01", "eligibility": "candidate", "original_url": None, "expected_value": None},
            {"case_id": "S02", "eligibility": "preverified", "original_url": "https://example.org/", "expected_value": "open"},
            {"case_id": "S03", "eligibility": "candidate", "original_url": None, "expected_value": None},
        ]
        self.assertEqual(select_cases(cases, "gold", True), ([cases[1]], [cases[1]]))
        self.assertEqual(select_cases(cases, "calibration", True), ([cases[1]], [cases[0]]))
        self.assertEqual(select_cases(cases, "candidates", True), ([cases[1]], [cases[0], cases[2]]))
        self.assertEqual(select_cases(cases, "candidates", False), ([cases[1]], []))

    def test_transfer_names_resolve_to_distinct_catalog_endpoints(self):
        import asyncio
        import json
        from unittest.mock import AsyncMock
        from scripts.travel_agent.evaluate_release_model import SAMPLES
        from travel_agent import Conversation

        fixed = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
        handler = AsyncMock(return_value=SAMPLES["transfer"])
        model = AsyncMock(return_value=json.dumps(dict(intent="transfer", confidence_band="high",
                         requested_outputs=["transfer"], parameters={"from_id":"荔波站","to_id":"小七孔东门"})))
        agent = Conversation(typed_output=True, intent_model=model, handlers={"fixture_transfer":handler},
                             fixture_mode=True, clock=lambda:fixed)
        result = asyncio.run(agent.ask("周六荔波站怎么去小七孔东门？"))["result"]
        self.assertEqual(result["kind"], "transfer")
        self.assertEqual(result["last_service"]["state"], "unknown")
        handler.assert_awaited_once_with({"from_id":"libo-station","to_id":"xiaoqikong-east-gate","date":"2026-11-28"})

    def test_adult_and_child_counts_are_kept_for_an_unknown_cost(self):
        import asyncio
        import json
        from unittest.mock import AsyncMock
        from scripts.travel_agent.evaluate_release_model import SAMPLES
        from travel_agent import Conversation

        fixed = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
        handler = AsyncMock(return_value=SAMPLES["cost_breakdown"])
        model = AsyncMock(return_value=json.dumps(dict(intent="cost_breakdown", confidence_band="high",
                         requested_outputs=["cost_breakdown"], parameters={"scenic_area":"小七孔"})))
        agent = Conversation(typed_output=True, intent_model=model, handlers={"fixture_cost":handler},
                             fixture_mode=True, clock=lambda:fixed)
        result = asyncio.run(agent.ask("周六去小七孔，两大一小门票多少？"))["result"]
        self.assertEqual(result["kind"], "cost_breakdown")
        self.assertIsNone(result["total_fen"])
        handler.assert_awaited_once_with({"place_id":"xiaoqikong","date":"2026-11-28","adults":2,"children":1})

    def test_radeon_transport_schema_preserves_api_fields_and_local_validation(self):
        from scripts.travel_agent.evaluate_release_model import radeon_schema
        from travel_contract import INTENT, ContractError, decode_intent

        schema = radeon_schema(INTENT.schema)
        props = schema["properties"]
        self.assertEqual(set(props), set(INTENT.schema["properties"]))
        self.assertIn("format", props["parameters"]["properties"])
        self.assertNotIn("uniqueItems", props["requested_outputs"])
        self.assertTrue(INTENT.schema["properties"]["requested_outputs"]["uniqueItems"])
        with self.assertRaises(ContractError):
            decode_intent('{"intent":"weather","requested_outputs":["weather","weather"],'
                          '"confidence_band":"high","parameters":{}}')

    def test_hotel_uses_a_stated_checkin_date(self):
        import asyncio
        import json
        from unittest.mock import AsyncMock
        from scripts.travel_agent.evaluate_release_model import SAMPLES
        from travel_agent import Conversation

        fixed = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
        handler = AsyncMock(return_value=SAMPLES["candidates"])
        model = AsyncMock(return_value=json.dumps(dict(intent="candidates",confidence_band="high",
                         requested_outputs=["candidates"],parameters={"candidate_kind":"hotel",
                         "date":"周六"})))
        agent = Conversation(typed_output=True,intent_model=model,handlers={"fixture_candidates":handler},
                             fixture_mode=True,clock=lambda:fixed)
        result = asyncio.run(agent.ask("两位成人住一晚一间房，周六入住，帮我找酒店。"))["result"]
        self.assertEqual(result["kind"], "candidates")
        handler.assert_awaited_once_with({"candidate_kind":"hotel","check_in":"2026-11-28",
                                         "adults":2,"rooms":1,"nights":1})

    def test_source_review_never_promotes_candidate_rows_to_formal_gold(self):
        import json
        from scripts.travel_agent.review_m12_source_originals import review_rows

        dataset = json.loads((ROOT/"tests/travel_agent/fixtures/live_smoke/source_gold.json").read_text(encoding="utf-8"))
        rows = review_rows(dataset["cases"], {"heritage_schedule":True})
        self.assertEqual([row["case_id"] for row in rows], [case["case_id"] for case in dataset["cases"]])
        self.assertEqual(sum(row["review_state"]=="ready_for_human_check" for row in rows),4)
        self.assertFalse(any(row["formal_gold"] for row in rows))
        self.assertTrue(all(row["target_date"]==case["target_date"] for row,case in zip(rows,dataset["cases"])))

    def test_route_prompt_distinguishes_statement_from_a_request(self):
        import asyncio
        import json
        from unittest.mock import AsyncMock
        from travel_agent import classify_intent

        fixed = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
        completion = AsyncMock(return_value={"content":"{}"})
        asyncio.run(classify_intent("我打算去天河潭。",fixed,{},False,completion=completion))
        prompt = completion.await_args.args[0]
        self.assertEqual(json.loads(prompt[1]["content"])["utterance_form"],"travel_statement")
        asyncio.run(classify_intent("我打算去天河潭，要预约吗？",fixed,{},False,completion=completion))
        prompt = completion.await_args.args[0]
        self.assertNotEqual(json.loads(prompt[1]["content"])["utterance_form"],"travel_statement")

    def test_radeon_transient_delay_respects_provider_header(self):
        from scripts.travel_agent.evaluate_release_model import provider_retry_delay

        self.assertEqual(provider_retry_delay(429, "60"),60)
        self.assertEqual(provider_retry_delay(429, "1"),1)
        self.assertIsNone(provider_retry_delay(429, "3600"))
        self.assertIsNone(provider_retry_delay(401, "1"))
        self.assertEqual(provider_retry_delay(503, None),3)
