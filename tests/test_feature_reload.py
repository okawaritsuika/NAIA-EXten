from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


EXTENSION_ROOT = Path(__file__).resolve().parents[1]
PORTABLE_ROOT = Path(__file__).resolve().parents[4]
BACKEND_ROOT = PORTABLE_ROOT / "resources" / "naia-backend"
for path in (EXTENSION_ROOT, BACKEND_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from naia_exten.feature_manager import FeatureManager
from naia_exten.host_bridge import HostBridge
from naia_exten.patch_manager import PatchManager


class _Context:
    def __init__(self, parquet_dir: Path):
        self._settings = {}
        self._app_context = SimpleNamespace(
            custom_parquet_dir=lambda: parquet_dir,
        )
        self._record = SimpleNamespace(is_active=True)
        self.logs = []

    def load_settings(self, defaults):
        return {**defaults, **self._settings}

    def save_settings(self, settings):
        self._settings = dict(settings)

    def subscribe(self, _event_name, _callback, **_kwargs):
        return None

    def unsubscribe(self, _event_name, _callback):
        return None

    def log(self, message):
        self.logs.append(str(message))


class FeatureReloadTests(unittest.TestCase):
    def test_all_features_register_and_reload_without_patch_conflicts(self):
        with patch.dict(sys.modules, self._host_modules()), tempfile.TemporaryDirectory() as temp_dir:
            ctx = _Context(Path(temp_dir))
            ext = SimpleNamespace(ctx=ctx, patches=PatchManager())
            ext.host = HostBridge(ctx)
            ext.features = FeatureManager(ext)

            try:
                ext.features.discover()
                ext.features.register_all()
                self.assertEqual(ext.features.errors, {}, ext.features.errors)
                self._assert_composed_state(ext)

                ok, message = ext.features.reload_all()
                self.assertTrue(ok, message)
                self.assertEqual(ext.features.errors, {}, ext.features.errors)
                self._assert_composed_state(ext)
            finally:
                ext.features.unregister_all()
                ext.patches.restore_all()

    def _assert_composed_state(self, ext):
        patches = ext.patches.list_patches()
        panel_fields = ext.features.build_panel_fields()
        toggles = {
            field["key"]: field
            for field in panel_fields
            if str(field.get("key", "")).endswith("__enabled")
        }
        visible_toggle_ids = {
            feature_id
            for feature_id, feature in ext.features.features.items()
            if "visible_when" not in toggles[feature.enabled_key]
        }
        self.assertEqual(
            visible_toggle_ids,
            {
                "character_same_wildcard",
                "multi_parquet_pool",
                "gsqe_probability",
                "server_random_prompt",
            },
        )
        self.assertEqual(
            toggles["feature__parquet_live_sync__enabled"].get("visible_when", {}).get("in"),
            ["1"],
        )
        for field in panel_fields:
            key = str(field.get("key", ""))
            if key.startswith("feature__") and not key.endswith("__enabled"):
                hidden = field.get("visible_when") or {}
                if field.get("type") == "action":
                    if key == "feature__comic_maker__make":
                        self.assertNotIn("visible_when", field)
                        continue
                    hidden_field = str(hidden.get("field", ""))
                    self.assertTrue(
                        hidden_field == key.rsplit("__", 1)[0] + "__enabled"
                        or hidden_field.endswith("internal_never__"),
                        key,
                    )
                    continue
                hidden_field = str(hidden.get("field", ""))
                self.assertTrue(
                    hidden_field.endswith("__naia_exten_internal_never__")
                    or hidden_field.endswith("__internal_never__")
                    or hidden_field.endswith("internal_never__"),
                    key,
                )
                self.assertEqual(hidden.get("in"), ["1"], key)

        app_js_injections = [
            item
            for item in patches
            if item["target"] == "web" and item["method"] == "inject:app.js"
        ]
        self.assertEqual(
            {item["owner"] for item in app_js_injections},
            {
                "parquet_live_sync",
                "character_same_wildcard",
                "multi_parquet_pool",
                "gsqe_probability",
                "server_random_prompt",
            },
        )
        self.assertEqual(len(app_js_injections), 5)

        pop_layers = [
            item
            for item in patches
            if item["method"] == "pop_random_row"
        ]
        self.assertEqual(
            {item["owner"] for item in pop_layers},
            {"parquet_live_sync", "multi_parquet_pool"},
        )

    @staticmethod
    def _host_modules():
        def package(name):
            module = types.ModuleType(name)
            module.__path__ = []
            return module

        def module(name, **members):
            value = types.ModuleType(name)
            for key, member in members.items():
                setattr(value, key, member)
            return value

        class SearchResultModel:
            def pop_random_row(self, active_ratings=None):
                return None

            def pop_random_row_matching(self, *args, **kwargs):
                return None

            def pop_random_row_matching_tags(self, *args, **kwargs):
                return None

            def pop_random_row_with_id_filter(self, *args, **kwargs):
                return None

        class WildcardProcessor:
            def _get_wildcard_line(self, wildcard_name, context):
                return None

        class PromptGenerationService:
            def prepare_next_source(self, *args, **kwargs):
                return None

        class HeadlessRandomPromptService:
            def generate(self, *args, **kwargs):
                return None

            def _pop_active_tag_filter_source_row(self, *args, **kwargs):
                return None, None, ""

            def reserve_next_random_row(self, active_ratings=None):
                return None

        class PromptSourcePreparation:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        return {
            "pandas": module("pandas"),
            "app": package("app"),
            "app.backend": package("app.backend"),
            "app.backend.server": package("app.backend.server"),
            "app.backend.server.web_shell_routes": module(
                "app.backend.server.web_shell_routes",
                _web_file=lambda path, media_type: None,
                _no_cache_headers=lambda: {},
            ),
            "app.backend.server.search_commands": module(
                "app.backend.server.search_commands",
                load_or_merge_custom_parquet=lambda *args, **kwargs: None,
                run_search_command=lambda *args, **kwargs: None,
            ),
            "app.backend.server.params_workflow_routes": module(
                "app.backend.server.params_workflow_routes",
                _apply_uploaded_search_parquet=lambda *args, **kwargs: None,
            ),
            "core": package("core"),
            "core.search_result_model": module(
                "core.search_result_model",
                SearchResultModel=SearchResultModel,
            ),
            "core.character_settings": module(
                "core.character_settings",
                character_params_from_settings=lambda *args, **kwargs: None,
            ),
            "core.wildcard_processor": module(
                "core.wildcard_processor",
                WildcardProcessor=WildcardProcessor,
            ),
            "core.prompt_generation_service": module(
                "core.prompt_generation_service",
                PromptGenerationService=PromptGenerationService,
                PromptSourcePreparation=PromptSourcePreparation,
            ),
            "core.headless_random_prompt_service": module(
                "core.headless_random_prompt_service",
                HeadlessRandomPromptService=HeadlessRandomPromptService,
            ),
        }


if __name__ == "__main__":
    unittest.main()
