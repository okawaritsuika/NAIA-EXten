from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from naia_exten.features.comic_maker.feature import ComicMakerFeature
from naia_exten.features.comic_maker.chooser_ui import CHOOSER_JS
from test_comic_maker import _FakeClient, _FakeContext, comic_plan


class ComicStoredTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = _FakeContext(Path(self.temp.name))
        self.feature = ComicMakerFeature()
        self.feature._attach(SimpleNamespace(ctx=self.ctx, refresh_panel=lambda: None))
        self.client = self.feature._client = _FakeClient(comic_plan())
        self.addCleanup(self.feature.unregister)

    def wait(self):
        self.feature._stored_worker.join(3)
        self.assertFalse(self.feature._stored_worker.is_alive())

    def test_saved_button_loads_plan_without_generating_until_confirmed(self):
        self.feature.handle_action(self.feature.key("make_saved"))
        self.wait()
        self.assertEqual(self.client.queries, [{"male_count": 1, "female_count": 1, "mark_used": False}])
        self.assertIsNotNone(self.feature._stored_pending)
        self.assertEqual(self.ctx.enqueued, [])
        self.assertEqual(self.client.generate_queries, [])
        self.feature.handle_action(self.feature.key("cancel_stored"))
        self.assertIsNone(self.feature._stored_pending)

    def test_removed_actions_cannot_call_server(self):
        with patch.object(self.feature._client, "_request", create=True) as request:
            for action in ("make_grok", "resume_grok", "confirm_grok"):
                self.feature.handle_action(self.feature.key(action))
            request.assert_not_called()
        self.assertEqual(self.ctx.enqueued, [])
        self.assertNotIn("grok", CHOOSER_JS.lower())
        self.assertFalse(any("grok" in field["key"] for field in self.feature.panel_fields()))

    def test_stored_can_finish_and_stop_without_resetting_nai_planning(self):
        entered, release = threading.Event(), threading.Event()
        original = self.client.generate_plan

        def delayed_nai(payload, progress=None):
            entered.set()
            release.wait(3)
            return original(payload, progress=progress)

        self.client.generate_plan = delayed_nai
        self.feature.handle_action(self.feature.key("make_nai"))
        try:
            self.assertTrue(entered.wait(1))
            self.feature._start_stored()
            self.wait()
            self.feature.handle_action(self.feature.key("confirm_stored"))
            self.assertTrue(self.feature._planning)
            stored = self.feature._stored_run
            self.assertIsNotNone(stored)
            stored_requests = list(stored.requests)
            self.feature.handle_action(self.feature.key("reset_stored"))
            self.assertTrue(self.feature._planning)
            self.assertIsNone(self.feature._stored_run)
            self.assertEqual(self.ctx.cancelled, stored_requests)
        finally:
            release.set()
            self.feature._nai_worker.join(2)
        self.assertIsNotNone(self.feature._active_run)
        self.assertFalse(self.feature._planning)


    def test_nai_stop_discards_late_plan_and_keeps_stored_running(self):
        entered, release = threading.Event(), threading.Event()
        original = self.client.generate_plan

        def delayed_nai(payload, progress=None):
            entered.set()
            release.wait(3)
            return original(payload, progress=progress)

        self.client.generate_plan = delayed_nai
        self.feature.handle_action(self.feature.key("make_nai"))
        try:
            self.assertTrue(entered.wait(1))
            self.feature._start_stored()
            self.wait()
            self.feature.handle_action(self.feature.key("confirm_stored"))
            stored = self.feature._stored_run
            self.feature.handle_action(self.feature.key("reset"))
            self.assertIs(self.feature._stored_run, stored)
            self.assertEqual(self.ctx.cancelled, [])
        finally:
            release.set()
            self.feature._nai_worker.join(2)
        self.assertIsNone(self.feature._active_run)
        self.assertIsNone(self.feature._pending)
        self.assertEqual(len(self.ctx.enqueued), 1)


    def test_stored_plan_confirmation_uses_stored_slot_while_nai_is_generating(self):
        self.feature.handle_action(self.feature.key("make_nai"))
        self.feature._nai_worker.join(2)
        nai = self.feature._active_run
        self.feature.handle_action(self.feature.key("make_saved"))
        self.wait()
        self.assertIs(self.feature._active_run, nai)
        self.assertIsNotNone(self.feature._stored_pending)
        self.assertEqual(len(self.ctx.enqueued), 1)
        self.feature.handle_action(self.feature.key("confirm_stored"))
        self.assertIs(self.feature._active_run, nai)
        self.assertIsNotNone(self.feature._stored_run)
        self.assertEqual(len(self.ctx.enqueued), 2)


    def test_nai_chooser_does_not_generate_and_large_mode_uses_each_panel(self):
        self.feature.handle_action(self.feature.key("make"))
        self.assertEqual(self.client.generate_queries, [])
        self.feature.handle_action(self.feature.key("make_ja"))
        self.feature._nai_worker.join(2)
        self.assertEqual(len(self.client.generate_queries), 1)
        self.assertTrue(self.feature._active_run.pending.single_panel_mode)
        self.assertEqual(len(self.ctx.enqueued), 2)
        self.assertEqual(self.ctx.queue_starts, 1)
