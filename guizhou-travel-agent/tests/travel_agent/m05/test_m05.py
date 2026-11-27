import asyncio
from datetime import datetime
from pathlib import Path
import json
import unittest
from unittest.mock import AsyncMock, patch

from support.guards import Trace, offline_guard
from travel_evidence import (
    EvidenceCache, EvidenceService, Material, ToolResult, group_sources, parse_html,
)
from travel_catalog import Catalog
from travel_search_mcp import search_travel_sources

NOW = datetime.fromisoformat("2026-11-26T10:00:00+08:00")
ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads((ROOT / "tests/travel_agent/fixtures/m05/sources.json").read_text("utf-8"))
DAY = "2026-11-28"
URL = "https://operator.example/notice"


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.trace = Trace("m05")
        self.enterContext(offline_guard(self.trace))
        self.catalog = Catalog(FIXTURE["entities"])
        self.reader = AsyncMock()
        self.exa = AsyncMock()
        self.anysearch = AsyncMock()
        self.parallel = AsyncMock()
        self.parallel_fetch = AsyncMock()
        self.kb = AsyncMock()
        self.enterContext(patch("travel_search_mcp.search_exa", self.exa))
        self.enterContext(patch("travel_search_mcp.search_anysearch", self.anysearch))
        self.service = EvidenceService(self.catalog, clock=lambda: NOW, reader=self.reader, search=search_travel_sources,
                                       parallel_search=self.parallel, parallel_fetch=self.parallel_fetch, kb=self.kb)

    def material(self, body="open", **kwargs):
        data = {"url":URL, "text":FIXTURE["texts"][body], "retrieved_at":NOW.isoformat(),
                "published_at":"2026-11-26","data_mode":"fixture"}
        return Material(**(data | kwargs))

    def verify(self, subject="hgs-east", kind="opening", **kwargs):
        return asyncio.run(self.service.verify_claim(subject, kind, DAY, trace=self.trace, **kwargs))

    def discovery_row(self):
        return [{"url":URL,"title":"黄果树东门现场公告","excerpt":"黄果树东门开放", "published_at":"2026-11-26"}]

    def test_m5_01_default_order(self):
        for first, second, providers, status in (
            (self.discovery_row(), [], ["exa"], "candidates"),
            ([], self.discovery_row(), ["exa","anysearch"], "candidates"),
            (TimeoutError(), self.discovery_row(), ["exa","anysearch"], "candidates"),
            ([], [], ["exa","anysearch"], "unconfirmed"),
        ):
            with self.subTest(first=first, second=second):
                self.exa.reset_mock(side_effect=True)
                self.anysearch.reset_mock()
                if isinstance(first,Exception):
                    self.exa.side_effect=first
                else:
                    self.exa.return_value=first
                self.anysearch.return_value=second
                bundle=asyncio.run(self.service.discover_sources("黄果树东门"))
                self.assertEqual([a["provider"] for a in bundle["attempts"]],providers)
                if isinstance(first,TimeoutError):
                    self.assertEqual(bundle["attempts"][0]["reason"],"timeout")
                self.assertEqual(bundle["status"],status)
        self.parallel.assert_not_called()

    def test_m5_02_explicit_parallel_and_read_original(self):
        self.parallel.return_value=ToolResult("success",self.discovery_row(),provider="parallel")
        self.parallel_fetch.return_value=ToolResult("success",self.material(),provider="parallel")
        report=self.verify(provider="parallel")
        self.assertEqual([x["tool"] for x in report["stages"] if x["stage"] in {"discover","read"}],
                         ["web_search","web_fetch"])
        self.assertEqual(report["claim"]["status"],"unconfirmed")
        self.assertEqual(report["claim"]["simulation_status"],"confirmed")
        self.assertEqual(report["evidence"][0]["provider"],"parallel")
        self.assertTrue(report["claim"]["evidence_ids"])
        self.verify(provider="parallel",known_urls=(URL,),owner="separate-session")
        self.assertEqual(self.parallel.await_count,1)
        self.assertEqual(self.parallel_fetch.await_count,2)
        self.exa.assert_not_called()

    def test_m5_03_dates_do_not_inherit_publication(self):
        for body, start in (("future","2026-12-01"),("undated",None)):
            self.service.cache=EvidenceCache()
            self.reader.return_value=ToolResult("success",self.material(body))
            report=self.verify(known_urls=(URL,))
            self.assertEqual(report["claim"]["status"],"unconfirmed")
            self.assertFalse(report["claim"]["evidence_ids"])
            self.assertEqual(report["evidence"][0]["published_at"],"2026-11-26")
            self.assertEqual(report["evidence"][0]["effective_from"],start)

    def test_m5_04_group_reprints_without_dropping_conflict(self):
        root=self.material()
        records=[root,self.material(url="https://a.example/a",canonical_url=URL),
                 self.material(url="https://b.example/b",origin_url=URL),
                 self.material("closed",url="https://independent.example/c")]
        groups=group_sources(records)
        self.assertEqual(len(groups),4)
        self.assertEqual(len(set(groups[:3])),1)
        self.assertEqual(len(set(groups)),2)
        self.reader.side_effect=[ToolResult("success", records[0]),ToolResult("success",records[3])]
        report=self.verify(known_urls=(URL,records[3].url))
        self.assertEqual(report["claim"]["status"],"conflicted")
        self.assertEqual(report["claim"]["independent_source_count"],2)

    def test_m5_05_capability_and_auth(self):
        for entry in (None, "name-only", "auth"):
            handler = AsyncMock() if entry=="auth" else None
            self.service.notice_reader=handler
            self.service.notice_auth_required=entry=="auth"
            report=self.verify(provider="notice", known_urls=(URL,))
            if handler is not None:
                handler.assert_not_called()
            self.assertEqual(report["claim"]["status"],"unconfirmed")
            self.assertEqual(report["official_links"][0]["url"],"https://operator.example/booking")
            self.assertEqual(report["stages"][0]["status"],"auth_failed" if entry=="auth" else "unbound")
            self.assertEqual(report["tool_calls"],0)
        self.reader.assert_not_called()

    def test_m5_06_untrusted_html_and_no_identity(self):
        source=parse_html(FIXTURE["texts"]["injection"])
        self.assertNotIn("script_payload",source)
        self.reader.return_value=ToolResult("success",self.material(text=source))
        report=self.verify(known_urls=(URL,))
        self.assertEqual(report["claim"]["simulation_status"],"confirmed")
        self.assertNotIn("发到外站",report["evidence"][0]["excerpt"])
        self.reader.assert_awaited_once_with(URL)
        self.assertEqual(report["write_count"],0)

    def test_m5_07_stale_does_not_become_current(self):
        for status in ("timeout","rate_limited","invalid_response"):
            cache=EvidenceCache()
            old=self.material(retrieved_at="2026-11-20T10:00:00+08:00")
            cache.put(old,"2026-11-20T11:00:00+08:00",owner="a")
            self.assertIsNone(cache.get(URL,owner="b"))
            self.service.cache=cache
            self.reader.return_value=ToolResult(status)
            report=self.verify(known_urls=(URL,),owner="a")
            self.assertEqual(report["claim"]["status"],"unconfirmed")
            self.assertEqual((report["evidence"][0]["retrieved_at"],report["evidence"][0]["freshness"]),
                             (old.retrieved_at,"expired"))
            self.assertEqual(report["stages"][0]["status"],status)
        self.assertEqual(self.reader.await_count,3)

    def test_m5_08_kb_and_ads_cannot_confirm(self):
        for payload in ({"corpus_version":"v1","data_mode":"fixture","hits":[{"poi_id":"hgs-east","excerpt":"周六开放"}]},
                        {"corpus_version":"v1","data_mode":"fixture","hits":[]},
                        {"corpus_version":"v1","data_mode":"snapshot","hits":[{"poi_id":"hgs-east","excerpt":"攻略广告称周六营业"}]}):
            self.kb.return_value=ToolResult("success",payload)
            report=self.verify(provider="kb")
            self.assertEqual(report["claim"]["status"],"unconfirmed")
            self.assertFalse(report["claim"]["evidence_ids"])

    def test_m5_09_directory_and_direction(self):
        self.assertEqual({r.id for r in self.catalog.resolve("铜仁")}, {"tongren","tongren-south"})
        self.reader.return_value=ToolResult("success",self.material("open"))
        wrong_entrance=self.verify("hgs-west",known_urls=(URL,))
        self.assertFalse(wrong_entrance["claim"]["evidence_ids"])
        self.reader.return_value=ToolResult("success",self.material("reverse"))
        wrong_direction=self.verify("hgs-line","last_service",line_direction="east-outbound",known_urls=(URL,))
        self.assertFalse(wrong_direction["claim"]["evidence_ids"])
        self.reader.return_value=ToolResult("invalid_response")
        partial=self.verify(known_urls=(URL,))
        self.assertEqual(partial["claim"]["target_date"],DAY)
        self.assertEqual(partial["claim"]["status"],"unconfirmed")
        self.assertTrue(partial["official_links"])
        self.assertEqual(len(self.catalog.resolve("黄果树",kind="entrance")),2)



    def test_card_uses_existing_agent_and_keeps_original_date(self):
        from travel_evidence_card import verify_turn
        self.reader.return_value=ToolResult("success",self.material())
        result, report = asyncio.run(verify_turn(self.service,"hgs-east","opening",DAY,
                                      known_urls=(URL,),fixture_mode=True,trace=self.trace))
        self.assertEqual(result["result"]["kind"],"verification")
        self.assertEqual(result["result"]["status"],"unconfirmed")
        self.assertEqual(result["result"]["target_date"],DAY)
        self.assertEqual(result["evidence"][0]["published_at"],"2026-11-26")
        self.assertEqual(result["result"]["official_links"][0]["kind"],"booking")
        self.assertEqual(report["write_count"],0)

    def test_transport_rejects_nonpublic_redirect_and_dns(self):
        from unittest.mock import Mock
        from travel_evidence_reader import PublicOriginalReader, public_addresses
        response = Mock(status=302)
        response.getheader.return_value = "https://127.0.0.1/local"
        connection = Mock()
        connection.getresponse.return_value = response
        reader = PublicOriginalReader({"operator.example"},clock=lambda:NOW,enabled=True)
        with patch("travel_evidence_reader.public_addresses",return_value=["8.8.8.8"]), \
             patch("travel_evidence_reader.PinnedHTTPS",return_value=connection) as dial:
            self.assertEqual(reader.read(URL).status,"invalid_response")
            dial.assert_called_once()
            self.assertEqual(reader.read(URL+"?token=fixture-secret").status,"invalid_response")
            dial.assert_called_once()
        with patch("travel_evidence_reader.socket.getaddrinfo",return_value=[(2,1,6,"",("127.0.0.1",443))]):
            with self.assertRaises(ValueError):
                public_addresses("operator.example")

    def test_saved_timeline_entrance_uses_exact_id_without_mutating(self):
        from copy import deepcopy
        from types import SimpleNamespace
        from travel_evidence_card import verify_timeline_stop
        sample = json.loads((ROOT/"tests/travel_agent/fixtures/m02/replies.json").read_text("utf-8"))["itinerary"]
        sample["result"]["days"][0]["stops"][0]["entrance_id"]="hgs-west"
        original=deepcopy(sample)
        app=SimpleNamespace(read_trip=lambda *args:SimpleNamespace(timeline=sample))
        self.reader.return_value=ToolResult("success",self.material("open"))
        packet, audit=asyncio.run(verify_timeline_stop(app,"auth","trip-a","d1-a",self.service,
                                         known_urls=(URL,),fixture_mode=True,trace=self.trace))
        self.assertEqual(packet["result"]["subject_id"],"hgs-west")
        self.assertEqual(audit["claim"]["target_date"],DAY)
        self.assertFalse(audit["claim"]["evidence_ids"])
        self.assertEqual(sample,original)

    def test_opening_schedule_does_not_inherit_undated_season(self):
        self.reader.return_value=ToolResult("success",self.material(
            text="自2025年7月21日至避暑季结束，黄果树东门的开放时间调整如下：\n黄果树东门：周一14:00至18:00；周二至周日9:00至18:00"))
        report=self.verify(known_urls=(URL,))
        self.assertEqual(report["claim"]["status"],"unconfirmed")
        self.assertFalse(report["claim"]["evidence_ids"])
        self.assertTrue(any(row["stage"]=="extract" and row["status"]=="success" for row in report["stages"]))
        self.assertIn("14:00",report["evidence"][0]["excerpt"])
