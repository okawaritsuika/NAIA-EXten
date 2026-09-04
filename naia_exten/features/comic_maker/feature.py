from __future__ import annotations

import json
import copy
import re
import threading
import time
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
    single_panel_mode: bool = False


@dataclass
class _ComicRun:
    pending: _PendingComic
    output_dir: Path
    requests: dict[str, int] = field(default_factory=dict)
    page_paths: dict[int, Path] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    enqueued_complete: bool = False
    next_page_index: int = 0


class ComicMakerFeature(BaseFeature):
    id = "comic_maker"
    name = "Comic Maker"
    description = "활성 캐릭터를 고정해 PromptServer 만화를 페이지 순서대로 생성합니다."
    category = "Comic Maker"
    order = 40
    default_enabled = True
    panel_toggle_visible = False
    _LOG_LIMIT = 16
    _LOG_LINE_LIMIT = 180
    _LOG_REFRESH_INTERVAL = 0.2
    _PERSON_COUNT_TAG_RE = re.compile(
        r"^(?:\d+\+?)?(?:boys?|girls?|others?)$", re.IGNORECASE
    )

    SERVER_BASE = "http://127.0.0.1:8765"
    _PANEL_STYLE_MARKER = "/* NAIA_EXTEN_COMIC_MAKER_CONFIRM_V1 */"
    _PANEL_STYLE = _PANEL_STYLE_MARKER + r"""
.ext-quick-popup .ext-field:has(> textarea[data-field="feature__comic_maker__summary"]) {
  display: block;
  min-height: 0;
  margin: 2px 0 4px;
  padding: 9px 11px;
  border: 1px solid var(--border-dim);
  border-radius: 8px;
  background: var(--bg-elevated);
}
.ext-quick-popup .ext-field:has(> textarea[data-field="feature__comic_maker__summary"]) > label {
  display: block;
  margin-bottom: 5px;
  color: var(--accent-light);
  font-weight: 750;
  white-space: nowrap;
}
.ext-quick-popup textarea[data-field="feature__comic_maker__summary"] {
  display: block;
  width: 100%;
  min-height: 40px;
  padding: 0;
  border: 0;
  outline: 0;
  resize: none;
  pointer-events: none;
  background: transparent;
  color: var(--text-primary);
  line-height: 1.55;
}
.ext-quick-popup textarea[data-field="feature__comic_maker__summary"]::placeholder {
  color: var(--text-primary);
  opacity: 1;
}
.ext-quick-popup .ext-field:has(> input[type="checkbox"][data-field$="__enabled"]) {
  grid-template-columns: minmax(0, 1fr) auto;
  column-gap: 12px;
}
.ext-quick-popup .ext-field:has(> input[type="checkbox"][data-field$="__enabled"]) > label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  width: max-content;
  max-width: 100%;
  white-space: nowrap;
}
.ext-quick-popup .ext-field:has(> input[type="checkbox"][data-field$="__enabled"]) > label .ext-help-mark {
  display: inline-block;
  flex: 0 0 auto;
  margin-left: 2px;
  white-space: nowrap;
}
.ext-quick-popup .ext-field:has(> input[type="checkbox"][data-field$="__enabled"]) > input[type="checkbox"] {
  grid-column: 2;
  justify-self: end;
  margin: 0 6px 0 0;
}
"""
    # Compatibility fallback for older NAIA hosts that do not expose their
    # current canvas size through get_current_request().
    COMIC_WIDTH = 832
    COMIC_HEIGHT = 1216
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
        self._planning = False
        self._planning_logs: list[str] = []
        self._recent_logs: list[str] = []
        self._last_log_refresh = 0.0

    def register(self) -> None:
        self.ext.patches.add_web_injection(
            owner=self.id,
            file_name="style.css",
            marker=self._PANEL_STYLE_MARKER,
            content=self._PANEL_STYLE,
        )
        self.ctx.subscribe("generation_result_available", self.on_generation_result)

    def unregister(self) -> None:
        with self._run_lock:
            self._pending = None
            self._active_run = None
            self._planning = False
            self._planning_logs.clear()
            self._recent_logs.clear()

    def panel_fields(self) -> list[dict]:
        with self._run_lock:
            planning = bool(getattr(self, "_planning", False))
            pending = self._pending
            run = self._active_run
            planning_logs = list(self._planning_logs)
            recent_logs = list(self._recent_logs)

        if planning:
            return [
                {
                    "key": "summary",
                    "type": "text",
                    "label": "Comic Maker",
                    "default": "",
                    "placeholder": (
                        "NovelAI가 Story / ComicPlan을 만드는 중입니다...\n"
                        "완료되면 모든 페이지가 NAIA 큐에 자동 등록됩니다."
                        + ("\n" + "\n".join(planning_logs) if planning_logs else "")
                    ),
                    "multiline": True,
                    "section": self.category,
                },
                {
                    "key": "reset",
                    "type": "action",
                    "label": "작업 상태 초기화",
                    "help": "응답이 멈췄거나 작업을 버리고 다시 시작할 때 사용합니다.",
                    "section": self.category,
                },
            ]

        if run is not None:
            total = int(
                run.pending.plan.get("page_count")
                or len(run.pending.plan.get("pages") or [])
            )
            completed = len(run.page_paths) + len(run.failures)
            queued = len(run.requests)
            log_text = "\n".join(run.logs)
            return [
                {
                    "key": "summary",
                    "type": "text",
                    "label": "Comic Maker",
                    "default": "",
                    "placeholder": (
                        f"NAIA 만화 생성 중...\n"
                        f"완료 {completed}/{total} · 추적 중 {queued}"
                        + ("\n" + log_text if log_text else "")
                    ),
                    "multiline": True,
                    "section": self.category,
                    "help": (
                        "NAIA 큐를 중간에 닫거나 비운 뒤 이 상태가 남아 있으면 "
                        "아래 초기화 버튼을 누르면 즉시 다시 생성할 수 있습니다."
                    ),
                },
                {
                    "key": "reset",
                    "type": "action",
                    "label": "큐 종료됨 / 작업 상태 초기화",
                    "help": "남은 Comic Maker request 추적을 취소하고 새 작업을 가능하게 합니다.",
                    "section": self.category,
                },
            ]

        if pending is not None:
            plan = pending.plan
            summary = (
                f"{plan['page_count']}페이지 · {plan['width']} × {plan['height']}\n"
                f"캐릭터: girl {plan['female_count']}명 · boy {plan['male_count']}명"
            )
            return [
                {
                    "key": "summary",
                    "type": "text",
                    "label": "생성 정보",
                    "default": "",
                    "placeholder": summary,
                    "multiline": True,
                    "section": self.category,
                },
                {
                    "key": "confirm",
                    "type": "action",
                    "label": "✓ 이 설정으로 만들기",
                    "help": summary,
                    "section": self.category,
                },
                {
                    "key": "cancel",
                    "type": "action",
                    "label": "취소",
                    "section": self.category,
                },
            ]

        fields = [
            {
                "key": "make",
                "type": "action",
                "label": "만화 만들기 · NovelAI 자동",
                "help": "현재 NAIA 자동생성 프롬프트를 Story → ComicPlan으로 변환해 자동 생성합니다.",
            },
            {
                "key": "make_saved",
                "type": "action",
                "label": "저장 ComicPlan 랜덤",
                "help": "PromptServer에 저장된 기존 ComicPlan을 사용합니다.",
            },
        ]
        if recent_logs:
            fields.insert(0, {
                "key": "summary",
                "type": "text",
                "label": "최근 상태",
                "default": "",
                "placeholder": "\n".join(recent_logs),
                "multiline": True,
                "section": self.category,
            })
        return fields

    @classmethod
    def _log_line(cls, message: Any) -> str:
        return str(message or "").replace("\r", " ").replace("\n", " ").strip()[: cls._LOG_LINE_LIMIT]

    def _append_log(
        self, message: Any, *, run: _ComicRun | None = None, force_refresh: bool = False
    ) -> None:
        line = self._log_line(message)
        if not line:
            return
        with self._run_lock:
            target = run.logs if run is not None else self._planning_logs
            if target and target[-1] == line:
                return
            target.append(line)
            del target[:-self._LOG_LIMIT]
            now = time.monotonic()
            should_refresh = force_refresh or (
                now - self._last_log_refresh >= self._LOG_REFRESH_INTERVAL
            )
            if should_refresh:
                self._last_log_refresh = now
        if should_refresh:
            self._refresh_panel()

    def _on_plan_progress(self, message: str) -> None:
        with self._run_lock:
            if not self._planning:
                return
        self._append_log(message)

    def _store_recent_logs(self, logs: list[str]) -> None:
        with self._run_lock:
            self._recent_logs = list(logs[-self._LOG_LIMIT:])


    def handle_action(self, full_key: str) -> None:
        if full_key == self.key("reset"):
            self._reset_comic_state(
                "Comic Maker 작업 상태를 초기화했습니다. 새 만화를 다시 만들 수 있습니다."
            )
            return

        if full_key == self.key("make"):
            with self._run_lock:
                busy = (
                    bool(getattr(self, "_planning", False))
                    or self._pending is not None
                    or self._active_run is not None
                )
                if not busy:
                    self._planning = True
                    self._planning_logs.clear()
                    self._recent_logs.clear()

            if busy:
                self._toast(
                    "기존 Comic Maker 작업이 남아 있습니다. "
                    "'작업 상태 초기화' 후 다시 시도하세요.",
                    "warning",
                )
                return

            self._refresh_panel()
            self._toast(
                "NovelAI에서 Story와 ComicPlan을 만드는 중입니다...",
                "info",
            )
            threading.Thread(
                target=self._prepare,
                kwargs={"auto_generate": True},
                daemon=True,
                name="comic-maker-novelai-plan",
            ).start()
            return

        if full_key == self.key("make_saved"):
            self._prepare(auto_generate=False)
            return

        if full_key == self.key("confirm"):
            self._start_pending()
            return

        if full_key == self.key("cancel"):
            with self._run_lock:
                self._pending = None
            self._refresh_panel()


    def _reset_comic_state(self, message: str = "") -> None:
        with self._run_lock:
            run = self._active_run
            self._active_run = None
            self._pending = None
            self._planning = False
            self._planning_logs.clear()
            self._recent_logs.clear()

        if run is not None:
            for request_id in list(run.requests):
                try:
                    self.ctx.cancel_generation(request_id)
                except Exception:
                    pass
            run.requests.clear()

        self._refresh_panel()
        if message:
            self._toast(message, "info")


    def _toast(self, message: str, level: str = "info") -> None:
        try:
            self.ctx.show_toast(message, level)
        except Exception:
            self.ctx.log(message)

    def _refresh_panel(self) -> None:
        try:
            self.ext.refresh_panel()
        except Exception as exc:
            self.ctx.log(f"Comic Maker 패널 갱신 실패: {exc}")

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

    @classmethod
    def _current_resolution(cls, current: dict[str, Any]) -> tuple[int, int]:
        params = current.get("params")
        if not isinstance(params, dict):
            return cls.COMIC_WIDTH, cls.COMIC_HEIGHT

        raw_width = params.get("width")
        raw_height = params.get("height")
        if raw_width in (None, "") or raw_height in (None, ""):
            raw_resolution = params.get("resolution")
            match = re.fullmatch(
                r"\s*(\d+)\s*[x×]\s*(\d+)\s*",
                str(raw_resolution or ""),
                re.IGNORECASE,
            )
            if match:
                raw_width, raw_height = match.groups()

        if raw_width in (None, "") or raw_height in (None, ""):
            return cls.COMIC_WIDTH, cls.COMIC_HEIGHT
        try:
            width, height = int(raw_width), int(raw_height)
        except (TypeError, ValueError) as exc:
            raise ValueError("NAIA 해상도를 읽지 못했습니다.") from exc
        if not (256 <= width <= 4096 and 256 <= height <= 4096):
            raise ValueError("NAIA 해상도는 가로·세로 256~4096 범위여야 합니다.")
        return width, height

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

    @staticmethod
    def _unit(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @classmethod
    def _panel_local_point(
        cls, point: dict[str, Any], panel_rect: dict[str, float]
    ) -> dict[str, float]:
        return {
            "x": cls._unit((point["x"] - panel_rect["x"]) / panel_rect["w"]),
            "y": cls._unit((point["y"] - panel_rect["y"]) / panel_rect["h"]),
        }

    @classmethod
    def _panel_local_rect(
        cls, rect: dict[str, float], panel_rect: dict[str, float]
    ) -> dict[str, float]:
        left = cls._unit((rect["x"] - panel_rect["x"]) / panel_rect["w"])
        top = cls._unit((rect["y"] - panel_rect["y"]) / panel_rect["h"])
        right = cls._unit(
            (rect["x"] + rect["w"] - panel_rect["x"]) / panel_rect["w"]
        )
        bottom = cls._unit(
            (rect["y"] + rect["h"] - panel_rect["y"]) / panel_rect["h"]
        )
        # A bubble that lies on/over a panel edge still needs a valid positive
        # rectangle for the persisted execution plan.
        width = max(1e-6, right - left)
        height = max(1e-6, bottom - top)
        return {"x": left, "y": top, "w": width, "h": height}

    def _make_large_screen_plan(
        self, source_plan: dict[str, Any], width: int, height: int
    ) -> dict[str, Any]:
        """Split every source panel into one independent full-canvas page."""
        execution_pages: list[dict[str, Any]] = []
        execution_page_number = 0
        for source_page in source_plan.get("pages") or []:
            source_panels = sorted(
                list(source_page.get("panels") or []),
                key=lambda item: int(item.get("order") or 0),
            )
            for source_panel in source_panels:
                execution_page_number += 1
                panel_id = str(source_panel["id"])
                panel_rect = source_panel["rect"]
                execution_page = copy.deepcopy(source_page)
                execution_page["page_number"] = execution_page_number
                execution_panel = copy.deepcopy(source_panel)
                execution_panel["order"] = 1
                execution_panel["rect"] = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
                execution_page["panels"] = [execution_panel]
                execution_page["page_prompt"] = "single panel, full scene"
                execution_page["_comic_maker_large_screen"] = True
                execution_page["_source_page_number"] = source_page["page_number"]
                execution_page["_source_panel_id"] = panel_id

                spatial_prompts = []
                for raw_spatial in source_page.get("spatial_prompts") or []:
                    if str(raw_spatial.get("panel_id") or "") != panel_id:
                        continue
                    spatial = copy.deepcopy(raw_spatial)
                    spatial["centers"] = [
                        self._panel_local_point(center, panel_rect)
                        for center in spatial.get("centers") or []
                    ]
                    spatial.pop("center", None)
                    spatial_prompts.append(spatial)
                execution_page["spatial_prompts"] = spatial_prompts

                execution_page["bubbles"] = []
                for raw_bubble in source_page.get("bubbles") or []:
                    if str(raw_bubble.get("panel_id") or "") != panel_id:
                        continue
                    bubble = copy.deepcopy(raw_bubble)
                    bubble["rect"] = self._panel_local_rect(
                        bubble["rect"], panel_rect
                    )
                    if bubble.get("tail") is not None:
                        bubble["tail"] = self._panel_local_point(
                            bubble["tail"], panel_rect
                        )
                    execution_page["bubbles"].append(bubble)

                execution_page["sound_effects"] = []
                for raw_effect in source_page.get("sound_effects") or []:
                    if str(raw_effect.get("panel_id") or "") != panel_id:
                        continue
                    effect = copy.deepcopy(raw_effect)
                    effect["anchor"] = self._panel_local_point(
                        effect["anchor"], panel_rect
                    )
                    execution_page["sound_effects"].append(effect)
                execution_pages.append(execution_page)

        if not execution_pages:
            raise ValueError("큰 화면 생성 대상 패널이 없습니다.")
        execution_plan = copy.deepcopy(source_plan)
        execution_plan["pages"] = execution_pages
        execution_plan["page_count"] = len(execution_pages)
        execution_plan["width"] = width
        execution_plan["height"] = height
        return execution_plan

    def _prepare(self, auto_generate: bool = True, single_panel_mode: bool = False) -> None:
        with self._run_lock:
            if self._active_run is not None or self._pending is not None:
                self._toast("이미 준비 또는 생성 중인 만화가 있습니다.", "warning")
                return
            self._planning_logs.clear()
            self._recent_logs.clear()
        current = self.ctx.get_current_request()
        if not current.get("ok") or str(current.get("api_mode") or "").upper() != "NAI":
            if auto_generate:
                with self._run_lock:
                    self._planning = False
                self._refresh_panel()
            self._toast("Comic Maker는 현재 NAI 모드에서만 사용할 수 있습니다.", "error")
            return
        try:
            width, height = self._current_resolution(current)
            prompts, ucs, positions, male_count, female_count = self._character_snapshot()
            if auto_generate:
                params = current.get("params") if isinstance(current.get("params"), dict) else {}
                # Story generation must receive the scene prompt, not PE's style prefix.
                # Prefix/pre_prompt is still preserved later when NAIA renders the comic images.
                prefix_for_strip = str(
                    params.get("pre_prompt") or params.get("prefix_prompt") or ""
                ).strip(" ,")
                postfix_prompt = str(params.get("post_prompt") or "").strip(" ,")

                # `params.input` is preferred because it is the prompt-box / generated scene body
                # before PE's separate prefix is applied. Older runtimes may expose only the
                # assembled current prompt, so strip the known prefix/postfix in that fallback.
                source_main = str(params.get("input") or "").strip(" ,")
                if not source_main:
                    source_main = str(current.get("prompt") or "").strip(" ,")
                    if prefix_for_strip and source_main.startswith(prefix_for_strip):
                        source_main = source_main[len(prefix_for_strip):].lstrip(" ,")
                    if postfix_prompt and source_main.endswith(postfix_prompt):
                        source_main = source_main[:-len(postfix_prompt)].rstrip(" ,")

                source_parts = []
                for value in (source_main, postfix_prompt):
                    value = str(value or "").strip(" ,")
                    if value and value not in source_parts:
                        source_parts.append(value)
                source_prompt = ", ".join(source_parts)
                if not source_prompt:
                    raise ValueError("현재 NAIA 자동생성 프롬프트를 읽지 못했습니다.")
                plan = validate_comic_plan(self._client.generate_plan({
                    "source_prompt": source_prompt,
                    "character_prompts": prompts,
                    "male_count": male_count,
                    "female_count": female_count,
                    "width": width,
                    "height": height,
                    "page_count": None,
                    "locale": "ko",
                    "dialogue_mode": "none",
                }, progress=self._on_plan_progress))
            else:
                plan = validate_comic_plan(self._client.random_plan(
                    male_count=male_count, female_count=female_count, mark_used=False,
                ))
            if plan["male_count"] != male_count or plan["female_count"] != female_count:
                raise ValueError("PromptServer가 다른 캐릭터 인원수의 계획을 반환했습니다.")
            if single_panel_mode:
                plan = self._make_large_screen_plan(plan, width, height)
        except (ComicPlanNotFound, ComicServerError, ValueError) as exc:
            if auto_generate:
                self._append_log(f"오류: {exc}", force_refresh=True)
                self._store_recent_logs(self._planning_logs)
                with self._run_lock:
                    self._planning = False
                self._refresh_panel()
            self._toast(str(exc), "error")
            return
        # Normalized page geometry can be rendered at the active NAIA canvas
        # size even when a stored server plan carries legacy dimensions.
        plan["width"] = width
        plan["height"] = height
        pending = _PendingComic(plan, current, prompts, ucs, positions, single_panel_mode)
        with self._run_lock:
            self._pending = pending
            if auto_generate:
                self._planning = False
        if auto_generate:
            self._append_log(
                f"ComicPlan 수신 완료: {plan['page_count']}페이지",
                force_refresh=True,
            )
            self._toast(
                f"ComicPlan 수신 완료: {plan['page_count']}페이지. NAIA 이미지 생성을 자동 시작합니다.",
                "success",
            )
            self._start_pending()
        else:
            self._refresh_panel()

    def _start_pending(self) -> None:
        with self._run_lock:
            pending = self._pending
            self._pending = None
            can_start = pending is not None and self._active_run is None

        if not can_start:
            self._refresh_panel()
            return

        run = _ComicRun(
            pending=pending,
            output_dir=self._create_output_dir(pending.plan),
            logs=list(self._planning_logs[-self._LOG_LIMIT:]),
        )
        with self._run_lock:
            self._active_run = run

        self._append_log("NAIA 이미지 생성을 시작합니다.", run=run, force_refresh=True)

        self._refresh_panel()

        try:
            (run.output_dir / "comic_plan.json").write_text(
                json.dumps(pending.plan, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (run.output_dir / "character_snapshot.json").write_text(
                json.dumps(
                    {"prompts": pending.prompts, "uc": pending.ucs},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            pages = list(pending.plan.get("pages") or [])
            if not pages:
                raise RuntimeError("ComicPlan에 생성할 페이지가 없습니다.")

            # IMPORTANT:
            # Enqueue ALL pages here, outside any generation-result callback.
            # NAIA blocks chained enqueue_generation() calls while a generation
            # event is being handled. Since the full ComicPlan already exists,
            # there is no reason to enqueue page N+1 from page N's result event.
            for index, page in enumerate(pages, start=1):
                self._enqueue_page(run, page)
                page_number = int(page.get("page_number") or index)
                message = f"[ComicMaker] queued page {page_number}/{len(pages)}"
                self.ctx.log(message)
                self._append_log(message, run=run)

            # Mark the whole plan as already submitted to the NAIA queue.
            if hasattr(run, "next_page_index"):
                run.next_page_index = len(pages)
            run.enqueued_complete = True

            message = (
                f"[ComicMaker] ComicPlan ready: {len(pages)} pages. "
                f"All page requests queued; starting NAIA queue once."
            )
            self.ctx.log(message)
            self._append_log(message, run=run)

            # Start the host queue exactly once.
            self._start_generation_queue(run)

        except Exception as exc:
            self._abort_run(run, f"Comic Maker 시작 실패: {exc}")
            return

        self._refresh_panel()
        self._toast(
            f"ComicPlan 완료 · {len(pages)}페이지를 NAIA 큐에 등록했습니다.",
            "success",
        )


    def _start_generation_queue(self, run: _ComicRun) -> None:
        """Wake the host queue on both newer and older NAIA extension APIs."""
        starter = getattr(self.ctx, "start_generation_queue", None)
        if callable(starter):
            started = starter()
            if not isinstance(started, dict) or not started.get("ok"):
                message = started.get("message") if isinstance(started, dict) else ""
                raise RuntimeError(message or "생성 큐 시작 실패")
            return

        params = run.pending.current.get("params")
        raw_port = params.get("web_session_port") if isinstance(params, dict) else None
        try:
            port = int(raw_port or 7243)
        except (TypeError, ValueError):
            port = 7243
        if not 1 <= port <= 65535:
            port = 7243
        threading.Thread(
            target=self._resume_generation_queue,
            args=(run, port),
            daemon=True,
            name="comic-maker-queue-resume",
        ).start()

    def _resume_generation_queue(self, run: _ComicRun, port: int) -> None:
        """Use the host's existing queue-resume HTTP route without blocking its WS loop."""
        from urllib.request import Request, urlopen

        request = Request(
            f"http://127.0.0.1:{port}/api/queue/action",
            data=json.dumps({"action": "resume"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise RuntimeError("큐 재개 응답이 올바르지 않습니다")
        except Exception as exc:
            self._abort_run(run, f"Comic Maker 큐 시작 실패: {exc}")

    @staticmethod
    def _join(parts: list[Any]) -> str:
        return ", ".join(str(value).strip(" ,") for value in parts if str(value or "").strip(" ,"))

    @classmethod
    def _page_person_count_prefix(
        cls, prefix_prompt: str, entries: list[dict[str, Any]]
    ) -> str:
        """Replace stale whole-plan person tags with this page's visible cast."""
        try:
            from core.wildcard_processor import split_tags_smart

            prefix_tags = split_tags_smart(prefix_prompt)
        except Exception:
            prefix_tags = str(prefix_prompt or "").split(",")
        preserved = [
            str(tag).strip()
            for tag in prefix_tags
            if str(tag).strip()
            and not cls._PERSON_COUNT_TAG_RE.fullmatch(str(tag).strip())
        ]

        character_ids = {
            str(item.get("character_id") or "").strip().lower()
            for item in entries
            if str(item.get("character_id") or "").strip()
        }
        counts = {
            "boy": sum(character_id.startswith("male") for character_id in character_ids),
            "girl": sum(character_id.startswith("female") for character_id in character_ids),
            "other": sum(
                not character_id.startswith(("male", "female"))
                for character_id in character_ids
            ),
        }
        count_tags = []
        for singular in ("boy", "girl", "other"):
            count = counts[singular]
            if count:
                amount = "6+" if count >= 6 else str(count)
                count_tags.append(f"{amount}{singular if count == 1 else singular + 's'}")
        return cls._join([*count_tags, *preserved])

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
        raw_input_prompt = str(current_params.get("input") or "")
        marker = "#랜덤프롬프트"

        # Keep only the fixed art/style section above the marker for final render.
        fixed_input_prompt = (
            raw_input_prompt.split(marker, 1)[0]
            if marker in raw_input_prompt
            else raw_input_prompt
        ).strip(" ,\n\r\t")

        prefix_prompt = str(
            current_params.get("pre_prompt")
            or current_params.get("prefix_prompt")
            or ""
        ).strip(" ,\n\r\t")

        # Older runtimes may not expose pre_prompt/prefix_prompt separately.
        # Then use params.input, but only the part above #랜덤프롬프트.
        if not prefix_prompt:
            prefix_prompt = fixed_input_prompt

        spatial_entries = self._spatial_entries(pending, page)
        prefix_prompt = self._page_person_count_prefix(prefix_prompt, spatial_entries)

        postfix_prompt = str(current_params.get("post_prompt") or "").strip()
        # panel-N belongs in BASE only. Keep it ascending.
        panel_directions = []
        ordered_panels = sorted(
            list(page.get("panels") or []),
            key=lambda item: int(item.get("order") or 0),
        )
        for panel_index, panel in enumerate(ordered_panels, start=1):
            panel_order = int(panel.get("order") or panel_index)
            panel_parts = [panel.get("shot"), panel.get("prompt")]
            if pending.single_panel_mode:
                panel_parts = [
                    panel.get("location"), panel.get("shot"),
                    panel.get("action"), panel.get("prompt"),
                ]
            shared_direction = self._join(panel_parts)
            if shared_direction:
                panel_directions.append(
                    f"panel{panel_order}: {shared_direction}"
                )

        render_description = (
            "single full-canvas comic panel, sequential art, consistent characters"
            if pending.single_panel_mode
            else "full comic page, multiple panels, sequential art, consistent characters"
        )
        prompt = self._join([prefix_prompt, plan.get("global_prompt"),
                             render_description,
                             page.get("location"), page.get("base_prompt"), page.get("page_prompt"),
                              *panel_directions,
                             postfix_prompt])
        negative = self._join([current_params.get("negative_prompt"), page.get("negative_prompt"), "watermark"])

        characters, ucs, positions, character_ids = [], [], [], []
        # Old stored plans predate the spatial/outfit-only contract and often
        # contain a second, conflicting physical identity here. Preserve fixed
        # active characters by applying server outfits only to new spatial plans.
        has_spatial_contract = any(p.get("spatial_prompts") for p in plan.get("pages") or [])
        # Auto-generated plans intentionally carry placeholder character_prompts only to satisfy
        # PromptServer's persisted ComicPlan schema. The active NAIA character prompts are authoritative.
        outfits = ({ } if plan.get("preset_id") == "comic_auto"
                   else (plan.get("character_prompts") or {})) if has_spatial_contract else {}
        for item in spatial_entries:
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
            message = str(fetched.get("message") or "result image unavailable")
            run.failures.append(f"page {page_number}: {message}")
            log_message = (
                f"[ComicMaker] NAI page failed: "
                f"{page_number}/{run.pending.plan['page_count']} - {message}"
            )
            self.ctx.log(log_message)
            self._append_log(log_message, run=run)
        else:
            try:
                plan = run.pending.plan
                image = ImageOps.fit(
                    fetched["image"].convert("RGB"),
                    (plan["width"], plan["height"]),
                    method=Image.Resampling.LANCZOS,
                )
                path = run.output_dir / f"page_{page_number:03d}.png"
                image.save(path, format="PNG")
                run.page_paths[page_number] = path
                log_message = (
                    f"[ComicMaker] NAI page complete: "
                    f"{page_number}/{plan['page_count']} -> {path.name}"
                )
                self.ctx.log(log_message)
                self._append_log(log_message, run=run)
            except Exception as exc:
                run.failures.append(f"page {page_number} 저장: {exc}")
                log_message = f"[ComicMaker] page {page_number} save failed: {exc}"
                self.ctx.log(log_message)
                self._append_log(log_message, run=run)

        total = int(
            run.pending.plan.get("page_count")
            or len(run.pending.plan.get("pages") or [])
        )
        completed = len(run.page_paths) + len(run.failures)

        try:
            with self._run_lock:
                still_active = self._active_run is run
                no_outstanding = not run.requests

            # All pages were already queued before generation started.
            # Never enqueue from a generation-result event.

            with self._run_lock:
                done = (
                    self._active_run is run
                    and run.enqueued_complete
                    and not run.requests
                )
                if done:
                    self._active_run = None
            self._refresh_panel()
            if done:
                self._finish_run(run)

        except Exception as exc:
            self._abort_run(
                run,
                f"다음 페이지 생성 시작 실패: {exc}",
            )


    def _finish_run(self, run: _ComicRun) -> None:
        plan = run.pending.plan
        if run.failures or len(run.page_paths) != plan["page_count"]:
            detail = run.failures[0] if run.failures else "완성되지 않은 페이지가 있습니다."
            self._append_log(f"만화 생성 실패: {detail}", run=run, force_refresh=True)
            self._store_recent_logs(run.logs)
            self._refresh_panel()
            self._toast(f"만화 생성 실패 — used 처리 안 함: {detail}", "error")
            return
        try:
            self._client.mark_used(plan["id"])
        except ComicServerError as exc:
            self._append_log(f"페이지 생성 완료, used 처리 실패: {exc}", run=run, force_refresh=True)
            self._store_recent_logs(run.logs)
            self._refresh_panel()
            self._toast(f"페이지 생성 완료, used 처리 실패: {exc}", "warning")
            return
        self._append_log(f"만화 {plan['page_count']}페이지 저장 완료", run=run, force_refresh=True)
        self._store_recent_logs(run.logs)
        self._refresh_panel()
        self._toast(f"만화 {plan['page_count']}페이지 저장 완료: {run.output_dir}", "success")

    def _abort_run(self, run: _ComicRun, message: str) -> None:
        with self._run_lock:
            if self._active_run is run:
                self._active_run = None
            self._planning = False
        self._append_log(f"실패: {message}", run=run, force_refresh=True)
        self._store_recent_logs(run.logs)
        self._refresh_panel()
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

# === LARGE SCREEN BUTTON COMPATIBILITY PATCH ===
if not getattr(ComicMakerFeature, "_large_screen_button_installed", False):
    _large_screen_original_panel_fields = ComicMakerFeature.panel_fields
    _large_screen_original_handle_action = ComicMakerFeature.handle_action

    def _large_screen_panel_fields(self, *args, **kwargs):
        fields = _large_screen_original_panel_fields(self, *args, **kwargs)

        if not isinstance(fields, list):
            return fields

        if any(
            isinstance(item, dict) and item.get("key") == "make_ja"
            for item in fields
        ):
            return fields

        make_index = None
        for index, item in enumerate(fields):
            if isinstance(item, dict) and item.get("key") == "make":
                make_index = index
                break

        if make_index is not None:
            fields.insert(make_index + 1, {
                "key": "make_ja",
                "type": "action",
                "label": "큰 화면으로 생성",
                "help": (
                    "자동 ComicPlan의 각 패널을 독립된 832 × 1216 전체 이미지로 "
                    "순서대로 생성합니다."
                ),
            })

        return fields

    def _large_screen_handle_action(self, full_key, *args, **kwargs):
        if full_key == self.key("make_ja"):
            lock = getattr(self, "_run_lock", None)

            if lock is not None:
                with lock:
                    busy = bool(
                        getattr(self, "_planning", False)
                        or getattr(self, "_pending", None) is not None
                        or getattr(self, "_active_run", None) is not None
                    )
                    if not busy:
                        try:
                            self._planning = True
                            self._planning_logs.clear()
                            self._recent_logs.clear()
                        except Exception:
                            pass
            else:
                busy = bool(
                    getattr(self, "_planning", False)
                    or getattr(self, "_pending", None) is not None
                    or getattr(self, "_active_run", None) is not None
                )
                if not busy and hasattr(self, "_planning"):
                    self._planning = True
                    if hasattr(self, "_planning_logs"):
                        self._planning_logs.clear()
                    if hasattr(self, "_recent_logs"):
                        self._recent_logs.clear()

            if busy:
                toast = getattr(self, "_toast", None)
                if callable(toast):
                    toast("이미 Comic Maker 작업이 진행 중입니다.", "warning")
                return

            refresh = getattr(self, "_refresh_panel", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    pass

            toast = getattr(self, "_toast", None)
            if callable(toast):
                toast(
                    "ComicPlan의 각 패널을 큰 화면 이미지로 만드는 중입니다...",
                    "info",
                )

            import threading as _large_screen_threading

            _large_screen_threading.Thread(
                target=self._prepare,
                kwargs={"auto_generate": True, "single_panel_mode": True},
                daemon=True,
                name="comic-maker-large-screen",
            ).start()
            return

        return _large_screen_original_handle_action(self, full_key, *args, **kwargs)

    ComicMakerFeature.panel_fields = _large_screen_panel_fields
    ComicMakerFeature.handle_action = _large_screen_handle_action
    ComicMakerFeature._large_screen_button_installed = True
