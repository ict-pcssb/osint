import unittest

from support.guards import Trace
from support.store import FixtureStore


class StorageFixtureTests(unittest.TestCase):
    def test_real_sqlite_fixture_rollback_and_authorization(self):
        trace = Trace("m01-storage-fixture")
        with FixtureStore(trace) as store:
            def scenario():
                with store.transaction():
                    store.create("fixture-A", "trip-1", "A")
                    store.create("fixture-B", "trip-1", "B")
                    self.assertEqual(store.read("fixture-A", "trip-1"), "A")
                    self.assertEqual(store.read("fixture-B", "trip-1"), "B")
                    store.create("fixture-A", "A-private", "private")
                    self.assertIsNone(store.read("fixture-B", "A-private"))
                    self.assertFalse(store.update("fixture-B", "A-private", "altered"))
                    self.assertEqual(store.read("fixture-A", "A-private"), "private")
                self.assertEqual(store.row_count(), 0)

            scenario()
            scenario()
            self.assertEqual(store.writes.count, 6)
            self.assertTrue(any(event["event"] == "storage_write_rejected" for event in trace.events))

