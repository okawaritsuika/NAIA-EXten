from __future__ import annotations

import copy
import math
from typing import Any


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}은 숫자여야 합니다.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}은 숫자여야 합니다.") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label}은 유한한 숫자여야 합니다.")
    return result


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label}은 정수여야 합니다.")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{label}은 정수여야 합니다.")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}은 정수여야 합니다.") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{label}은 {minimum}~{maximum} 범위여야 합니다.")
    return result


def _point(raw: Any, label: str) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} 형식이 올바르지 않습니다.")
    point = {axis: _number(raw.get(axis), f"{label}.{axis}") for axis in ("x", "y")}
    if any(value < 0.0 or value > 1.0 for value in point.values()):
        raise ValueError(f"{label} 좌표는 0.0~1.0 범위여야 합니다.")
    return point


def _rect(raw: Any, label: str) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} 형식이 올바르지 않습니다.")
    rect = {axis: _number(raw.get(axis), f"{label}.{axis}") for axis in ("x", "y", "w", "h")}
    if rect["x"] < 0.0 or rect["y"] < 0.0 or rect["w"] <= 0.0 or rect["h"] <= 0.0:
        raise ValueError(f"{label} 좌표와 크기가 올바르지 않습니다.")
    if rect["x"] + rect["w"] > 1.0 + 1e-9 or rect["y"] + rect["h"] > 1.0 + 1e-9:
        raise ValueError(f"{label}이 페이지 경계를 벗어납니다.")
    return rect


def _unique_id(raw: Any, seen: set[str], label: str) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError(f"{label} ID가 비었습니다.")
    if value in seen:
        raise ValueError(f"{label} ID가 중복되었습니다: {value}")
    seen.add(value)
    return value


def _expected_character_ids(male_count: int, female_count: int) -> list[str]:
    return [f"male{index}" for index in range(1, male_count + 1)] + [
        f"female{index}" for index in range(1, female_count + 1)
    ]


def validate_comic_plan(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("ComicPlan 응답이 객체가 아닙니다.")
    plan = copy.deepcopy(raw)
    plan["id"] = _integer(plan.get("id"), "id", 1, 2**63 - 1)
    plan["width"] = _integer(plan.get("width"), "width", 256, 4096)
    plan["height"] = _integer(plan.get("height"), "height", 256, 4096)
    plan["page_count"] = _integer(plan.get("page_count"), "page_count", 1, 20)
    plan["male_count"] = _integer(plan.get("male_count", 0), "male_count", 0, 20)
    plan["female_count"] = _integer(plan.get("female_count", 0), "female_count", 0, 20)
    plan["preset_id"] = str(plan.get("preset_id") or "").strip()
    if not plan["preset_id"]:
        raise ValueError("preset_id가 비었습니다.")
    plan["text_mode"] = str(plan.get("text_mode") or "overlay").strip().lower()
    if plan["text_mode"] not in {"overlay", "ai"}:
        raise ValueError("text_mode는 overlay 또는 ai여야 합니다.")

    characters = plan.get("character_prompts")
    if not isinstance(characters, dict):
        raise ValueError("character_prompts 형식이 올바르지 않습니다.")
    expected_ids = _expected_character_ids(plan["male_count"], plan["female_count"])
    if set(map(str, characters.keys())) != set(expected_ids):
        raise ValueError("요청 인원수와 character_prompts 키가 일치하지 않습니다.")
    plan["character_prompts"] = {
        character_id: str(characters.get(character_id) or "").strip()
        for character_id in expected_ids
    }
    if any(not value for value in plan["character_prompts"].values()):
        raise ValueError("character_prompts에 빈 프롬프트가 있습니다.")

    pages = plan.get("pages")
    if not isinstance(pages, list) or len(pages) != plan["page_count"]:
        raise ValueError("page_count와 pages 길이가 일치하지 않습니다.")
    normalized_pages = []
    for page_index, raw_page in enumerate(pages, start=1):
        if not isinstance(raw_page, dict):
            raise ValueError(f"page {page_index} 형식이 올바르지 않습니다.")
        page = copy.deepcopy(raw_page)
        if _integer(page.get("page_number"), "page_number", 1, plan["page_count"]) != page_index:
            raise ValueError("page_number는 1부터 빠짐없이 연속이어야 합니다.")
        page["page_number"] = page_index

        panel_ids: set[str] = set()
        panels = page.get("panels") or []
        if not isinstance(panels, list):
            raise ValueError(f"page {page_index}.panels 형식이 올바르지 않습니다.")
        normalized_panels = []
        for panel_index, raw_panel in enumerate(panels, start=1):
            if not isinstance(raw_panel, dict):
                raise ValueError(f"page {page_index}.panel 형식이 올바르지 않습니다.")
            panel = copy.deepcopy(raw_panel)
            panel["id"] = _unique_id(panel.get("id"), panel_ids, "panel")
            panel["order"] = _integer(panel.get("order", panel_index), "panel.order", 1, 100)
            panel["rect"] = _rect(panel.get("rect"), f"panel {panel['id']}.rect")
            raw_character_ids = panel.get("character_ids") or []
            if not isinstance(raw_character_ids, list):
                raise ValueError(f"panel {panel['id']}.character_ids 형식이 올바르지 않습니다.")
            panel["character_ids"] = [str(value) for value in raw_character_ids]
            unknown = set(panel["character_ids"]) - set(expected_ids)
            if unknown:
                raise ValueError(f"panel {panel['id']}에 알 수 없는 캐릭터가 있습니다: {sorted(unknown)}")
            normalized_panels.append(panel)
        normalized_panels.sort(key=lambda item: item["order"])
        page["panels"] = normalized_panels

        spatial_prompts = page.get("spatial_prompts") or []
        if not isinstance(spatial_prompts, list):
            raise ValueError(f"page {page_index}.spatial_prompts 형식이 올바르지 않습니다.")
        normalized_spatial = []
        for spatial_index, raw_spatial in enumerate(spatial_prompts, start=1):
            if not isinstance(raw_spatial, dict):
                raise ValueError(f"page {page_index}.spatial_prompt 형식이 올바르지 않습니다.")
            spatial = copy.deepcopy(raw_spatial)
            character_id = str(spatial.get("character_id") or "").strip()
            if character_id and character_id not in expected_ids:
                raise ValueError(f"spatial prompt에 알 수 없는 캐릭터가 있습니다: {character_id}")
            prompt = str(spatial.get("prompt") or "").strip()
            if not prompt and not character_id:
                raise ValueError("spatial prompt에는 character_id 또는 prompt가 필요합니다.")
            centers = spatial.get("centers")
            if centers is None and spatial.get("center") is not None:
                centers = [spatial.get("center")]
            if not isinstance(centers, list) or not centers:
                raise ValueError("spatial prompt centers는 한 개 이상의 좌표여야 합니다.")
            spatial["character_id"] = character_id
            spatial["prompt"] = prompt
            spatial["centers"] = [
                _point(center, f"spatial prompt {spatial_index}.centers") for center in centers
            ]
            spatial.pop("center", None)
            normalized_spatial.append(spatial)
        page["spatial_prompts"] = normalized_spatial

        for key, label in (("bubbles", "bubble"), ("sound_effects", "sound effect")):
            values = page.get(key) or []
            if not isinstance(values, list):
                raise ValueError(f"page {page_index}.{key} 형식이 올바르지 않습니다.")
            seen_ids: set[str] = set()
            normalized = []
            for raw_item in values:
                if not isinstance(raw_item, dict):
                    raise ValueError(f"{label} 형식이 올바르지 않습니다.")
                item = copy.deepcopy(raw_item)
                item["id"] = _unique_id(item.get("id"), seen_ids, label)
                panel_id = str(item.get("panel_id") or "")
                if panel_id not in panel_ids:
                    raise ValueError(f"{label} {item['id']}가 존재하지 않는 panel_id를 참조합니다.")
                item["panel_id"] = panel_id
                if label == "bubble":
                    item["rect"] = _rect(item.get("rect"), f"bubble {item['id']}.rect")
                    item["tail"] = (
                        _point(item.get("tail"), f"bubble {item['id']}.tail")
                        if item.get("tail") is not None else None
                    )
                else:
                    item["anchor"] = _point(item.get("anchor"), f"sound effect {item['id']}.anchor")
                    max_width = _number(item.get("max_width"), f"sound effect {item['id']}.max_width")
                    if max_width <= 0.0 or max_width > 1.0:
                        raise ValueError("sound effect max_width는 0보다 크고 1 이하여야 합니다.")
                    item["max_width"] = max_width
                font_scale = _number(item.get("font_scale"), f"{label} {item['id']}.font_scale")
                if font_scale <= 0.0:
                    raise ValueError(f"{label} font_scale은 0보다 커야 합니다.")
                item["font_scale"] = font_scale
                item["rotation"] = _number(item.get("rotation", 0), f"{label} {item['id']}.rotation")
                item["text"] = str(item.get("text") or "")
                normalized.append(item)
            page[key] = normalized
        normalized_pages.append(page)

    plan["pages"] = normalized_pages
    plan["title"] = str(plan.get("title") or f"comic_{plan['id']}").strip()
    plan["global_prompt"] = str(plan.get("global_prompt") or "").strip()
    plan["locale"] = str(plan.get("locale") or "ko").strip()
    return plan


def panels_for_page(page: dict[str, Any]) -> list[dict[str, Any]]:
    panels = page.get("panels") or []
    if panels:
        return panels
    return [
        {
            "id": "__page__",
            "order": 1,
            "rect": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "location": page.get("location", ""),
            "shot": "",
            "action": "",
            "prompt": "",
            "character_ids": [],
        }
    ]
