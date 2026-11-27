"""Offline routing checks; no network or account required."""
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import travel_search_mcp as search


class SearchRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.row = {"title": "青岩夜游公告", "url": "https://venue.example/notice",
                    "excerpt": "青岩夜游安排", "published_at": "2026-11-28"}
        self.exa = self.enterContext(patch.object(search, "search_exa", new_callable=AsyncMock))
        self.backup = self.enterContext(patch.object(search, "search_anysearch", new_callable=AsyncMock))

    async def test_no_backup_after_exa_match(self):
        self.exa.return_value = [self.row, self.row]
        result = await search.search_travel_sources("青岩夜游", "青岩", required_terms=["夜游"])
        self.assertEqual(len(result["sources"]), 1)
        self.backup.assert_not_awaited()

    async def test_published_date_does_not_confirm_event(self):
        self.exa.return_value = [self.row]
        self.backup.return_value = [{**self.row, "excerpt": "青岩夜游演出日期为2026年11月28日。"}]
        result = await search.search_travel_sources("青岩夜游", "青岩", target_date=date(2026, 11, 28))
        self.assertEqual([a["status"] for a in result["attempts"]], ["no_match", "matched"])
        self.assertEqual(result["sources"][0]["provider"], "anysearch")

    async def test_timeout_falls_back(self):
        self.exa.side_effect = TimeoutError()
        self.backup.return_value = [self.row]
        result = await search.search_travel_sources("青岩夜游", "青岩")
        self.assertEqual(result["attempts"][0]["reason"], "timeout")
        self.assertEqual(result["status"], "candidates")

    async def test_two_errors(self):
        self.exa.side_effect = TimeoutError()
        self.backup.side_effect = ValueError("invalid_provider_response")
        result = await search.search_travel_sources("青岩夜游", "青岩")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["sources"], [])

    async def test_rejected_source_and_domain_boundary(self):
        self.exa.return_value = [self.row, {**self.row, "url": "https://venue.example.other.org/notice"}]
        self.backup.return_value = [self.row]
        result = await search.search_travel_sources("青岩夜游", "青岩", source_domains=["venue.example"],
                                                    rejected_urls=[self.row["url"]])
        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(result["sources"], [])
