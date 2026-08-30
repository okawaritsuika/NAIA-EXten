from __future__ import annotations

import json
import sys
import types
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


EXTENSION_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = Path(__file__).resolve().parents[4] / "resources" / "naia-backend"
for path in (EXTENSION_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from naia_exten.features.server_random_prompt import (  # noqa: E402
    _ACTIVE_SERVER_RESPONSE,
    _ServerRandomPromptHook,
    ServerRandomPromptFeature,
)


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Context:
    def __init__(self, *, enabled=True):
        self._settings = {"feature__server_random_prompt__enabled": enabled}
        self._record = SimpleNamespace(is_active=True)
        self.logs = []
        self.saved = []

    def load_settings(self, defaults):
        return {**defaults, **self._settings}

    def log(self, message):
        self.logs.append(str(message))

    def save_settings(self, data):
        self.saved.append(dict(data))
        self._settings = dict(data)
        return True


class ServerRandomPromptTests(unittest.TestCase):
    def feature(self, *, enabled=True):
        ctx = _Context(enabled=enabled)
        app_context = SimpleNamespace()
        ext = SimpleNamespace(ctx=ctx, host=SimpleNamespace(app_context=app_context))
        feature = ServerRandomPromptFeature()
        feature._attach(ext)
        feature._context = app_context
        return feature, ctx, app_context

    def test_http_query_includes_counts_and_omits_random_preset(self):
        feature, _ctx, _app = self.feature()
        with patch(
            "naia_exten.features.server_random_prompt.urllib.request.urlopen",
            return_value=_Response({"character_prompts": {"male1": "m"}}),
        ) as urlopen:
            self.assertIsNotNone(feature._fetch_server_prompt(2, 3, feature.RANDOM_PRESET))
            request = urlopen.call_args.args[0]
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            self.assertEqual(
                query,
                {
                    "male_count": ["2"],
                    "female_count": ["3"],
                    "mark_used": ["true"],
                    "include_used": ["false"],
                },
            )

        with patch(
            "naia_exten.features.server_random_prompt.urllib.request.urlopen",
            return_value=_Response({"character_prompts": {"male1": "m"}}),
        ) as urlopen:
            feature._fetch_server_prompt(1, 1, "dreamlike")
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(urlopen.call_args.args[0].full_url).query
            )
            self.assertEqual(query["preset"], ["dreamlike"])

    def test_http_query_accepts_new_usage_options(self):
        feature, _ctx, _app = self.feature()
        with patch(
            "naia_exten.features.server_random_prompt.urllib.request.urlopen",
            return_value=_Response({"character_prompts": {}}),
        ) as urlopen:
            feature._fetch_server_prompt(
                1,
                2,
                feature.RANDOM_PRESET,
                mark_used=False,
                include_used=True,
            )
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(urlopen.call_args.args[0].full_url).query
            )
            self.assertEqual(query["mark_used"], ["false"])
            self.assertEqual(query["include_used"], ["true"])
            self.assertNotIn("exclude_used", query)

    def test_no_matching_scenario_404_is_not_logged_as_server_failure(self):
        feature, ctx, _app = self.feature()
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8765/api/scenarios/random",
            404,
            "not found",
            {},
            None,
        )
        with patch(
            "naia_exten.features.server_random_prompt.urllib.request.urlopen",
            side_effect=error,
        ):
            self.assertIsNone(feature._fetch_server_prompt(1, 1, feature.RANDOM_PRESET))
        self.assertEqual(ctx.logs, [])

    def test_generate_passes_usage_settings_to_server_request(self):
        feature, ctx, app_context = self.feature()
        ctx._settings.update(
            {
                feature.key("mark_used"): False,
                feature.key("include_used"): True,
            }
        )
        service = SimpleNamespace(context=app_context)
        def original(current_service):
            feature._apply_character_settings(
                lambda *_args: None,
                current_service,
                {"characters": ["boy", "2girls"]},
            )
            return "original"

        with patch.object(feature, "_fetch_server_prompt", return_value=None) as fetch:
            self.assertEqual(feature._generate(original, service), "original")
        fetch.assert_called_once_with(
            1,
            2,
            feature.RANDOM_PRESET,
            mark_used=False,
            include_used=True,
        )

    def test_generate_resolves_deferred_character_wildcards_for_server_counts(self):
        feature, _ctx, app_context = self.feature()
        service = SimpleNamespace(context=app_context)
        captured = {}

        def original(current_service):
            settings = {}
            captured["settings"] = settings
            feature._apply_character_settings(
                lambda *_args: None,
                current_service,
                settings,
            )
            return "original"

        resolved = {
            "characters": ["girl, first", "girl, second"],
            "uc": ["", ""],
            "character_ids": ["a", "b"],
        }
        with patch.object(feature, "_resolve_character_params_for_server", return_value=resolved):
            with patch.object(feature, "_fetch_server_prompt", return_value=None) as fetch:
                self.assertEqual(feature._generate(original, service), "original")

        self.assertEqual(captured["settings"]["characters"], resolved["characters"])
        fetch.assert_called_once_with(
            0,
            2,
            feature.RANDOM_PRESET,
            mark_used=True,
            include_used=False,
        )

    def test_prompt_engineering_controls_are_registered_as_hidden_settings(self):
        feature, _ctx, _app = self.feature()
        fields = feature.panel_fields()

        self.assertEqual(
            {field["key"] for field in fields},
            {
                "preset",
                "mark_used",
                "include_used",
                "preset_options",
                "server_status",
            },
        )
        self.assertTrue(all(field.get("visible_when") for field in fields))

    def test_panel_injection_uses_prompt_render_boundary_not_dom_observer(self):
        script = ServerRandomPromptFeature._PANEL_JS

        self.assertEqual(
            ServerRandomPromptFeature._PANEL_JS_MARKER,
            "/* NAIA_EXTEN_SERVER_RANDOM_PROMPT_PANEL_V5 */",
        )
        self.assertIn("__naiaExtenServerRandomPromptPanelV5", script)
        self.assertIn("previousRenderPromptEngineering", script)
        self.assertIn("data-server-mark-used", script)
        self.assertIn("data-server-include-used", script)
        self.assertIn("MARK_USED_KEY", script)
        self.assertIn("INCLUDE_USED_KEY", script)
        self.assertIn("PRESET_OPTIONS_KEY", script)
        self.assertIn("data-server-status", script)
        self.assertIn("PromptServer 연결 대기 중", script)
        self.assertIn("/api/presets", script)
        self.assertIn("serverPresetsRetry", script)
        self.assertIn("data?.presets", script)
        self.assertIn("char의 girl/boy 태그로 인원 수를 자동 판별", script)
        self.assertNotIn("data-server-male", script)
        self.assertNotIn("data-server-female", script)
        self.assertNotIn("new MutationObserver", script)
        ensure_start = script.index("function ensureRow()")
        ensure_end = script.index("// Prompt Engineering owns", ensure_start)
        ensure_body = script[ensure_start:ensure_end]
        self.assertGreater(
            ensure_body.index("const currentPreset = row.querySelector"),
            ensure_body.index("if (!row)"),
        )
        self.assertNotIn("if (preset &&", ensure_body)

    def test_generate_response_reaches_final_prompt_hook(self):
        feature, _ctx, app_context = self.feature()
        setattr(app_context, feature.FEATURE_ATTR, feature)
        context = SimpleNamespace(postfix_tags=["quality"], metadata={})

        with patch.dict(
            sys.modules,
            {
                "core": types.ModuleType("core"),
                "core.wildcard_processor": types.SimpleNamespace(
                    split_tags_smart=lambda text: [part.strip() for part in text.split(",")]
                ),
            },
        ):
            def original(service):
                feature._apply_character_settings(
                    lambda *_args: None,
                    service,
                    {"characters": ["girl"]},
                )
                return _ServerRandomPromptHook(app_context).execute_pipeline_hook(context)

            service = SimpleNamespace(context=app_context)
            with patch.object(
                feature,
                "_fetch_server_prompt",
                return_value={"base_prompt": "station, rain", "character_prompts": {}},
            ):
                result = feature._generate(original, service)

        self.assertIs(result, context)
        self.assertEqual(context.main_tags, [])
        self.assertEqual(
            context.postfix_tags,
            [feature.SERVER_MARKER, "station", "rain", "quality"],
        )

    def test_character_gender_counts_use_exact_boy_girl_tags(self):
        feature, _ctx, _app = self.feature()

        self.assertEqual(
            feature._character_gender_counts(
                ["boy, smile", "2girls", "6+boys", "cowgirl", "tomboy", "girl"]
            ),
            (7, 3),
        )

    def test_boy_girl_mapping_does_not_match_substrings(self):
        feature, _ctx, app_context = self.feature()
        app_context.__dict__[feature.RESPONSE_ATTR] = {
            "character_prompts": {
                "male1": "male response 1",
                "male2": "male response 2",
                "female1": "female response 1",
            }
        }
        original = lambda *_args, **_kwargs: {
            "characters": ["boy, smile", "cowgirl", "1girl", "tomboy", "2boys"],
            "uc": ["u"] * 5,
            "character_ids": ["a", "b", "c", "d", "e"],
        }
        result = feature._character_params(original, app_context)
        self.assertEqual(
            result["characters"],
            [
                "boy, smile, male response 1",
                "cowgirl",
                "1girl, female response 1",
                "tomboy",
                "2boys, male response 2",
            ],
        )
        self.assertEqual(result["uc"], ["u"] * 5)
        self.assertEqual(result["character_ids"], ["a", "b", "c", "d", "e"])

    def test_base_prompt_is_appended_to_postfix_once(self):
        feature, _ctx, app_context = self.feature()
        app_context.__dict__[feature.RESPONSE_ATTR] = {"base_prompt": "station, rain"}
        context = SimpleNamespace(postfix_tags=["quality"], metadata={})
        with patch.dict(
            sys.modules,
            {
                "core": types.ModuleType("core"),
                "core.wildcard_processor": types.SimpleNamespace(
                    split_tags_smart=lambda text: [part.strip() for part in text.split(",")]
                ),
            },
        ):
            feature._append_base_prompt(context)
            feature._append_base_prompt(context)
        self.assertEqual(context.main_tags, [])
        self.assertEqual(
            context.postfix_tags,
            [feature.SERVER_MARKER, "station", "rain", "quality"],
        )
        self.assertEqual(
            context.metadata[feature.CONTEXT_RESPONSE_KEY]["base_prompt"],
            "station, rain",
        )

    def test_base_prompt_wildcards_are_expanded_before_append(self):
        feature, _ctx, app_context = self.feature()
        app_context.wildcard_manager = object()
        app_context.__dict__[feature.RESPONSE_ATTR] = {"base_prompt": "station, __weather__"}
        context = SimpleNamespace(postfix_tags=["quality"], metadata={})

        class _Processor:
            def __init__(self, _manager):
                pass

            def expand_tags(self, tags, _context, **kwargs):
                self.location = kwargs["location"]
                return ["sunny" if tag == "__weather__" else tag for tag in tags]

        with patch.dict(
            sys.modules,
            {
                "core": types.ModuleType("core"),
                "core.wildcard_processor": types.SimpleNamespace(
                    split_tags_smart=lambda text: [part.strip() for part in text.split(",")],
                    WildcardProcessor=_Processor,
                ),
            },
        ):
            feature._append_base_prompt(context)

        self.assertEqual(context.main_tags, [])
        self.assertEqual(
            context.postfix_tags,
            [feature.SERVER_MARKER, "station", "sunny", "quality"],
        )

    def test_server_base_replaces_source_scene_but_keeps_pipeline_main_additions(self):
        feature, _ctx, app_context = self.feature()
        app_context.__dict__[feature.RESPONSE_ATTR] = {
            "base_prompt": "station, rain",
            "character_prompts": {},
        }
        context = SimpleNamespace(
            source_row={"general": "old scene"},
            main_tags=["old scene", "pipeline addition"],
            postfix_tags=["quality"],
            metadata={"boost_main_tags": ["old scene"]},
        )
        with patch.dict(
            sys.modules,
            {
                "core": types.ModuleType("core"),
                "core.wildcard_processor": types.SimpleNamespace(
                    split_tags_smart=lambda text: [part.strip() for part in text.split(",")]
                ),
            },
        ):
            feature._append_base_prompt(context)

        self.assertEqual(context.main_tags, [])
        self.assertEqual(
            context.postfix_tags,
            [feature.SERVER_MARKER, "station", "rain", "pipeline addition", "quality"],
        )
        self.assertIs(
            context.metadata[feature.CONTEXT_RESPONSE_KEY],
            app_context.__dict__[feature.RESPONSE_ATTR],
        )

    def test_empty_server_base_still_removes_random_source_scene(self):
        feature, _ctx, app_context = self.feature()
        app_context.__dict__[feature.RESPONSE_ATTR] = {
            "base_prompt": "",
            "character_prompts": {"female1": "server character"},
        }
        context = SimpleNamespace(
            source_row={"general": "old scene"},
            main_tags=["old scene", "pipeline addition"],
            postfix_tags=["quality"],
            metadata={"boost_main_tags": ["old scene"]},
        )

        with patch.dict(
            sys.modules,
            {
                "core": types.ModuleType("core"),
                "core.wildcard_processor": types.SimpleNamespace(
                    split_tags_smart=lambda text: [part.strip() for part in text.split(",")]
                ),
            },
        ):
            feature._append_base_prompt(context)

        self.assertEqual(context.main_tags, [])
        self.assertEqual(
            context.postfix_tags,
            [feature.SERVER_MARKER, "pipeline addition", "quality"],
        )

    def test_server_counts_are_visible_in_prefix_without_core_random_marker(self):
        feature, _ctx, app_context = self.feature()
        app_context.__dict__[feature.RESPONSE_ATTR] = {
            "base_prompt": "station",
            "character_prompts": {},
            feature._REQUESTED_MALE_COUNT_KEY: 2,
            feature._REQUESTED_FEMALE_COUNT_KEY: 7,
        }
        context = SimpleNamespace(
            main_tags=["old scene"],
            prefix_tags=["artist"],
            postfix_tags=["quality"],
            metadata={"boost_main_tags": ["old scene"]},
        )
        feature._append_base_prompt(context)

        self.assertEqual(context.main_tags, [])
        self.assertEqual(context.prefix_tags, ["2boys", "6+girls", "artist"])
        self.assertEqual(
            context.postfix_tags,
            [feature.SERVER_MARKER, "station", "quality"],
        )

    def test_character_mapping_handles_counted_tags_and_fallback_without_drops(self):
        feature, _ctx, app_context = self.feature()
        app_context.__dict__[feature.RESPONSE_ATTR] = {
            "character_prompts": {
                "male1": "male response 1",
                "male2": "male response 2",
                "male3": "must not be used",
                "female1": "female response 1",
                "female2": "female response 2",
                "female3": "female response 3",
                "female4": "female response 4",
                "female5": "female response 5",
                "female6": "female response 6",
                "female7": "must not be used",
            },
            feature._REQUESTED_MALE_COUNT_KEY: 2,
            feature._REQUESTED_FEMALE_COUNT_KEY: 6,
        }
        original = lambda *_args, **_kwargs: {
            "characters": ["", "2girls", "1boy", "6+girls"],
            "uc": [""] * 4,
        }
        result = feature._character_params(original, app_context)

        self.assertEqual(
            result["characters"],
            [
                "male response 2",
                "2girls, female response 1, female response 2",
                "1boy, male response 1",
                "6+girls, female response 3, female response 4, female response 5, female response 6",
            ],
        )

    def test_snapshot_character_settings_receive_overlay_without_snapshot_mutation(self):
        feature, _ctx, app_context = self.feature()
        response = {
            "character_prompts": {"female1": "server character"},
            feature._REQUESTED_MALE_COUNT_KEY: 0,
            feature._REQUESTED_FEMALE_COUNT_KEY: 1,
        }
        app_context.__dict__[feature.RESPONSE_ATTR] = response
        snapshot_characters = ["1girl"]
        settings = {"characters": list(snapshot_characters), "uc": [""]}
        service = SimpleNamespace(context=app_context)

        def original(_service, current_settings):
            current_settings["characters"] = list(snapshot_characters)
            return None

        feature._apply_character_settings(original, service, settings)
        self.assertEqual(settings["characters"], ["1girl, server character"])
        self.assertEqual(snapshot_characters, ["1girl"])

        # A second application to the same per-run settings is a no-op.
        feature._apply_character_settings(lambda *_args: None, service, settings)
        self.assertEqual(settings["characters"], ["1girl, server character"])

    def test_snapshot_overlay_strips_previous_server_suffix_before_new_response(self):
        feature, _ctx, app_context = self.feature()
        old_response = {
            "character_prompts": {
                "female1": "old female one",
                "female2": "old female two",
            },
            feature._REQUESTED_MALE_COUNT_KEY: 0,
            feature._REQUESTED_FEMALE_COUNT_KEY: 2,
        }
        new_response = {
            "character_prompts": {
                "female1": "new female one",
                "female2": "new female two",
            },
            feature._REQUESTED_MALE_COUNT_KEY: 0,
            feature._REQUESTED_FEMALE_COUNT_KEY: 2,
        }
        app_context.current_prompt_context = SimpleNamespace(
            metadata={feature.CONTEXT_RESPONSE_KEY: old_response}
        )
        app_context.__dict__[feature.RESPONSE_ATTR] = new_response
        snapshot_characters = ["2girls, old female one, old female two"]
        original_snapshot = {
            "characters": list(snapshot_characters),
            "uc": [""],
        }
        app_context._character_roll_snapshot = {"NAI": original_snapshot}
        settings = {"characters": list(snapshot_characters), "uc": [""]}
        service = SimpleNamespace(context=app_context)

        token = _ACTIVE_SERVER_RESPONSE.set(new_response)
        try:
            fake_character_settings = types.ModuleType("core.character_settings")

            def store_snapshot(context, params, mode):
                snapshot = {
                    "characters": list(params["characters"]),
                    "uc": list(params.get("uc") or []),
                    "character_ids": list(params.get("character_ids") or []),
                }
                context._character_roll_snapshot = {
                    str(mode).upper(): {
                        **snapshot,
                    }
                }
                return context._character_roll_snapshot[str(mode).upper()]

            fake_character_settings.store_character_roll_snapshot = store_snapshot
            fake_character_settings.clear_character_roll_snapshot = lambda *_args: None
            with patch.dict(sys.modules, {"core.character_settings": fake_character_settings}):
                feature._apply_character_settings(lambda *_args: None, service, settings)
        finally:
            _ACTIVE_SERVER_RESPONSE.reset(token)

        self.assertEqual(
            settings["characters"],
            ["2girls, new female one, new female two"],
        )
        self.assertEqual(snapshot_characters, ["2girls, old female one, old female two"])
        self.assertEqual(
            original_snapshot["characters"],
            ["2girls, old female one, old female two"],
        )
        self.assertEqual(
            app_context._character_roll_snapshot["NAI"]["characters"],
            ["2girls, new female one, new female two"],
        )

    def test_resync_character_params_uses_tracker_and_updates_it(self):
        feature, _ctx, app_context = self.feature()
        old_response = {
            "character_prompts": {"female1": "old female"},
        }
        new_response = {
            "character_prompts": {"female1": "new female"},
        }
        app_context.__dict__[feature.RESPONSE_ATTR] = new_response
        app_context.__dict__[feature.PERSISTED_RESPONSE_ATTR] = old_response
        app_context.current_prompt_context = SimpleNamespace(
            metadata={feature.CONTEXT_RESPONSE_KEY: new_response}
        )
        original = lambda *_args, **_kwargs: {
            "characters": ["1girl, old female"],
            "uc": [""],
        }

        result = feature._character_params(original, app_context)

        self.assertEqual(result["characters"], ["1girl, new female"])
        self.assertIs(
            getattr(app_context, feature.PERSISTED_RESPONSE_ATTR),
            new_response,
        )

    def test_disabled_server_feature_cleans_tracked_suffix_from_snapshot_settings(self):
        feature, _ctx, app_context = self.feature(enabled=False)
        old_response = {"character_prompts": {"female1": "old female"}}
        app_context.__dict__[feature.PERSISTED_RESPONSE_ATTR] = old_response
        settings = {"characters": ["1girl, old female"], "uc": [""]}
        service = SimpleNamespace(context=app_context)

        with patch.object(feature, "_store_character_snapshot", return_value=True) as store:
            feature._apply_character_settings(lambda *_args: None, service, settings)

        self.assertEqual(settings["characters"], ["1girl"])
        store.assert_called_once()
        self.assertFalse(hasattr(app_context, feature.PERSISTED_RESPONSE_ATTR))

    def test_failed_server_response_cleans_tracked_suffix_from_snapshot_settings(self):
        feature, _ctx, app_context = self.feature(enabled=True)
        old_response = {"character_prompts": {"female1": "old female"}}
        app_context.__dict__[feature.PERSISTED_RESPONSE_ATTR] = old_response
        app_context.__dict__[feature.RESPONSE_ATTR] = None
        settings = {"characters": ["1girl, old female"], "uc": [""]}
        service = SimpleNamespace(context=app_context)

        with patch.object(feature, "_store_character_snapshot", return_value=True) as store:
            feature._apply_character_settings(lambda *_args: None, service, settings)

        self.assertEqual(settings["characters"], ["1girl"])
        store.assert_called_once()
        self.assertFalse(hasattr(app_context, feature.PERSISTED_RESPONSE_ATTR))

    def test_server_character_prompt_leaves_wildcard_for_followup_character_hook(self):
        feature, _ctx, app_context = self.feature()
        app_context.__dict__[feature.RESPONSE_ATTR] = {
            "character_prompts": {"male1": "__shared__"}
        }
        original = lambda *_args, **_kwargs: {"characters": ["1boy"], "uc": [""]}
        result = feature._character_params(original, app_context)

        self.assertEqual(result["characters"], ["1boy, __shared__"])

    def test_generate_time_character_roll_reuses_random_context_response(self):
        feature, _ctx, app_context = self.feature()
        app_context.current_prompt_context = SimpleNamespace(
            metadata={
                feature.CONTEXT_RESPONSE_KEY: {
                    "character_prompts": {"female1": "late response"}
                }
            }
        )
        original = lambda *_args, **_kwargs: {"characters": ["girl"], "uc": [""]}

        result = feature._character_params(original, app_context)

        self.assertEqual(result["characters"], ["girl, late response"])

    def test_disabled_or_failed_server_keeps_original_and_cleans_run_attr(self):
        feature, _ctx, app_context = self.feature(enabled=False)
        service = SimpleNamespace(context=app_context)
        original = Mock(return_value="original")
        with patch.object(feature, "_fetch_server_prompt") as fetch:
            self.assertEqual(feature._generate(original, service), "original")
            fetch.assert_not_called()
        self.assertFalse(hasattr(app_context, feature.RESPONSE_ATTR))

        feature, _ctx, app_context = self.feature(enabled=True)
        service = SimpleNamespace(context=app_context)
        original = Mock(return_value="original")
        with patch.object(feature, "_fetch_server_prompt", return_value=None):
            self.assertEqual(feature._generate(original, service), "original")
        self.assertFalse(hasattr(app_context, feature.RESPONSE_ATTR))
        self.assertEqual(original.call_count, 1)


if __name__ == "__main__":
    unittest.main()
