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

    _PANEL_JS_MARKER = "/* NAIA_EXTEN_CHARACTER_SAME_WILDCARD_PANEL_V4 */"
    _PANEL_JS = r'''/* NAIA_EXTEN_CHARACTER_SAME_WILDCARD_PANEL_V4 */
(() => {
  if (window.__naiaExtenCharacterSameWildcardPanelV4) return;
  window.__naiaExtenCharacterSameWildcardPanelV4 = true;

  const EXT_ID = 'naia_exten';
  const SETTING_KEY = 'feature__character_same_wildcard__enabled';
  const ROW_ID = 'naiaExtenCharacterSameWildcardRow';
  const STYLE_ID = 'naiaExtenCharacterSameWildcardStyle';

  function extensionState() {
    const list = Array.isArray(lastExtensionsState?.extensions)
      ? lastExtensionsState.extensions
      : [];
    return list.find(item => item?.id === EXT_ID) || null;
  }

  function enabledValue() {
    return Boolean(extensionState()?.settings?.[SETTING_KEY]);
  }

  function requestExtensionState() {
    try {
      if (typeof requestModuleState === 'function') requestModuleState('extensions');
    } catch (_) {}
  }

  function setEnabled(checked) {
    const ext = extensionState();
    if (ext) {
      if (!ext.settings || typeof ext.settings !== 'object') ext.settings = {};
      ext.settings[SETTING_KEY] = Boolean(checked);
    }
    setModuleParam('extensions', `setting:${EXT_ID}:${SETTING_KEY}`, Boolean(checked));
  }

  function installStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      #${ROW_ID} {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        min-height: 38px;
        margin: 2px 0 8px;
        padding: 7px 10px;
        border: 1px solid var(--border-dim);
        border-radius: 8px;
        background: color-mix(in srgb, var(--bg-surface) 82%, transparent);
      }
      #${ROW_ID} .naia-exten-character-label-wrap { min-width: 0; }
      #${ROW_ID} .naia-exten-character-controls { display:flex; align-items:center; gap:8px; flex:0 0 auto; }
      #${ROW_ID} .naia-exten-character-label {
        display: block;
        color: var(--text-primary);
        font-size: 12px;
        font-weight: 650;
        line-height: 1.3;
      }
      #${ROW_ID} .naia-exten-character-help {
        display: block;
        margin-top: 2px;
        color: var(--text-muted);
        font-size: 10px;
        line-height: 1.25;
      }
      #${ROW_ID} .naia-exten-character-switch {
        position: relative;
        display: inline-block;
        flex: 0 0 auto;
        width: 42px;
        height: 23px;
        cursor: pointer;
      }
      #${ROW_ID} .naia-exten-character-switch input {
        position: absolute;
        opacity: 0;
        width: 1px;
        height: 1px;
        pointer-events: none;
      }
      #${ROW_ID} .naia-exten-character-track {
        position: absolute;
        inset: 0;
        border: 1px solid var(--border-dim);
        border-radius: 999px;
        background: var(--bg-elevated);
        transition: background .15s ease, border-color .15s ease;
      }
      #${ROW_ID} .naia-exten-character-track::after {
        content: '';
        position: absolute;
        top: 2px;
        left: 2px;
        width: 17px;
        height: 17px;
        border-radius: 50%;
        background: var(--text-muted);
        transition: transform .15s ease, background .15s ease;
      }
      #${ROW_ID} input:checked + .naia-exten-character-track {
        background: color-mix(in srgb, var(--accent) 72%, var(--bg-elevated));
        border-color: var(--accent);
      }
      #${ROW_ID} input:checked + .naia-exten-character-track::after {
        transform: translateX(19px);
        background: #fff;
      }
      #${ROW_ID} input:focus-visible + .naia-exten-character-track {
        outline: 2px solid var(--accent);
        outline-offset: 2px;
      }
      #${ROW_ID}.naia-exten-unavailable { opacity: .58; }
      #${ROW_ID} .naia-exten-character-resync {
        min-height: 27px; padding: 4px 8px; border: 1px solid var(--border-dim);
        border-radius: 7px; background: var(--bg-elevated); color: var(--text-secondary);
        cursor: pointer; font-size: 10px; font-weight: 650; white-space: nowrap;
      }
      #${ROW_ID} .naia-exten-character-resync:hover:not(:disabled) { border-color: var(--accent); color: var(--text-primary); }
      #${ROW_ID} .naia-exten-character-resync:disabled { cursor: default; opacity: .55; }
    `;
    document.head.appendChild(style);
  }

  function syncRowState() {
    const row = document.getElementById(ROW_ID);
    if (!row) return;
    const input = row.querySelector('input[type="checkbox"]');
    if (!input) return;
    const resync = row.querySelector('[data-character-resync]');

    const ext = extensionState();
    const available = Boolean(ext && ext.status === 'loaded' && ext.enabled !== false);
    const enabled = available && enabledValue();
    input.disabled = !available;
    input.checked = enabled;
    if (resync) resync.disabled = !enabled;
    // The feature switch disables behavior, but its control must remain
    // visible so the feature can be enabled again from this module.
    row.style.display = available ? '' : 'none';
    row.classList.toggle('naia-exten-unavailable', !available);
    if (!available) requestExtensionState();
  }

  function ensureRow() {
    if (typeof currentModuleId === 'undefined' || currentModuleId !== 'character') return;
    if (typeof moduleBody === 'undefined' || !moduleBody) return;

    const workspace = moduleBody.querySelector('.mod-character-workspace');
    const actions = workspace?.querySelector('.mod-char-actions');
    if (!workspace || !actions) return;

    installStyle();
    let row = document.getElementById(ROW_ID);
    if (!row) {
      row = document.createElement('div');
      row.id = ROW_ID;

      const labelWrap = document.createElement('div');
      labelWrap.className = 'naia-exten-character-label-wrap';

      const label = document.createElement('span');
      label.className = 'naia-exten-character-label';
      label.textContent = '같은 와일드카드 값 공유';

      const help = document.createElement('span');
      help.className = 'naia-exten-character-help';
      help.textContent = '같은 그림의 Character 태그에서 동일 __name__/__*name__ 결과를 재사용';

      const switchLabel = document.createElement('label');
      switchLabel.className = 'naia-exten-character-switch';
      switchLabel.title = '예: 두 캐릭터에 __*aa__를 쓰면 한 그림에서는 같은 aa 값이 적용됩니다.';

      const input = document.createElement('input');
      input.type = 'checkbox';
      input.setAttribute('aria-label', '같은 와일드카드 값 공유');
      input.addEventListener('change', () => {
        setEnabled(input.checked);
        syncRowState();
      });

      const track = document.createElement('span');
      track.className = 'naia-exten-character-track';

      const controls = document.createElement('div');
      controls.className = 'naia-exten-character-controls';

      const resync = document.createElement('button');
      resync.type = 'button';
      resync.className = 'naia-exten-character-resync';
      resync.dataset.characterResync = '1';
      resync.textContent = '다시 동기화';
      resync.title = '현재 활성 캐릭터 프롬프트를 새로 전개하고 같은 와일드카드 값을 다시 공유합니다.';
      resync.addEventListener('click', () => {
        if (!enabledValue()) {
          if (typeof showToast === 'function') showToast('먼저 같은 와일드카드 값 공유를 켜세요.', 'info');
          return;
        }
        resync.disabled = true;
        resync.textContent = '동기화 중…';
        try {
          setModuleParam('extensions', `setting:${EXT_ID}:feature__character_same_wildcard__resync_now`, true);
          if (typeof showToast === 'function') showToast('캐릭터 와일드카드 다시 동기화 중…', 'info');
          setTimeout(() => {
            try { if (typeof requestModuleState === 'function') requestModuleState('character'); } catch (_) {}
            resync.textContent = '다시 동기화';
            syncRowState();
          }, 350);
        } catch (_) {
          resync.textContent = '다시 동기화';
          syncRowState();
          if (typeof showToast === 'function') showToast('캐릭터 와일드카드 동기화 요청에 실패했습니다.', 'error');
        }
      });

      labelWrap.append(label, help);
      switchLabel.append(input, track);
      controls.append(resync, switchLabel);
      row.append(labelWrap, controls);
      actions.insertAdjacentElement('beforebegin', row);
    }

    syncRowState();
  }

  if (typeof moduleBody !== 'undefined' && moduleBody) {
    new MutationObserver(() => queueMicrotask(ensureRow))
      .observe(moduleBody, {childList: true, subtree: true});
  }

  try {
    const previousRenderExtensions = renderExtensions;
    renderExtensions = function(m) {
      const result = previousRenderExtensions(m);
      queueMicrotask(() => {
        ensureRow();
        syncRowState();
      });
      return result;
    };
  } catch (_) {}

  queueMicrotask(ensureRow);
  setTimeout(ensureRow, 250);
  setTimeout(ensureRow, 1000);
})();
'''
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
        self._patch_character_panel_frontend()
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
        # The visible Character-panel button uses the same validated action
        # route as the generic EXten panel. Keep it hidden there to avoid two
        # controls for the same operation.
        return [
            {
                "key": self.RESYNC_ACTION,
                "type": "action",
                "label": "캐릭터 와일드카드 다시 동기화",
                "help": (
                    "현재 활성 캐릭터 프롬프트를 새로 전개하고, 같은 와일드카드 토큰에 "
                    "하나의 값을 공유하도록 미리보기와 생성용 상태를 갱신합니다."
                ),
                "visible_when": {
                    "field": "__naia_exten_internal_never__",
                    "in": ["1"],
                },
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


    # ------------------------------------------------------------------
    # NAID4 Character module UI (extension-served; NAIA core untouched).
    # ------------------------------------------------------------------

    def _patch_character_panel_frontend(self) -> None:
        try:
            self.ext.patches.add_web_injection(
                owner=self.id,
                file_name="app.js",
                marker=self._PANEL_JS_MARKER,
                content=self._PANEL_JS,
            )
        except Exception as exc:
            self.ctx.log(f"Character same-wildcard panel UI unavailable: {exc}")
