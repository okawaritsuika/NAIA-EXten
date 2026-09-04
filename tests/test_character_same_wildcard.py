from __future__ import annotations

import sys
import types
import unittest
import weakref
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


EXTENSION_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = Path(__file__).resolve().parents[4] / "resources" / "naia-backend"
for path in (EXTENSION_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from naia_exten.features.character_same_wildcard import CharacterSameWildcardFeature
from naia_exten.patch_manager import PatchManager


class _Context:
    def __init__(self, app_context):
        self._app_context = app_context
        self._record = SimpleNamespace(is_active=True)

    def load_settings(self, defaults):
        values = dict(defaults)
        values["feature__character_same_wildcard__enabled"] = True
        return values


class _WildcardManager:
    wildcard_dict_tree = {
        "colors": [(1.0, "blue")],
    }
    instant_wildcard_tree = {}
    instant_wildcard_dict = {}

    def __init__(self, app_context):
        self._app_context_ref = weakref.ref(app_context)


class _AppContext:
    pass


class CharacterSameWildcardTests(unittest.TestCase):
    def _feature(self, app_context):
        feature = CharacterSameWildcardFeature()
        feature.ext = SimpleNamespace(
            ctx=_Context(app_context),
            patches=PatchManager(),
        )
        feature._context = app_context
        return feature

    def test_reuses_character_value_and_clears_cache_between_calls(self):
        app_context = _AppContext()
        feature = self._feature(app_context)
        processor = SimpleNamespace(
            _find_wildcard_key=lambda name: "colors" if name == "colors" else None,
            _record_roll=lambda *_args: None,
        )
        prompt_context = SimpleNamespace(
            _wc_location="character",
            wildcard_history={},
        )
        calls = []

        def original(_processor, wildcard_name, _context):
            calls.append(wildcard_name)
            return "blue"

        def resolve(_app_context):
            setattr(_app_context, feature.CACHE_ATTR, {})
            first = feature._get_wildcard_line(original, processor, "colors", prompt_context)
            second = feature._get_wildcard_line(original, processor, "colors", prompt_context)
            return {"characters": [first, second]}

        feature._character_params_scope(resolve, app_context)
        self.assertEqual(calls, ["colors"])
        self.assertFalse(hasattr(app_context, feature.CACHE_ATTR))

        outside = feature._get_wildcard_line(original, processor, "colors", prompt_context)
        self.assertEqual(outside, "blue")
        self.assertEqual(calls, ["colors", "colors"])

    def test_server_character_suffix_uses_same_cache_as_base_character(self):
        app_context = _AppContext()
        app_context.current_prompt_context = SimpleNamespace(
            wildcard_history={},
            wildcard_rolls=[],
            sequential_counters={},
            wildcard_state={},
            global_append_tags=[],
        )
        app_context.wildcard_manager = _WildcardManager(app_context)
        app_context._naia_exten_server_random_prompt_response = {
            "character_prompts": {"male1": "__colors__"},
        }
        feature = self._feature(app_context)

        class FakeWildcardProcessor:
            def __init__(self, manager):
                self.wildcard_manager = manager

            def _find_wildcard_key(self, name):
                return "colors" if name == "colors" else None

            def _record_roll(self, *_args):
                return None

            def expand_tags(self, tags, context, *, location=None, slot=None, slot_label=None):
                context._wc_location = location
                context._wc_slot = slot
                context._wc_slot_label = slot_label
                output = []
                for tag in tags:
                    if tag == "__colors__":
                        output.append(
                            self.feature._get_wildcard_line(
                                lambda _processor, _name, _context: "blue",
                                self,
                                "colors",
                                context,
                            )
                        )
                    else:
                        output.append(tag)
                return output

        FakeWildcardProcessor.feature = feature

        fake_core = types.ModuleType("core")
        fake_core.__path__ = []
        fake_wildcard = types.ModuleType("core.wildcard_processor")
        fake_wildcard.WildcardProcessor = FakeWildcardProcessor
        fake_wildcard.split_tags_smart = lambda text: str(text).split(",")

        def server_layer(next_layer, context):
            result = next_layer(context)
            result["characters"][0] += ", __colors__"
            return result

        def host_character_params(context):
            processor = FakeWildcardProcessor(context.wildcard_manager)
            prompt = processor.expand_tags(
                ["boys", "__colors__"],
                context.current_prompt_context,
                location="character",
                slot="slot-1",
                slot_label=1,
            )
            return {
                "characters": [", ".join(prompt)],
                "uc": [""],
                "character_ids": ["slot-1"],
            }

        with patch.dict(
            sys.modules,
            {
                "core": fake_core,
                "core.wildcard_processor": fake_wildcard,
            },
        ):
            result = feature._character_params_scope(
                lambda context: server_layer(host_character_params, context),
                app_context,
            )
        self.assertEqual(result["characters"], ["boys, blue, blue"])
        self.assertFalse(hasattr(app_context, feature.CACHE_ATTR))

    def test_general_location_does_not_use_character_cache(self):
        app_context = SimpleNamespace()
        feature = self._feature(app_context)
        app_context.__dict__[feature.CACHE_ATTR] = {":colors": ("colors", "cached")}
        processor = SimpleNamespace(
            _find_wildcard_key=lambda name: "colors" if name == "colors" else None,
        )
        context = SimpleNamespace(_wc_location="main", wildcard_history={})
        calls = []

        def original(_processor, _name, _context):
            calls.append(True)
            return "fresh"

        self.assertEqual(
            feature._get_wildcard_line(original, processor, "colors", context),
            "fresh",
        )
        self.assertEqual(calls, [True])

    def test_resync_action_rolls_fresh_snapshot_for_active_characters(self):
        app_context = _AppContext()
        app_context.current_api_mode = "NAI"
        feature = self._feature(app_context)
        calls = []

        fake_core = types.ModuleType("core")
        fake_core.__path__ = []
        fake_character_settings = types.ModuleType("core.character_settings")

        def roll_character_params(context, *, mode, reuse_current_context):
            calls.append((context, mode, reuse_current_context))
            return {"characters": ["boys, blue", "girls, blue"], "uc": ["", ""]}

        fake_character_settings.roll_character_params = roll_character_params
        with patch.dict(
            sys.modules,
            {
                "core": fake_core,
                "core.character_settings": fake_character_settings,
            },
        ):
            feature.handle_action(feature.key(feature.RESYNC_ACTION))

        self.assertEqual(calls, [(app_context, "NAI", False)])

    def test_extension_panel_exposes_resync_action_when_enabled(self):
        fields = CharacterSameWildcardFeature().panel_fields()
        self.assertEqual(fields[0]["type"], "action")
        self.assertEqual(fields[0]["key"], "resync_now")
        self.assertEqual(
            fields[0]["visible_when"],
            {"field": "__enabled__", "in": [True]},
        )


if __name__ == "__main__":
    unittest.main()
