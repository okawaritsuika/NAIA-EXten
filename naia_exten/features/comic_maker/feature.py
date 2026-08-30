from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from ..base_feature import BaseFeature
from .client import ComicPlanNotFound, ComicServerClient, ComicServerError
from .models import validate_comic_plan


@dataclass
class _PendingComic:
    plan: dict[str, Any]
    current: dict[str, Any]
    prompts: dict[str, str]
    ucs: dict[str, str]
    positions: dict[str, dict[str, float]]


@dataclass
class _ComicRun:
    pending: _PendingComic
    output_dir: Path
    requests: dict[str, int] = field(default_factory=dict)
    page_paths: dict[int, Path] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    enqueued_complete: bool = False


class ComicMakerFeature(BaseFeature):
    id = "comic_maker"
    name = "Comic Maker"
    description = "활성 캐릭터를 고정해 PromptServer 만화를 페이지 순서대로 생성합니다."
    category = "Comic Maker"
    order = 40
    default_enabled = True
    panel_toggle_visible = False

    SERVER_BASE = "http://127.0.0.1:8765"
    # NAI maximum portrait canvas used by Comic Maker.  ComicPlan geometry is
    # normalized (0..1), so server-side preset dimensions can be safely mapped
    # to this single output resolution for every page.
    COMIC_WIDTH = 832
    COMIC_HEIGHT = 1216
    _NEVER = "__comic_maker_internal_never__"
    _INHERITED_PARAM_KEYS = (
        "model", "sampler", "scheduler", "steps", "cfg_scale", "scale",
        "cfg_rescale", "SMEA", "DYN", "VAR+", "DECRISP", "seed_fixed",
        "enable_hr", "hr_scale", "hr_upscaler", "denoising_strength",
        "hires_steps", "hr_cfg", "rescale_cfg",
    )

    def __init__(self):
        super().__init__()
        self._client = ComicServerClient(self.SERVER_BASE)
        self._run_lock = threading.RLock()
        self._pending: _PendingComic | None = None
        self._active_run: _ComicRun | None = None

    def register(self) -> None:
        self.ctx.subscribe("generation_result_available", self.on_generation_result)

    def unregister(self) -> None:
        with self._run_lock:
            self._pending = None
            self._active_run = None

    def panel_fields(self) -> list[dict]:
        hidden = {"field": self._NEVER, "in": ["1"]}
        return [
            {"key": "make", "type": "action", "label": "만화 만들기",
             "help": "활성 캐릭터 수로 계획을 받아 확인 후 모든 페이지를 연속 생성합니다."},
            {"key": "confirm", "type": "action", "label": "확인", "visible_when": hidden},
            {"key": "cancel", "type": "action", "label": "취소", "visible_when": hidden},
        ]

    def handle_action(self, full_key: str) -> None:
        if full_key == self.key("make"):
            self._prepare()
        elif full_key == self.key("confirm"):
            self._start_pending()
        elif full_key == self.key("cancel"):
            with self._run_lock:
                self._pending = None

    def _toast(self, message: str, level: str = "info") -> None:
        try:
            self.ctx.show_toast(message, level)
        except Exception:
            self.ctx.log(message)

    @staticmethod
    def _gender(prompt: str) -> str:
        matches = re.findall(r"(?<![a-z])(\d*)\s*(girls?|boys?)(?![a-z])", prompt.lower())
        genders: set[str] = set()
        for count_text, word in matches:
            count = int(count_text) if count_text else 1
            if count != 1:
                raise ValueError("캐릭터 슬롯 하나에는 1girl 또는 1boy 한 명만 지정해야 합니다.")
            genders.add("female" if word.startswith("girl") else "male")
        if len(genders) != 1:
            raise ValueError("각 활성 캐릭터 프롬프트에 1girl 또는 1boy를 하나만 넣어주세요.")
        return next(iter(genders))

    def _character_snapshot(
        self,
    ) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, float]], int, int]:
        snapshot = self.ctx.resolve_nai_characters()
        if not snapshot or not snapshot.get("characters"):
            raise ValueError("활성화된 Character 프롬프트가 없습니다.")
        characters = [str(value) for value in snapshot.get("characters") or []]
        raw_ucs = [str(value) for value in snapshot.get("uc") or []]
        raw_positions = list(snapshot.get("character_positions") or [])
        grouped: dict[str, list[tuple[str, str, dict[str, float]]]] = {"male": [], "female": []}
        for index, prompt in enumerate(characters):
            gender = self._gender(prompt)
            uc = raw_ucs[index] if index < len(raw_ucs) else ""
            position = raw_positions[index] if index < len(raw_positions) else {"x": 0.5, "y": 0.5}
            grouped[gender].append((prompt, uc, position))
        prompts: dict[str, str] = {}
        ucs: dict[str, str] = {}
        positions: dict[str, dict[str, float]] = {}
        for gender in ("male", "female"):
            for index, (prompt, uc, position) in enumerate(grouped[gender], start=1):
                character_id = f"{gender}{index}"
                prompts[character_id] = prompt
                ucs[character_id] = uc
                positions[character_id] = position
        return prompts, ucs, positions, len(grouped["male"]), len(grouped["female"])

    def _prepare(self) -> None:
        with self._run_lock:
            if self._active_run is not None or self._pending is not None:
                self._toast("이미 준비 또는 생성 중인 만화가 있습니다.", "warning")
                return
        current = self.ctx.get_current_request()
        if not current.get("ok") or str(current.get("api_mode") or "").upper() != "NAI":
            self._toast("Comic Maker는 현재 NAI 모드에서만 사용할 수 있습니다.", "error")
            return
        try:
            prompts, ucs, positions, male_count, female_count = self._character_snapshot()
            plan = validate_comic_plan(self._client.random_plan(
                male_count=male_count, female_count=female_count, mark_used=False,
            ))
            if plan["male_count"] != male_count or plan["female_count"] != female_count:
                raise ValueError("PromptServer가 다른 캐릭터 인원수의 계획을 반환했습니다.")
        except (ComicPlanNotFound, ComicServerError, ValueError) as exc:
            self._toast(str(exc), "error")
            return
        # Keep page geometry and all generated files at one known resolution.
        # The server may return its preset's legacy dimensions (for example
        # 704x1216); those values are not used by the NAI request.
        plan["width"] = self.COMIC_WIDTH
        plan["height"] = self.COMIC_HEIGHT
        pending = _PendingComic(plan, current, prompts, ucs, positions)
        with self._run_lock:
            self._pending = pending
        try:
            shown = self.ctx.request_confirmation(
                f"{plan['page_count']}페이지 만화를 만들까요?\n"
                f"해상도: {plan['width']} × {plan['height']}\n"
                f"캐릭터: girl {female_count}명 · boy {male_count}명",
                title="Comic Maker",
                confirm_action=self.key("confirm"),
                cancel_action=self.key("cancel"),
                confirm_label="만들기",
            )
            if shown is False:
                raise RuntimeError("확장이 작동 OFF 상태입니다")
        except Exception as exc:
            with self._run_lock:
                self._pending = None
            self._toast(f"확인 창을 열지 못했습니다. NAIA를 재시작해주세요: {exc}", "error")

    def _start_pending(self) -> None:
        with self._run_lock:
            pending = self._pending
            self._pending = None
            if pending is None or self._active_run is not None:
                return
        run = _ComicRun(pending=pending, output_dir=self._create_output_dir(pending.plan))
        with self._run_lock:
            self._active_run = run
        try:
            (run.output_dir / "comic_plan.json").write_text(
                json.dumps(pending.plan, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (run.output_dir / "character_snapshot.json").write_text(
                json.dumps({"prompts": pending.prompts, "uc": pending.ucs}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for page in pending.plan["pages"]:
                self._enqueue_page(run, page)
            run.enqueued_complete = True
            started = self.ctx.start_generation_queue()
            if not started.get("ok"):
                raise RuntimeError(started.get("message") or "생성 큐 시작 실패")
        except Exception as exc:
            self._abort_run(run, f"Comic Maker 시작 실패: {exc}")
            return
        with self._run_lock:
            already_done = self._active_run is run and not run.requests
            if already_done:
                self._active_run = None
        if already_done:
            self._finish_run(run)
            return
        self._toast(f"{pending.plan['page_count']}페이지 만화 생성을 시작했습니다.", "success")

    @staticmethod
    def _join(parts: list[Any]) -> str:
        return ", ".join(str(value).strip(" ,") for value in parts if str(value or "").strip(" ,"))

    def _spatial_entries(self, pending: _PendingComic, page: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        explicit = page.get("spatial_prompts") or []
        if explicit:
            for item in explicit:
                for center in item.get("centers") or []:
                    entries.append({"character_id": str(item.get("character_id") or ""),
                                    "panel_id": str(item.get("panel_id") or ""),
                                    "prompt": str(item.get("prompt") or ""), "center": center})
        else:
            for panel in page.get("panels") or []:
                rect = panel["rect"]
                center = {"x": rect["x"] + rect["w"] / 2, "y": rect["y"] + rect["h"] / 2}
                direction = self._join([panel.get("location"), panel.get("shot"),
                                        panel.get("action"), panel.get("prompt")])
                for character_id in panel.get("character_ids") or []:
                    entries.append({"character_id": character_id, "panel_id": str(panel.get("id") or ""),
                                    "prompt": direction, "center": center})
        if not entries:
            for character_id in pending.prompts:
                entries.append({"character_id": character_id, "panel_id": "", "prompt": "",
                                "center": pending.positions.get(character_id, {"x": 0.5, "y": 0.5})})

        for bubble in page.get("bubbles") or []:
            text = str(bubble.get("text") or "").strip()
            if not text:
                continue
            speaker = str(bubble.get("speaker") or "")
            panel_id = str(bubble.get("panel_id") or "")
            subject = "girl" if speaker.startswith("female") else "boy" if speaker.startswith("male") else ""
            bubble_prompt = (f"speech bubble next to {subject},text: {text}"
                             if subject else f"only speech bubble,text: {text}")
            target = next((item for item in entries
                           if item["character_id"] == speaker and item["panel_id"] == panel_id), None)
            if target is None and speaker:
                matches = [item for item in entries if item["character_id"] == speaker]
                target = matches[0] if len(matches) == 1 else None
            if target is not None:
                target["prompt"] = self._join([target["prompt"], bubble_prompt])
                continue
            rect = bubble["rect"]
            entries.append({
                "character_id": speaker if speaker in pending.prompts else "",
                "panel_id": panel_id,
                "prompt": bubble_prompt,
                "center": {"x": rect["x"] + rect["w"] / 2, "y": rect["y"] + rect["h"] / 2},
            })

        for effect in page.get("sound_effects") or []:
            text = str(effect.get("text") or "").strip()
            if text:
                entries.append({"character_id": "", "panel_id": str(effect.get("panel_id") or ""),
                                "prompt": f"sound effects,text: {text}", "center": effect["anchor"]})
        return entries

    def _enqueue_page(self, run: _ComicRun, page: dict[str, Any]) -> None:
        pending, plan = run.pending, run.pending.plan
        current_params = pending.current.get("params")
        if not isinstance(current_params, dict):
            current_params = {}
        prefix_prompt = str(
            current_params.get("pre_prompt")
            or current_params.get("prefix_prompt")
            or ""
        ).strip()
        if not prefix_prompt:
            # Older runtimes do not expose PE's separate pre_prompt field. In
            # that case retain the visible prompt box as the user's fixed base
            # instead of silently dropping manually entered prefix text.
            prefix_prompt = str(current_params.get("input") or "").strip()
        postfix_prompt = str(current_params.get("post_prompt") or "").strip()
        panel_directions = [
            f"panel {panel.get('order')}: " + self._join([
                panel.get("location"), panel.get("shot"), panel.get("action"), panel.get("prompt")
            ]) for panel in page.get("panels") or []
        ]
        prompt = self._join([prefix_prompt, plan.get("global_prompt"),
                             "full comic page, multiple panels, sequential art, consistent characters",
                             page.get("location"), page.get("base_prompt"), page.get("page_prompt"),
                             *panel_directions, postfix_prompt])
        negative = self._join([current_params.get("negative_prompt"), page.get("negative_prompt"), "watermark"])

        characters, ucs, positions, character_ids = [], [], [], []
        # Old stored plans predate the spatial/outfit-only contract and often
        # contain a second, conflicting physical identity here. Preserve fixed
        # active characters by applying server outfits only to new spatial plans.
        has_spatial_contract = any(p.get("spatial_prompts") for p in plan.get("pages") or [])
        outfits = (plan.get("character_prompts") or {}) if has_spatial_contract else {}
        for item in self._spatial_entries(pending, page):
            character_id = item["character_id"]
            characters.append(self._join([
                pending.prompts.get(character_id), outfits.get(character_id), item["prompt"]
            ]))
            ucs.append(pending.ucs.get(character_id, ""))
            positions.append(item["center"])
            character_ids.append(character_id or f"spatial{len(character_ids) + 1}")
        inherited = {}
        for key in self._INHERITED_PARAM_KEYS:
            value = current_params.get(key)
            if value in (None, ""):
                continue
            # An unlocked seed should be rerolled per page; only carry a seed
            # when the user explicitly enabled seed locking.
            if key == "seed_fixed" and not bool(value):
                continue
            if key == "seed" and not bool(current_params.get("seed_fixed")):
                continue
            inherited[key] = value
        if bool(current_params.get("seed_fixed")) and current_params.get("seed") not in (None, ""):
            inherited["seed"] = current_params["seed"]
        inherited.update({
            "width": plan["width"], "height": plan["height"],
            "resolution": f"{plan['width']} x {plan['height']}",
            "characters": characters, "uc": ucs, "character_positions": positions,
            "character_ids": character_ids, "_skip_character_late_binding": True,
        })
        result = self.ctx.enqueue_generation(
            prompt=prompt, negative_prompt=negative, api_mode="NAI",
            prompt_run_id=pending.current.get("prompt_run_id"),
            overrides=inherited,
        )
        if not result.get("ok") or not result.get("request_id"):
            raise RuntimeError(result.get("message") or f"page {page['page_number']} 큐 추가 실패")
        run.requests[str(result["request_id"])] = page["page_number"]

    def on_generation_result(self, info: Any) -> None:
        if not isinstance(info, dict):
            return
        request_id = str(info.get("request_id") or "")
        with self._run_lock:
            run = self._active_run
            page_number = run.requests.pop(request_id, None) if run else None
        if run is None or page_number is None:
            return
        fetched = self.ctx.get_result_image(request_id)
        if not fetched.get("ok"):
            run.failures.append(f"page {page_number}: {fetched.get('message')}")
        else:
            try:
                plan = run.pending.plan
                image = ImageOps.fit(fetched["image"].convert("RGB"),
                                     (plan["width"], plan["height"]), method=Image.Resampling.LANCZOS)
                path = run.output_dir / f"page_{page_number:03d}.png"
                image.save(path, format="PNG")
                run.page_paths[page_number] = path
            except Exception as exc:
                run.failures.append(f"page {page_number} 저장: {exc}")
        with self._run_lock:
            done = self._active_run is run and run.enqueued_complete and not run.requests
            if done:
                self._active_run = None
        if done:
            self._finish_run(run)

    def _finish_run(self, run: _ComicRun) -> None:
        plan = run.pending.plan
        if run.failures or len(run.page_paths) != plan["page_count"]:
            detail = run.failures[0] if run.failures else "완성되지 않은 페이지가 있습니다."
            self._toast(f"만화 생성 실패 — used 처리 안 함: {detail}", "error")
            return
        try:
            self._client.mark_used(plan["id"])
        except ComicServerError as exc:
            self._toast(f"페이지 생성 완료, used 처리 실패: {exc}", "warning")
            return
        self._toast(f"만화 {plan['page_count']}페이지 저장 완료: {run.output_dir}", "success")

    def _abort_run(self, run: _ComicRun, message: str) -> None:
        with self._run_lock:
            if self._active_run is run:
                self._active_run = None
        for request_id in list(run.requests):
            try:
                self.ctx.cancel_generation(request_id)
            except Exception:
                pass
        self._toast(message, "error")

    def _create_output_dir(self, plan: dict[str, Any]) -> Path:
        root_text = str(self.ctx.get_save_directory() or "").strip()
        root = Path(root_text) if root_text else Path(self.ctx.ext_dir) / "comic_output"
        safe_title = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", plan["title"]).strip("._") or "comic"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output = root / "comic_maker" / f"{stamp}_{plan['id']}_{safe_title[:60]}"
        output.mkdir(parents=True, exist_ok=False)
        return output
