from __future__ import annotations

from .base_feature import BaseFeature


class CharacterSameWildcardFeature(BaseFeature):
    """Reuse one wildcard result inside one NAID4 Character expansion only."""

    id = "character_same_wildcard"
    name = "캐릭터 동일 와일드카드 고정"
    description = (
        "한 번의 그림 생성에서 NAID4 Character 프롬프트/UC에 같은 와일드카드가 "
        "여러 번 나오면 첫 번째 결과를 재사용합니다. 일반 프롬프트에는 적용하지 않습니다."
    )
    category = "NAID4 Character"
    # Register after server_random_prompt so this layer surrounds its
    # character_params_from_settings replacement.  The server feature appends
    # character prompt text after calling the next layer; being outermost lets
    # us resolve wildcard tokens in that appended character text while the
    # per-call cache is still alive.
    order = 40
    default_enabled = False

    CACHE_ATTR = "_naia_exten_character_same_wildcard_cache"
    DEPTH_ATTR = "_naia_exten_character_same_wildcard_depth"
    SERVER_RESPONSE_ATTR = "_naia_exten_server_random_prompt_response"
    SERVER_RESPONSE_METADATA_KEY = "naia_exten_server_random_prompt_response"
    RESYNC_ACTION = "resync_now"

    def __init__(self):
        super().__init__()
        self._context = None

    def register(self) -> None:
        app_context = self.ext.host.app_context
        if app_context is None:
            self.ctx.log("캐릭터 동일 와일드카드: NAIA context를 찾지 못해 비활성화됩니다.")
            return

        self._context = app_context
        self._patch_character_scope()
        self._patch_wildcard_resolution()
        self.ctx.log("Character same-wildcard feature registered")

    def _runtime_active(self) -> bool:
        if not self.is_enabled():
            return False
        record = getattr(self.ctx, "_record", None)
        if record is not None:
            try:
                return bool(record.is_active)
            except Exception:
                return False
        return True

    # ------------------------------------------------------------------
    # One cache per fresh NAID4 Character resolution call.
    # ------------------------------------------------------------------

    def _patch_character_scope(self) -> None:
        import core.character_settings as character_settings

        self.ext.patches.wrap_method(
            owner=self.id,
            target=character_settings,
            method_name="character_params_from_settings",
            replace=self._character_params_scope,
        )

    def _character_params_scope(self, original, app_context, *args, **kwargs):
        if not self._runtime_active():
            return original(app_context, *args, **kwargs)

        depth = int(getattr(app_context, self.DEPTH_ATTR, 0) or 0)
        if depth == 0:
            setattr(app_context, self.CACHE_ATTR, {})
        setattr(app_context, self.DEPTH_ATTR, depth + 1)

        try:
            result = original(app_context, *args, **kwargs)
            if depth == 0:
                result = self._expand_server_character_additions(app_context, result)
            return result
        finally:
            next_depth = max(0, int(getattr(app_context, self.DEPTH_ATTR, 1) or 1) - 1)
            if next_depth:
                setattr(app_context, self.DEPTH_ATTR, next_depth)
            else:
                try:
                    delattr(app_context, self.DEPTH_ATTR)
                except Exception:
                    pass
                try:
                    delattr(app_context, self.CACHE_ATTR)
                except Exception:
                    pass

    def _expand_server_character_additions(self, app_context, result):
        """Expand only server-appended character prompt suffixes.

        ``server_random_prompt`` composes its character replacement outside the
        host implementation and appends ``character_prompts`` after the inner
        layer returns.  Resolve those suffixes here, while the character cache
        is still scoped to this call.  General/base prompt text is deliberately
        not inspected.
        """
        if not isinstance(result, dict):
            return result
        characters = result.get("characters")
        if not isinstance(characters, list):
            return result

        response = getattr(app_context, self.SERVER_RESPONSE_ATTR, None)
        if not isinstance(response, dict):
            prompt_context = getattr(app_context, "current_prompt_context", None)
            metadata = getattr(prompt_context, "metadata", None)
            if isinstance(metadata, dict):
                response = metadata.get(self.SERVER_RESPONSE_METADATA_KEY)
        prompts = response.get("character_prompts") if isinstance(response, dict) else None
        if not isinstance(prompts, dict):
            return result

        prompt_context = getattr(app_context, "current_prompt_context", None)
        wildcard_manager = getattr(app_context, "wildcard_manager", None)
        if prompt_context is None or wildcard_manager is None:
            return result
        try:
            from core.wildcard_processor import WildcardProcessor, split_tags_smart
        except Exception:
            return result

        processor = WildcardProcessor(wildcard_manager)
        additions = sorted(
            {
                str(value).strip()
                for value in prompts.values()
                if str(value or "").strip()
            },
            key=len,
            reverse=True,
        )
        if not additions:
            return result

        updated = list(characters)
        changed = False
        character_ids = result.get("character_ids")
        for index, raw_prompt in enumerate(characters):
            prompt = str(raw_prompt or "")
            addition = next(
                (
                    candidate
                    for candidate in additions
                    if prompt == candidate or prompt.endswith(", " + candidate)
                ),
                None,
            )
            if addition is None:
                continue

            slot = (
                character_ids[index]
                if isinstance(character_ids, list) and index < len(character_ids)
                else str(index + 1)
            )
            expanded = ", ".join(
                processor.expand_tags(
                    [str(tag).strip() for tag in split_tags_smart(addition) if str(tag).strip()],
                    prompt_context,
                    location="character",
                    slot=slot,
                    slot_label=index + 1,
                )
            )
            if expanded == addition:
                continue
            if prompt == addition:
                updated[index] = expanded
            else:
                updated[index] = prompt[: -len(addition)] + expanded
            changed = True

        if not changed:
            return result
        output = dict(result)
        output["characters"] = updated
        return output

    # ------------------------------------------------------------------
    # Reuse only when WildcardProcessor says location == "character".
    # ------------------------------------------------------------------

    def _patch_wildcard_resolution(self) -> None:
        from core.wildcard_processor import WildcardProcessor

        self.ext.patches.wrap_method(
            owner=self.id,
            target=WildcardProcessor,
            method_name="_get_wildcard_line",
            replace=self._get_wildcard_line,
        )

    def _get_wildcard_line(self, original, processor, wildcard_name, context):
        if not self._runtime_active():
            return original(processor, wildcard_name, context)
        if str(getattr(context, "_wc_location", "") or "") != "character":
            return original(processor, wildcard_name, context)

        app_context = self._context
        if app_context is None:
            return original(processor, wildcard_name, context)

        cache = getattr(app_context, self.CACHE_ATTR, None)
        if not isinstance(cache, dict):
            # Outside character_params_from_settings: do not accidentally make
            # the cache persist across independent rolls.
            return original(processor, wildcard_name, context)

        cache_key = self._cache_key(processor, wildcard_name)
        if cache_key in cache:
            actual_key, chosen_line = cache[cache_key]
            # A repeated occurrence is still recorded for Wildcard Watch/history,
            # but the sequential counter is deliberately NOT advanced again.
            try:
                context.wildcard_history.setdefault(actual_key, []).append(chosen_line)
            except Exception:
                pass
            try:
                processor._record_roll(context, actual_key, chosen_line)
            except Exception:
                pass
            return chosen_line

        chosen_line = original(processor, wildcard_name, context)
        if chosen_line is None:
            return None

        actual_key = self._actual_key(processor, wildcard_name)
        cache[cache_key] = (actual_key, chosen_line)
        return chosen_line

    @staticmethod
    def _cache_key(processor, wildcard_name) -> str:
        raw = str(wildcard_name or "").strip()
        # Keep sequential/random/observer syntax distinct, while normalizing the
        # underlying file key so fuzzy aliases of the same token stay coherent.
        prefix = ""
        lookup_name = raw
        if raw.startswith("*"):
            prefix = "*"
            lookup_name = raw[1:]
        elif raw.startswith("$"):
            prefix = "$"
            try:
                master, slave = raw[1:].split(":", 1)
            except ValueError:
                return raw
            actual_master = processor._find_wildcard_key(master) or master
            actual_slave = processor._find_wildcard_key(slave) or slave
            return f"$:{actual_master}:{actual_slave}"
        actual = processor._find_wildcard_key(lookup_name) or lookup_name
        return f"{prefix}:{actual}"

    @staticmethod
    def _actual_key(processor, wildcard_name) -> str:
        raw = str(wildcard_name or "").strip()
        lookup_name = raw
        if raw.startswith("*"):
            lookup_name = raw[1:]
        elif raw.startswith("$"):
            try:
                _master, lookup_name = raw[1:].split(":", 1)
            except ValueError:
                return raw
        return str(processor._find_wildcard_key(lookup_name) or lookup_name)

    def panel_fields(self) -> list[dict]:
        # The Character module layout changes between NAIA releases, so keep
        # this feature's controls in the extension-owned panel only.
        return [
            {
                "key": self.RESYNC_ACTION,
                "type": "action",
                "label": "캐릭터 와일드카드 다시 동기화",
                "help": (
                    "현재 활성 캐릭터 프롬프트를 새로 전개하고, 같은 와일드카드 토큰에 "
                    "하나의 값을 공유하도록 미리보기와 생성용 상태를 갱신합니다."
                ),
                "visible_when": {"field": "__enabled__", "in": [True]},
            },
        ]

    def handle_action(self, full_key: str) -> None:
        if full_key != self.key(self.RESYNC_ACTION):
            return
        if not self._runtime_active():
            self._resync_feedback("먼저 같은 와일드카드 값 공유를 켜세요.", "info")
            return

        app_context = self._context
        if app_context is None:
            self._resync_feedback("NAIA Character context를 찾지 못했습니다.", "error")
            return

        try:
            from core.character_settings import roll_character_params

            mode_getter = getattr(app_context, "get_api_mode", None)
            mode = mode_getter() if callable(mode_getter) else getattr(
                app_context, "current_api_mode", "NAI"
            )
            # This is an explicit fresh roll, not another read of the last
            # generation's wildcard history. roll_character_params stores the
            # expanded payload as the host's SSOT character snapshot.
            params = roll_character_params(
                app_context,
                mode=str(mode or "NAI"),
                reuse_current_context=False,
            )
            count = len(params.get("characters") or []) if isinstance(params, dict) else 0
            if count:
                self._resync_feedback(f"캐릭터 {count}개 와일드카드 다시 동기화 완료", "success")
            else:
                self._resync_feedback("활성 캐릭터 프롬프트가 없습니다.", "info")
        except Exception as exc:
            self.ctx.log(f"Character same-wildcard resync failed: {exc}")
            self._resync_feedback(f"캐릭터 와일드카드 동기화 실패: {exc}", "error")

    def _resync_feedback(self, message: str, level: str) -> None:
        try:
            self.ctx.show_toast(str(message), str(level))
        except Exception:
            pass
