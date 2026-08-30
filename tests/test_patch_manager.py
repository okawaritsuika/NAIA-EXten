from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from naia_exten.patch_manager import PatchManager


class _Target:
    def call(self, value):
        return f"base:{value}"


class _Response:
    def __init__(self, *, content, media_type, headers):
        self.content = content
        self.media_type = media_type
        self.headers = headers


class PatchManagerTests(unittest.TestCase):
    def test_layers_compose_and_restore_by_owner(self):
        manager = PatchManager()
        events = []

        def observe(result, target, value):
            events.append(("observe", result))

        def replace(next_layer, target, value):
            events.append(("replace", value))
            return next_layer(target, value + 1) + ":outer"

        manager.wrap_method(
            owner="observer",
            target=_Target,
            method_name="call",
            after=observe,
        )
        manager.wrap_method(
            owner="selector",
            target=_Target,
            method_name="call",
            replace=replace,
        )

        self.assertEqual(_Target().call(3), "base:4:outer")
        self.assertEqual(events, [("replace", 3), ("observe", "base:4")])

        manager.restore_owner("selector")
        self.assertEqual(_Target().call(5), "base:5")
        self.assertTrue(manager.is_patched(_Target, "call"))

        manager.restore_owner("observer")
        self.assertEqual(_Target().call(7), "base:7")
        self.assertFalse(manager.is_patched(_Target, "call"))

    def test_same_owner_cannot_accidentally_duplicate_a_layer(self):
        manager = PatchManager()
        manager.wrap_method(
            owner="feature",
            target=_Target,
            method_name="call",
            after=lambda result, *args, **kwargs: None,
        )
        with self.assertRaisesRegex(RuntimeError, "already patched by feature"):
            manager.wrap_method(
                owner="feature",
                target=_Target,
                method_name="call",
                before=lambda *args, **kwargs: None,
            )
        manager.restore_all()

    def test_web_snippets_compose_deduplicate_and_remove_by_owner(self):
        web_routes = types.ModuleType("app.backend.server.web_shell_routes")
        original_calls = []

        def web_file(path, media_type):
            original_calls.append((path, media_type))
            return "original"

        web_routes._web_file = web_file
        web_routes._no_cache_headers = lambda: {"Cache-Control": "no-store"}

        modules = {
            "app": types.ModuleType("app"),
            "app.backend": types.ModuleType("app.backend"),
            "app.backend.server": types.ModuleType("app.backend.server"),
            "app.backend.server.web_shell_routes": web_routes,
            "fastapi": types.ModuleType("fastapi"),
            "fastapi.responses": types.ModuleType("fastapi.responses"),
        }
        modules["fastapi.responses"].Response = _Response

        with patch.dict(sys.modules, modules):
            manager = PatchManager()
            manager.add_web_injection(
                owner="parquet",
                file_name="app.js",
                marker="/* PARQUET */",
                content="/* PARQUET */\nparquet();",
            )
            manager.add_web_injection(
                owner="character",
                file_name="app.js",
                marker="/* CHARACTER */",
                content="/* CHARACTER */\ncharacter();",
            )
            manager.add_web_injection(
                owner="parquet",
                file_name="app.js",
                marker="/* PARQUET */",
                content="/* PARQUET */\nparquet();",
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                app_js = Path(temp_dir) / "app.js"
                app_js.write_text("host();", encoding="utf-8")

                response = web_routes._web_file(app_js, "text/javascript")
                self.assertEqual(response.content.count("/* PARQUET */"), 1)
                self.assertEqual(response.content.count("/* CHARACTER */"), 1)

                manager.restore_owner("character")
                response = web_routes._web_file(app_js, "text/javascript")
                self.assertEqual(response.content.count("/* PARQUET */"), 1)
                self.assertNotIn("/* CHARACTER */", response.content)

            manager.restore_all()
            self.assertIs(web_routes._web_file, web_file)
            self.assertEqual(original_calls, [])


if __name__ == "__main__":
    unittest.main()
