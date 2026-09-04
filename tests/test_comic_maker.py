from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from naia_exten.features.comic_maker.feature import ComicMakerFeature
from naia_exten.features.comic_maker.models import validate_comic_plan
from naia_exten.features.comic_maker.renderer import compose_page, font_pixels, panel_generation_size


def comic_plan():
    return {
        "schema_version": 1,
        "id": 7,
        "title": "선술집의 밤",
        "preset_id": "comic_default",
        "width": 704,
        "height": 1280,
        "page_count": 1,
        "male_count": 1,
        "female_count": 1,
        "locale": "ko",
        "text_mode": "overlay",
        "global_prompt": "comic, sequential art",
        "character_prompts": {
            "male1": "adult man, black hair",
            "female1": "adult woman, auburn hair",
        },
        "pages": [
            {
                "page_number": 1,
                "location": "old tavern, indoors, night",
                "base_prompt": "dim lantern light",
                "negative_prompt": "lowres",
                "panels": [
                    {
                        "id": "p1",
                        "order": 1,
                        "rect": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.5},
                        "location": "at a wooden table",
                        "shot": "wide shot",
                        "action": "the characters sit together",
                        "prompt": "bottles on table",
                        "character_ids": ["male1", "female1"],
                    },
                    {
                        "id": "p2",
                        "order": 2,
                        "rect": {"x": 0.0, "y": 0.5, "w": 1.0, "h": 0.5},
                        "location": "near the tavern door",
                        "shot": "close-up",
                        "action": "the woman looks surprised",
                        "prompt": "dramatic lighting",
                        "character_ids": ["female1"],
                    },
                ],
                "bubbles": [
                    {
                        "id": "b1",
                        "panel_id": "p1",
                        "speaker": "female1",
                        "text": "이 술은 너무 독해요.",
                        "rect": {"x": 0.08, "y": 0.07, "w": 0.30, "h": 0.10},
                        "tail": {"x": 0.38, "y": 0.19},
                        "style": "round",
                        "font_scale": 0.035,
                        "rotation": 0,
                    }
                ],
                "sound_effects": [
                    {
                        "id": "s1",
                        "panel_id": "p2",
                        "text": "쾅!",
                        "anchor": {"x": 0.80, "y": 0.70},
                        "max_width": 0.22,
                        "font_scale": 0.07,
                        "rotation": -12,
                        "style": "jagged",
                        "color": "#d94b64",
                    }
                ],
            }
        ],
    }


class _FakeClient:
    def __init__(self, plan):
        self.plan = copy.deepcopy(plan)
        self.queries = []
        self.generate_queries = []
        self.used = []

    def presets(self):
        return [{"id": "comic_default"}]

    def random_plan(self, **query):
        self.queries.append(query)
        return copy.deepcopy(self.plan)

    def generate_plan(self, payload, progress=None):
        self.generate_queries.append(copy.deepcopy(payload))
        if progress is not None:
            progress("Story 단계 완료")
            progress("ComicPlan 단계 완료")
        return copy.deepcopy(self.plan)

    def mark_used(self, comic_id):
        self.used.append(comic_id)


class _FakeContext:
    def __init__(self, root: Path):
        self.ext_dir = root
        self.root = root
        self.settings = {
            "feature__comic_maker__enabled": True,
            "feature__comic_maker__preset": "comic_default",
            "feature__comic_maker__width": 832,
            "feature__comic_maker__height": 1216,
            "feature__comic_maker__page_count": 1,
            "feature__comic_maker__male_count": 1,
            "feature__comic_maker__female_count": 1,
            "feature__comic_maker__locale": "ko",
            "feature__comic_maker__text_mode": "overlay",
            "feature__comic_maker__include_used": False,
        }
        self.enqueued = []
        self.cancelled = []
        self.results = {}
        self.toasts = []
        self.logs = []
        self.queue_starts = 0
        self.current_params = {
            "input": "A quiet tavern scene",
            "pre_prompt": "masterpiece, clean lineart",
            "post_prompt": "high detail",
            "negative_prompt": "bad anatomy",
            "steps": 41,
            "cfg_scale": 7.2,
            "sampler": "k_euler_ancestral",
            "scheduler": "karras",
        }
        self.character_snapshot = {
            "characters": ["1boy, short black hair", "1girl, long auburn hair"],
            "uc": ["", ""],
            "character_positions": [{"x": 0.3, "y": 0.5}, {"x": 0.7, "y": 0.5}],
        }

    def subscribe(self, *_args, **_kwargs):
        return None

    def load_settings(self, defaults):
        return {**defaults, **self.settings}

    def get_current_request(self):
        return {"ok": True, "api_mode": "NAI", "prompt_run_id": "",
                "params": copy.deepcopy(self.current_params)}

    def resolve_nai_characters(self):
        return copy.deepcopy(self.character_snapshot)

    def enqueue_generation(self, **kwargs):
        request_id = f"req-{len(self.enqueued) + 1}"
        self.enqueued.append((request_id, kwargs))
        return {"ok": True, "request_id": request_id, "message": ""}

    def cancel_generation(self, request_id):
        self.cancelled.append(request_id)
        return {"ok": True, "skip_scheduled": False, "message": ""}

    def start_generation_queue(self):
        self.queue_starts += 1
        return {"ok": True, "message": ""}

    def get_result_image(self, request_id):
        image = self.results.get(request_id)
        if image is None:
            return {"ok": False, "message": "missing"}
        return {"ok": True, "image": image.copy(), "file_path": "", "message": ""}

    def get_save_directory(self):
        return str(self.root)

    def show_toast(self, message, level="info"):
        self.toasts.append((message, level))

    def log(self, message):
        self.logs.append(message)


class ComicMakerTests(unittest.TestCase):
    def test_panel_exposes_make_action_without_feature_activation(self):
        feature = ComicMakerFeature()
        fields = feature.panel_fields()
        make = next(field for field in fields if field["key"] == "make")
        self.assertNotIn("visible_when", make)
        self.assertFalse(feature.panel_toggle_visible)
        large = next(field for field in fields if field["key"] == "make_ja")
        self.assertEqual(large["label"], "큰 화면으로 생성")
        self.assertNotIn("일본어", large["help"])
        self.assertNotIn("visible_when", large)
        saved = next(field for field in fields if field["key"] == "make_saved")
        self.assertEqual(
            [field["key"] for field in fields],
            ["make", "make_ja", "make_saved"],
        )
        self.assertNotIn("visible_when", saved)

    def test_live_plan_logs_are_bounded_trimmed_and_deduplicated(self):
        feature = ComicMakerFeature()
        feature._planning = True
        feature._refresh_panel = lambda: None
        for index in range(20):
            feature._on_plan_progress(f"step {index}")
        feature._on_plan_progress("step 19")
        feature._on_plan_progress("x" * 200)

        self.assertEqual(len(feature._planning_logs), 16)
        self.assertLessEqual(max(map(len, feature._planning_logs)), 180)
        self.assertEqual(feature._planning_logs[-1], "x" * 180)
        self.assertEqual(feature._planning_logs.count("step 19"), 1)

    def test_planning_placeholder_shows_streamed_progress(self):
        feature = ComicMakerFeature()
        feature._refresh_panel = lambda: None
        with feature._run_lock:
            feature._planning = True
        feature._on_plan_progress("Story 단계 완료")
        summary = next(field for field in feature.panel_fields() if field["key"] == "summary")
        self.assertIn("Story 단계 완료", summary["placeholder"])

    def test_completed_run_clears_active_state_and_keeps_recent_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = _FakeContext(Path(temp_dir))
            feature = ComicMakerFeature()
            feature._attach(SimpleNamespace(ctx=ctx, refresh_panel=lambda: None))
            feature._client = _FakeClient(comic_plan())
            feature._prepare(auto_generate=False)
            feature._start_pending()
            ctx.results["req-1"] = Image.new("RGB", (832, 1216), "red")
            feature.on_generation_result({"request_id": "req-1"})

            self.assertIsNone(feature._active_run)
            fields = feature.panel_fields()
            self.assertEqual(
                [field["key"] for field in fields],
                ["summary", "make", "make_ja", "make_saved"],
            )
            self.assertIn("저장 완료", fields[0]["placeholder"])

    def test_validates_coordinates_and_references(self):
        plan = validate_comic_plan(comic_plan())
        self.assertEqual(plan["pages"][0]["panels"][0]["id"], "p1")

        broken = comic_plan()
        broken["pages"][0]["bubbles"][0]["panel_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "존재하지 않는 panel_id"):
            validate_comic_plan(broken)

        broken = comic_plan()
        broken["pages"][0]["panels"][0]["rect"]["w"] = 1.1
        with self.assertRaisesRegex(ValueError, "페이지 경계"):
            validate_comic_plan(broken)

        broken = comic_plan()
        broken["width"] = 704.5
        with self.assertRaisesRegex(ValueError, "정수"):
            validate_comic_plan(broken)

        oversized = comic_plan()
        oversized["pages"][0]["sound_effects"][0]["font_scale"] = 1.1
        self.assertEqual(
            validate_comic_plan(oversized)["pages"][0]["sound_effects"][0]["font_scale"],
            1.1,
        )

    def test_font_scale_accepts_multiplier_and_preserves_legacy_normalized_values(self):
        self.assertEqual(font_pixels(1.0, 704, base_ratio=0.035), 25)
        self.assertEqual(font_pixels(1.1, 704, base_ratio=0.07), 54)
        self.assertEqual(font_pixels(0.035, 704, base_ratio=0.035), 25)

    def test_gender_detection_is_strict_per_active_slot(self):
        self.assertEqual(ComicMakerFeature._gender("1girl, red hair"), "female")
        self.assertEqual(ComicMakerFeature._gender("1boy, black hair"), "male")
        with self.assertRaises(ValueError):
            ComicMakerFeature._gender("red hair")
        with self.assertRaises(ValueError):
            ComicMakerFeature._gender("2girls")

    def test_explicit_spatial_prompts_repeat_fixed_identity_at_server_centers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = _FakeContext(Path(temp_dir))
            plan = comic_plan()
            plan["pages"][0]["spatial_prompts"] = [{
                "character_id": "female1",
                "prompt": "surprised, looking left",
                "centers": [{"x": 0.2, "y": 0.3}, {"x": 0.8, "y": 0.7}],
            }]
            plan["pages"][0]["bubbles"] = []
            plan["pages"][0]["sound_effects"] = []
            feature = ComicMakerFeature()
            feature._attach(SimpleNamespace(ctx=ctx, refresh_panel=lambda: None))
            feature._client = _FakeClient(plan)

            feature._prepare(auto_generate=False)
            feature._start_pending()

            overrides = ctx.enqueued[0][1]["overrides"]
            self.assertEqual(overrides["character_positions"], [
                {"x": 0.2, "y": 0.3}, {"x": 0.8, "y": 0.7}
            ])
            self.assertEqual(len(overrides["characters"]), 2)
            self.assertTrue(all("1girl, long auburn hair" in value for value in overrides["characters"]))

    def test_panel_size_and_page_composition_keep_target_resolution(self):
        plan = validate_comic_plan(comic_plan())
        page = plan["pages"][0]
        self.assertEqual(panel_generation_size(page["panels"][0]["rect"], 704, 1280), (704, 640))
        panel_images = {
            "p1": Image.new("RGB", (704, 640), "red"),
            "p2": Image.new("RGB", (704, 640), "blue"),
        }
        image = compose_page(704, 1280, page, panel_images, text_mode="overlay")
        self.assertEqual(image.size, (704, 1280))
        self.assertNotEqual(image.getpixel((100, 100)), image.getpixel((100, 1000)))

    def test_run_fetches_by_cast_only_and_generates_one_full_image_per_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = _FakeContext(Path(temp_dir))
            client = _FakeClient(comic_plan())
            feature = ComicMakerFeature()
            feature._attach(SimpleNamespace(ctx=ctx, refresh_panel=lambda: None))
            feature._client = client

            feature._prepare(auto_generate=False)
            self.assertEqual(set(client.queries[0]), {"male_count", "female_count", "mark_used"})
            self.assertEqual((client.queries[0]["male_count"], client.queries[0]["female_count"]), (1, 1))
            confirmation = feature.panel_fields()
            self.assertEqual(
                [field["key"] for field in confirmation], ["summary", "confirm", "cancel"]
            )
            self.assertEqual(confirmation[0]["section"], "Comic Maker")
            self.assertIn("1페이지", confirmation[0]["placeholder"])
            self.assertIn("832 × 1216", confirmation[0]["placeholder"])

            feature._start_pending()
            self.assertEqual(len(ctx.enqueued), 1)
            self.assertEqual(ctx.queue_starts, 1)
            self.assertEqual(
                [field["key"] for field in feature.panel_fields()],
                ["summary", "reset"],
            )
            self.assertFalse(client.queries[0]["mark_used"])
            first_overrides = ctx.enqueued[0][1]["overrides"]
            self.assertEqual((first_overrides["width"], first_overrides["height"]), (832, 1216))
            self.assertEqual(first_overrides["steps"], 41)
            self.assertEqual(first_overrides["cfg_scale"], 7.2)
            self.assertEqual(first_overrides["sampler"], "k_euler_ancestral")
            self.assertIn("masterpiece, clean lineart", ctx.enqueued[0][1]["prompt"])
            self.assertTrue(ctx.enqueued[0][1]["prompt"].endswith("high detail"))
            self.assertIn("bad anatomy", ctx.enqueued[0][1]["negative_prompt"])
            self.assertEqual(len(first_overrides["characters"]), 4)
            self.assertIn("1boy, short black hair", first_overrides["characters"][0])
            self.assertIn("1girl, long auburn hair", first_overrides["characters"][1])
            self.assertIn("speech bubble next to girl,text: 이 술은 너무 독해요.",
                          first_overrides["characters"][1])
            self.assertIn("1girl, long auburn hair", first_overrides["characters"][2])
            self.assertEqual(first_overrides["characters"][3], "sound effects,text: 쾅!")
            self.assertNotIn("dialogue letters", ctx.enqueued[0][1]["negative_prompt"])
            self.assertNotIn("이 술은 너무 독해요.", ctx.enqueued[0][1]["prompt"])

            ctx.results["req-1"] = Image.new("RGB", (704, 1280), "red")
            feature.on_generation_result({"request_id": "req-1"})

            self.assertEqual(client.used, [7])
            pages = list(Path(temp_dir).glob("comic_maker/*/page_001.png"))
            self.assertEqual(len(pages), 1)
            with Image.open(pages[0]) as saved:
                self.assertEqual(saved.size, (832, 1216))

    def test_uses_current_naia_resolution_for_plan_and_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = _FakeContext(Path(temp_dir))
            ctx.current_params.update({
                "width": 640,
                "height": 960,
                "resolution": "832 x 1216",
            })
            client = _FakeClient(comic_plan())
            feature = ComicMakerFeature()
            feature._attach(SimpleNamespace(ctx=ctx, refresh_panel=lambda: None))
            feature._client = client

            feature._prepare(auto_generate=True)

            self.assertEqual(
                (client.generate_queries[0]["width"], client.generate_queries[0]["height"]),
                (640, 960),
            )
            plan = feature._active_run.pending.plan
            self.assertEqual((plan["width"], plan["height"]), (640, 960))
            overrides = ctx.enqueued[0][1]["overrides"]
            self.assertEqual((overrides["width"], overrides["height"]), (640, 960))
            self.assertEqual(overrides["resolution"], "640 x 960")

    def test_reads_legacy_naia_resolution_string(self):
        self.assertEqual(
            ComicMakerFeature._current_resolution({"params": {"resolution": "704 × 1280"}}),
            (704, 1280),
        )

    def test_confirmation_panel_cancel_returns_to_make_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = _FakeContext(Path(temp_dir))
            refreshes = []
            feature = ComicMakerFeature()
            feature._attach(SimpleNamespace(ctx=ctx, refresh_panel=lambda: refreshes.append(True)))
            feature._client = _FakeClient(comic_plan())

            feature._prepare(auto_generate=False)
            feature.handle_action(feature.key("cancel"))

            self.assertIsNone(feature._pending)
            self.assertEqual(
                [field["key"] for field in feature.panel_fields()],
                ["make", "make_ja", "make_saved"],
            )
            self.assertEqual(ctx.enqueued, [])
            self.assertEqual(ctx.queue_starts, 0)
            self.assertEqual(len(refreshes), 2)

    def test_panel_style_keeps_enabled_help_inline_and_checkbox_at_right(self):
        style = ComicMakerFeature._PANEL_STYLE

        self.assertIn('input[type="checkbox"][data-field$="__enabled"]', style)
        self.assertIn("display: inline-flex", style)
        self.assertIn("white-space: nowrap", style)
        self.assertIn("justify-self: end", style)
        self.assertIn("margin: 0 6px 0 0", style)

    def test_old_host_resumes_queue_through_background_http_compatibility(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = _FakeContext(Path(temp_dir))
            ctx.start_generation_queue = None
            ctx.current_params["web_session_port"] = 8123
            resumed = []
            completed = threading.Event()
            feature = ComicMakerFeature()
            feature._attach(SimpleNamespace(ctx=ctx, refresh_panel=lambda: None))
            feature._client = _FakeClient(comic_plan())

            def resume(run, port):
                resumed.append((run, port))
                completed.set()

            feature._resume_generation_queue = resume
            feature._prepare(auto_generate=False)
            feature._start_pending()

            self.assertTrue(completed.wait(1))
            self.assertEqual(resumed[0][1], 8123)
            self.assertEqual(len(ctx.enqueued), 1)
            self.assertEqual(ctx.queue_starts, 0)

    def test_multiple_pages_reuse_the_same_fixed_character_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = _FakeContext(Path(temp_dir))
            multi = comic_plan()
            multi["page_count"] = 2
            page2 = copy.deepcopy(multi["pages"][0])
            page2["page_number"] = 2
            multi["pages"].append(page2)
            feature = ComicMakerFeature()
            feature._attach(SimpleNamespace(ctx=ctx, refresh_panel=lambda: None))
            feature._client = _FakeClient(multi)

            feature._prepare(auto_generate=False)
            feature._start_pending()

            self.assertEqual(len(ctx.enqueued), 2)
            first = ctx.enqueued[0][1]["overrides"]["characters"]
            second = ctx.enqueued[1][1]["overrides"]["characters"]
            self.assertEqual(first, second)

    def test_single_panel_mode_splits_panels_and_rebases_spatial_centers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = _FakeContext(Path(temp_dir))
            ctx.current_params["pre_prompt"] = (
                "1boy, 1girl, 2girls, 6+boys, other, 3others, masterpiece, clean lineart"
            )
            plan = comic_plan()
            plan["pages"][0]["spatial_prompts"] = [
                {
                    "character_id": "male1",
                    "panel_id": "p1",
                    "prompt": "sitting at table",
                    "centers": [{"x": 0.2, "y": 0.2}],
                },
                {
                    "character_id": "female1",
                    "panel_id": "p2",
                    "prompt": "looking at door",
                    "centers": [{"x": 0.8, "y": 0.9}],
                },
            ]
            plan["pages"][0]["bubbles"][0]["speaker"] = "male1"
            plan["pages"][0]["page_prompt"] = "comic, 2koma, multiple views"
            client = _FakeClient(plan)
            original = copy.deepcopy(client.plan)
            feature = ComicMakerFeature()
            feature._attach(SimpleNamespace(ctx=ctx, refresh_panel=lambda: None))
            feature._client = client

            feature._prepare(single_panel_mode=True)

            self.assertEqual(client.generate_queries[0]["locale"], "ko")
            self.assertEqual(client.plan, original)
            self.assertEqual(len(ctx.enqueued), 2)
            self.assertEqual(ctx.queue_starts, 1)
            self.assertEqual(
                [request[1]["overrides"]["character_positions"] for request in ctx.enqueued],
                [
                    [{"x": 0.2, "y": 0.4}],
                    [{"x": 0.8, "y": 0.8}, {"x": 0.8, "y": 0.3999999999999999}],
                ],
            )
            self.assertIn("the characters sit together", ctx.enqueued[0][1]["prompt"])
            self.assertIn("the woman looks surprised", ctx.enqueued[1][1]["prompt"])
            self.assertTrue(ctx.enqueued[0][1]["prompt"].startswith("1boy, masterpiece"))
            self.assertTrue(ctx.enqueued[1][1]["prompt"].startswith("1girl, masterpiece"))
            for request_id, request in ctx.enqueued:
                self.assertNotIn("2girls", request["prompt"])
                self.assertNotIn("6+boys", request["prompt"])
                self.assertNotIn("3others", request["prompt"])
                self.assertNotIn(", other,", request["prompt"])
                self.assertIn("single full-canvas", request["prompt"])
                self.assertNotIn("2koma", request["prompt"])
                self.assertNotIn("multiple views", request["prompt"])
                self.assertNotIn("multiple panels", request["prompt"])
            self.assertTrue(
                all(
                    (request[1]["overrides"]["width"], request[1]["overrides"]["height"])
                    == (832, 1216)
                    for request in ctx.enqueued
                )
            )
            execution = feature._active_run.pending.plan
            self.assertEqual(execution["page_count"], 2)
            self.assertEqual([page["page_number"] for page in execution["pages"]], [1, 2])
            self.assertEqual(
                [page["panels"][0]["id"] for page in execution["pages"]], ["p1", "p2"]
            )
            self.assertEqual(
                [page["panels"][0]["rect"] for page in execution["pages"]],
                [{"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}] * 2,
            )

    def test_failed_panel_does_not_mark_plan_used(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ctx = _FakeContext(Path(temp_dir))
            client = _FakeClient(comic_plan())
            feature = ComicMakerFeature()
            feature._attach(SimpleNamespace(ctx=ctx, refresh_panel=lambda: None))
            feature._client = client
            feature._prepare(auto_generate=False)
            feature._start_pending()

            feature.on_generation_result({"request_id": "req-1"})
            self.assertEqual(client.used, [])
            self.assertTrue(any("used 처리 안 함" in message for message, _ in ctx.toasts))


if __name__ == "__main__":
    unittest.main()
