from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


EXTENSION_ROOT = Path(__file__).resolve().parents[1]
if str(EXTENSION_ROOT) not in sys.path:
    sys.path.insert(0, str(EXTENSION_ROOT))

from naia_exten.features.gsqe_probability import GSQEProbabilityFeature


class _SearchResults:
    def __init__(self):
        self.count_calls = 0
        self.pop_calls = 0

    def get_count_by_rating(self):
        self.count_calls += 1
        return {rating: 1 for rating in "gsqe"}

    def pop_random_row(self, _ratings):
        self.pop_calls += 1
        return {"id": 3} if self.pop_calls == 3 else None


class GSQEProbabilityTests(unittest.TestCase):
    def test_panel_does_not_replace_naia_extensions_renderer(self):
        script = GSQEProbabilityFeature._PANEL_JS
        self.assertNotIn("renderExtensions =", script)
        self.assertIn(".observe(moduleBody, {childList: true})", script)
        self.assertNotIn("subtree: true", script)

    def test_bundled_weights_override_legacy_fields(self):
        class _Context:
            @staticmethod
            def load_settings(defaults):
                return {
                    **defaults,
                    "feature__gsqe_probability__weights_json": (
                        '{"g":10,"s":20,"q":30,"e":40}'
                    ),
                    "feature__gsqe_probability__g_pct": 99,
                }

        feature = GSQEProbabilityFeature()
        feature.ext = SimpleNamespace(ctx=_Context())

        self.assertEqual(
            feature._weights(),
            {"g": 10.0, "s": 20.0, "q": 30.0, "e": 40.0},
        )

    def test_weighted_retry_reads_settings_and_counts_once(self):
        feature = GSQEProbabilityFeature()
        feature.ext = SimpleNamespace(
            features=SimpleNamespace(get=lambda _feature_id: None),
        )
        search_results = _SearchResults()

        with patch.object(
            feature,
            "_weights",
            return_value={rating: 25.0 for rating in "gsqe"},
        ) as read_weights, patch(
            "naia_exten.features.gsqe_probability.random.random",
            return_value=0.0,
        ):
            row = feature._pop_weighted_random_row(search_results, set("gsqe"))

        self.assertEqual(row, {"id": 3})
        self.assertEqual(read_weights.call_count, 1)
        self.assertEqual(search_results.count_calls, 1)
        self.assertEqual(search_results.pop_calls, 3)


if __name__ == "__main__":
    unittest.main()
