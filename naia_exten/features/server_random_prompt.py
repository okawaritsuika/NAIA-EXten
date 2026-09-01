from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base_feature import BaseFeature


_NO_ACTIVE_RESPONSE = object()
_ACTIVE_SERVER_RESPONSE: ContextVar[Any] = ContextVar(
    "naia_exten_active_server_random_prompt", default=_NO_ACTIVE_RESPONSE
)
_APPLYING_CHARACTER_SETTINGS: ContextVar[bool] = ContextVar(
    "naia_exten_applying_character_settings", default=False
)
_PENDING_SERVER_REQUEST: ContextVar[Any] = ContextVar(
    "naia_exten_pending_server_random_prompt_request", default=None
)


class _ServerRandomPromptHook:
    """Final PromptProcessor hook; the feature pointer survives hot reload."""

    def __init__(self, app_context):
        self.app_context = app_context

    def get_title(self) -> str:
        return "Server Random Prompt Base"

    def get_pipeline_hook_info(self) -> dict[str, Any]:
        return {
            "target_pipeline": "PromptProcessor",
            "hook_point": "final_hookpoint",
            "priority": 900,
        }

    def execute_pipeline_hook(self, context):
        feature = getattr(self.app_context, ServerRandomPromptFeature.FEATURE_ATTR, None)
        if feature is None:
            return context
        return feature._append_base_prompt(context)


class ServerRandomPromptFeature(BaseFeature):
    """Use a local PromptServer scenario for the current random generation."""

    id = "server_random_prompt"
    name = "서버 랜덤 프롬프트"
    description = "PromptServer의 저장된 상황을 랜덤 생성에 결합합니다."
    category = "Prompt Engineering"
    order = 30
    default_enabled = False

    SERVER_BASE = "http://127.0.0.1:8765"
    RANDOM_PRESET = "__random__"
    RESPONSE_ATTR = "_naia_exten_server_random_prompt_response"
    PERSISTED_RESPONSE_ATTR = "_naia_exten_server_random_prompt_persisted_response"
    CONTEXT_RESPONSE_KEY = "naia_exten_server_random_prompt_response"
    PRESET_OPTIONS_KEY = "preset_options"
    SERVER_STATUS_KEY = "server_status"
    APPLIED_ATTR = "_naia_exten_server_random_prompt_base_applied"
    FEATURE_ATTR = "_naia_exten_server_random_prompt_feature"
    HOOK_ATTR = "_naia_exten_server_random_prompt_hook"
    _PANEL_JS_MARKER = "/* NAIA_EXTEN_SERVER_RANDOM_PROMPT_PANEL_V6 */"
    REQUEST_TIMEOUT = 0.8
    _GENDER_TAG_RE = re.compile(
        r"^(?P<count>\d+\+?|)?(?P<gender>boys?|girls?)$",
        re.IGNORECASE,
    )
    SERVER_MARKER = "#서버 랜덤 프롬프트"
    _REQUESTED_MALE_COUNT_KEY = "_naia_exten_requested_male_count"
    _REQUESTED_FEMALE_COUNT_KEY = "_naia_exten_requested_female_count"

    _PANEL_JS = r'''/* NAIA_EXTEN_SERVER_RANDOM_PROMPT_PANEL_V6 */
(() => {
  if (window.__naiaExtenServerRandomPromptPanelV6) return;
  window.__naiaExtenServerRandomPromptPanelV6 = true;

  const EXT_ID = 'naia_exten';
  const ENABLED_KEY = 'feature__server_random_prompt__enabled';
  const PRESET_KEY = 'feature__server_random_prompt__preset';
  const MARK_USED_KEY = 'feature__server_random_prompt__mark_used';
  const INCLUDE_USED_KEY = 'feature__server_random_prompt__include_used';
  const PRESET_OPTIONS_KEY = 'feature__server_random_prompt__preset_options';
  const SERVER_STATUS_KEY = 'feature__server_random_prompt__server_status';
  const SERVER_BASE = 'http://127.0.0.1:8765';
  const ROW_ID = 'naiaExtenServerRandomPromptRow';
  const STYLE_ID = 'naiaExtenServerRandomPromptStyle';
  const RANDOM_PRESET = '__random__';
  const FREE_PRESET = 'free';

  function extensionState() {
    const list = Array.isArray(lastExtensionsState?.extensions)
      ? lastExtensionsState.extensions : [];
    return list.find(item => item?.id === EXT_ID) || null;
  }
  function setting(key, fallback) {
    const value = extensionState()?.settings?.[key];
    return value === undefined ? fallback : value;
  }
  function setSetting(key, value) {
    const ext = extensionState();
    if (ext) {
      if (!ext.settings || typeof ext.settings !== 'object') ext.settings = {};
      ext.settings[key] = value;
    }
    setModuleParam('extensions', `setting:${EXT_ID}:${key}`, value);
  }
  function asBoolean(value, fallback) {
    if (typeof value === 'boolean') return value;
    if (typeof value === 'string') {
      const normalized = value.trim().toLowerCase();
      if (['true', '1', 'yes', 'on'].includes(normalized)) return true;
      if (['false', '0', 'no', 'off', ''].includes(normalized)) return false;
    }
    if (typeof value === 'number') return value !== 0;
    return fallback;
  }
  function requestExtensionState() {
    try { if (typeof requestModuleState === 'function') requestModuleState('extensions'); }
    catch (_) {}
  }
  function available() {
    const ext = extensionState();
    return Boolean(ext && ext.status === 'loaded' && ext.enabled !== false);
  }
  function installStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      #${ROW_ID} { margin: 4px 0 12px; padding: 9px 10px; border: 1px solid var(--border-dim); border-radius: 8px; background: color-mix(in srgb, var(--bg-surface) 82%, transparent); }
      #${ROW_ID} .naia-exten-server-title { display:flex; align-items:center; justify-content:space-between; gap:10px; font-size:12px; font-weight:650; }
      #${ROW_ID} .naia-exten-server-help { margin-top:3px; color:var(--text-muted); font-size:10px; line-height:1.3; }
      #${ROW_ID} .naia-exten-server-status { margin-top:6px; color:var(--text-muted); font-size:10px; }
      #${ROW_ID}.naia-exten-server-error .naia-exten-server-status { color:var(--text-warning, #e0a04a); }
      #${ROW_ID} .naia-exten-server-grid { display:grid; grid-template-columns:minmax(0,1fr); gap:7px; margin-top:8px; }
      #${ROW_ID} .naia-exten-server-options { display:flex; flex-wrap:wrap; gap:10px; margin-top:8px; }
      #${ROW_ID} .naia-exten-server-options label { flex-direction:row; align-items:center; gap:4px; white-space:nowrap; }
      #${ROW_ID} .naia-exten-server-options input { margin:0; accent-color:var(--accent); }
      #${ROW_ID} label { display:flex; flex-direction:column; gap:3px; color:var(--text-muted); font-size:10px; }
      #${ROW_ID} select { width:100%; box-sizing:border-box; min-height:28px; padding:3px 5px; color:var(--text-primary); background:var(--bg-elevated); border:1px solid var(--border-dim); border-radius:5px; }
      #${ROW_ID}.naia-exten-unavailable { opacity:.58; }
      #${ROW_ID} .naia-exten-server-switch { position:relative; display:inline-block; width:42px; height:23px; flex:0 0 auto; cursor:pointer; }
      #${ROW_ID} .naia-exten-server-switch input { position:absolute; opacity:0; width:1px; height:1px; pointer-events:none; }
      #${ROW_ID} .naia-exten-server-track { position:absolute; inset:0; border:1px solid var(--border-dim); border-radius:999px; background:var(--bg-elevated); }
      #${ROW_ID} .naia-exten-server-track::after { content:''; position:absolute; top:2px; left:2px; width:17px; height:17px; border-radius:50%; background:var(--text-muted); transition:transform .15s ease,background .15s ease; }
      #${ROW_ID} input:checked + .naia-exten-server-track { background:color-mix(in srgb, var(--accent) 72%, var(--bg-elevated)); border-color:var(--accent); }
      #${ROW_ID} input:checked + .naia-exten-server-track::after { transform:translateX(19px); background:#fff; }
    `;
    document.head.appendChild(style);
  }
  function updatePresetOptions(select, presets) {
    const current = String(setting(PRESET_KEY, RANDOM_PRESET));
    const options = [
      `<option value="${RANDOM_PRESET}">랜덤</option>`,
      `<option value="${FREE_PRESET}">선택 안 함 (free)</option>`,
      ...presets.filter(p => p && p.id && p.id !== FREE_PRESET).map(p =>
        `<option value="${String(p.id).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')}">${String(p.name || p.id).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}</option>`),
    ];
    select.innerHTML = options.join('');
    select.value = current;
    if (![...select.options].some(option => option.value === current)) select.value = RANDOM_PRESET;
  }
  function cachedPresets() {
    const raw = setting(PRESET_OPTIONS_KEY, '[]');
    if (Array.isArray(raw)) return raw;
    try {
      const parsed = JSON.parse(String(raw || '[]'));
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) { return []; }
  }
  function setServerStatus(text, error = false) {
    const row = document.getElementById(ROW_ID);
    const status = row?.querySelector('[data-server-status]');
    if (status) status.textContent = text || 'PromptServer 상태 확인 중';
    if (row) row.classList.toggle('naia-exten-server-error', Boolean(error));
  }
  async function loadPresets(select) {
    if (!select || select.dataset.serverPresetsLoading === 'true') return;
    select.dataset.serverPresetsLoading = 'true';
    const cached = cachedPresets();
    if (cached.length) updatePresetOptions(select, cached);
    setServerStatus(cached.length ? '캐시된 프리셋 표시 · 서버 동기화 중' : 'PromptServer 프리셋 로딩 중');
    try {
      const response = await fetch(`${SERVER_BASE}/api/presets`, {headers:{Accept:'application/json'}});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const presets = Array.isArray(data) ? data : (Array.isArray(data?.presets) ? data.presets : []);
      updatePresetOptions(select, presets);
      select.dataset.serverPresetsLoaded = 'true';
      select.dataset.serverPresetsRetry = '0';
      setServerStatus(`PromptServer 연결됨 · 프리셋 ${presets.length}개`);
      requestExtensionState();
    } catch (_) {
      setServerStatus(cached.length ? '서버 연결 실패 · 캐시 사용 중' : 'PromptServer 연결 대기 중 · 기존 랜덤 생성 유지', true);
      /* PromptServer may start after NAIA. Keep retrying while this panel is
         mounted so a recovered server repopulates the select automatically. */
      const delay = Math.min(5000, 1000 + Number(select.dataset.serverPresetsRetry || 0) * 500);
      select.dataset.serverPresetsRetry = String(Number(select.dataset.serverPresetsRetry || 0) + 1);
      setTimeout(() => {
        if (document.body.contains(select)) loadPresets(select);
      }, delay);
    } finally {
      delete select.dataset.serverPresetsLoading;
    }
  }
  function syncRow() {
    const row = document.getElementById(ROW_ID);
    if (!row) return;
    const input = row.querySelector('[data-server-enabled]');
    const preset = row.querySelector('[data-server-preset]');
    const markUsed = row.querySelector('[data-server-mark-used]');
    const includeUsed = row.querySelector('[data-server-include-used]');
    const ok = available();
    const enabled = Boolean(setting(ENABLED_KEY, false));
    // The feature switch controls behavior only. Keep this module UI visible
    // so it can be turned back on; the extension-level switch controls UI
    // visibility through `available()`.
    row.style.display = ok ? '' : 'none';
    if (input) { input.checked = ok && enabled; input.disabled = !ok; }
    if (preset) preset.value = String(setting(PRESET_KEY, RANDOM_PRESET));
    if (markUsed) markUsed.checked = asBoolean(setting(MARK_USED_KEY, true), true);
    if (includeUsed) includeUsed.checked = asBoolean(setting(INCLUDE_USED_KEY, false), false);
    const cached = cachedPresets();
    if (preset && cached.length && preset.options.length <= 2) updatePresetOptions(preset, cached);
    const serverStatus = String(setting(SERVER_STATUS_KEY, '') || '').trim();
    if (serverStatus) setServerStatus(serverStatus, /실패|대기/.test(serverStatus));
    [preset, markUsed, includeUsed].forEach(el => { if (el) el.disabled = !ok || !enabled; });
    row.classList.toggle('naia-exten-unavailable', !ok);
    if (!ok) requestExtensionState();
  }
  function ensureRow() {
    if (typeof currentModuleId === 'undefined' || currentModuleId !== 'prompt_engineering') return;
    if (typeof moduleBody === 'undefined' || !moduleBody) return;
    installStyle();
    let row = document.getElementById(ROW_ID);
    if (!row) {
      row = document.createElement('div');
      row.id = ROW_ID;
      row.innerHTML = `
        <div class="naia-exten-server-title"><span>서버 랜덤 프롬프트</span><label class="naia-exten-server-switch" title="PromptServer 랜덤 상황 사용"><input type="checkbox" data-server-enabled aria-label="서버 랜덤 프롬프트"><span class="naia-exten-server-track"></span></label></div>
        <div class="naia-exten-server-help">char의 girl/boy 태그로 인원 수를 자동 판별해 PromptServer 상황을 가져옵니다.</div>
        <div class="naia-exten-server-status" data-server-status aria-live="polite">PromptServer 상태 확인 중</div>
        <div class="naia-exten-server-grid"><label>프리셋<select data-server-preset aria-label="서버 랜덤 프롬프트 프리셋"><option value="${RANDOM_PRESET}">랜덤</option></select></label></div>
        <div class="naia-exten-server-options"><label><input type="checkbox" data-server-mark-used aria-label="조회 시 사용 처리"> 조회 시 사용 처리</label><label><input type="checkbox" data-server-include-used aria-label="사용한 상황 포함"> 사용한 상황 포함</label></div>`;
      moduleBody.appendChild(row);
      const enabled = row.querySelector('[data-server-enabled]');
      const preset = row.querySelector('[data-server-preset]');
      const markUsed = row.querySelector('[data-server-mark-used]');
      const includeUsed = row.querySelector('[data-server-include-used]');
      enabled.addEventListener('change', () => { setSetting(ENABLED_KEY, Boolean(enabled.checked)); syncRow(); });
      preset.addEventListener('change', () => setSetting(PRESET_KEY, preset.value || RANDOM_PRESET));
      markUsed.addEventListener('change', () => setSetting(MARK_USED_KEY, Boolean(markUsed.checked)));
      includeUsed.addEventListener('change', () => setSetting(INCLUDE_USED_KEY, Boolean(includeUsed.checked)));
      void loadPresets(preset);
    }
    const currentPreset = row.querySelector('[data-server-preset]');
    if (currentPreset && currentPreset.dataset.serverPresetsLoaded !== 'true') void loadPresets(currentPreset);
    syncRow();
  }
  // Prompt Engineering replaces moduleBody's direct children on render. Watch
  // only that boundary: subtree:false deliberately ignores custom-select DOM
  // updates, and avoiding renderPromptEngineering monkey-patching keeps the host
  // renderer intact when the extension is disabled and enabled again.
  if (typeof moduleBody !== 'undefined' && moduleBody) {
    new MutationObserver(() => queueMicrotask(ensureRow))
      .observe(moduleBody, {childList: true});
  }
  queueMicrotask(ensureRow);
  setTimeout(ensureRow, 250);
  setTimeout(ensureRow, 1000);
})();
'''

    def __init__(self):
        super().__init__()
        self._context = None
        self._last_server_log: dict[str, float] = {}
        self._last_preset_refresh = 0.0
        self._hook = None

    def panel_fields(self) -> list[dict]:
        # The controls live in Prompt Engineering, but extension setting writes
        # are accepted only for fields declared through the official panel API.
        hidden = {
            "field": "__naia_exten_internal_never__",
            "in": ["1"],
        }
        return [
            {
                "key": "preset",
                "type": "text",
                "default": self.RANDOM_PRESET,
                "label": "서버 랜덤 프롬프트 프리셋",
                "visible_when": hidden,
            },
            {
                "key": "mark_used",
                "type": "bool",
                "default": True,
                "label": "서버 랜덤 프롬프트 사용 처리",
                "visible_when": hidden,
            },
            {
                "key": "include_used",
                "type": "bool",
                "default": False,
                "label": "서버 랜덤 프롬프트 사용 항목 포함",
                "visible_when": hidden,
            },
            {
                "key": self.PRESET_OPTIONS_KEY,
                "type": "text",
                "default": "[]",
                "label": "서버 랜덤 프롬프트 프리셋 캐시",
                "visible_when": hidden,
            },
            {
                "key": self.SERVER_STATUS_KEY,
                "type": "text",
                "default": "PromptServer 연결 대기 중",
                "label": "서버 랜덤 프롬프트 상태",
                "visible_when": hidden,
            },
        ]

    def register(self) -> None:
        self._context = self.ext.host.app_context
        if self._context is None:
            self.ctx.log("서버 랜덤 프롬프트: NAIA context를 찾지 못했습니다.")
            return

        from core.headless_random_prompt_service import HeadlessRandomPromptService
        import core.character_settings as character_settings

        self.ext.patches.wrap_method(
            owner=self.id,
            target=HeadlessRandomPromptService,
            method_name="generate",
            replace=self._generate,
        )
        if callable(getattr(HeadlessRandomPromptService, "_apply_character_settings", None)):
            self.ext.patches.wrap_method(
                owner=self.id,
                target=HeadlessRandomPromptService,
                method_name="_apply_character_settings",
                replace=self._apply_character_settings,
            )
        self.ext.patches.wrap_method(
            owner=self.id,
            target=character_settings,
            method_name="character_params_from_settings",
            replace=self._character_params,
        )
        self._register_pipeline_hook()
        try:
            self.ext.patches.add_web_injection(
                owner=self.id,
                file_name="app.js",
                marker=self._PANEL_JS_MARKER,
                content=self._PANEL_JS,
            )
        except Exception as exc:
            self.ctx.log(f"서버 랜덤 프롬프트 UI unavailable: {exc}")
        self._refresh_preset_cache(force=True)

    def unregister(self) -> None:
        if self._context is not None and getattr(self._context, self.FEATURE_ATTR, None) is self:
            setattr(self._context, self.FEATURE_ATTR, None)
        self._context = None

    def _register_pipeline_hook(self) -> None:
        hook = getattr(self._context, self.HOOK_ATTR, None)
        if hook is None:
            hook = _ServerRandomPromptHook(self._context)
            register_hook = getattr(self.ctx, "register_hook", None)
            if not callable(register_hook):
                self.ctx.log("서버 랜덤 프롬프트: 공식 PromptProcessor hook을 사용할 수 없습니다.")
                return
            register_hook(hook)
            setattr(self._context, self.HOOK_ATTR, hook)
        self._hook = hook
        setattr(self._context, self.FEATURE_ATTR, self)

    def _runtime_active(self) -> bool:
        if not self.is_enabled():
            return False
        record = getattr(self.ctx, "_record", None)
        if record is None:
            return True
        try:
            return bool(record.is_active)
        except Exception:
            return False

    def _settings(self) -> dict[str, Any]:
        values = self.load_settings({
            "preset": self.RANDOM_PRESET,
            "mark_used": True,
            "include_used": False,
        })
        return values

    @staticmethod
    def _bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off", ""}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    def _generate(self, original, service, *args, **kwargs):
        if not self._runtime_active():
            return original(service, *args, **kwargs)

        settings = self._settings()
        mark_used = self._bool(settings.get(self.key("mark_used"), True), True)
        include_used = self._bool(settings.get(self.key("include_used"), False), False)
        pending = {
            "preset": settings.get(self.key("preset"), self.RANDOM_PRESET),
            "mark_used": mark_used,
            "include_used": include_used,
            "fetched": False,
            "response_token": None,
        }

        app_context = getattr(service, "context", None) or self._context
        had_previous = app_context is not None and hasattr(app_context, self.RESPONSE_ATTR)
        previous = getattr(app_context, self.RESPONSE_ATTR, None) if had_previous else None
        if app_context is not None:
            # Keep an explicit None during this run.  If the server lookup failed,
            # character resolution must not fall back to the previous prompt
            # context's successful response before generate() installs a new one.
            setattr(app_context, self.RESPONSE_ATTR, None)
        response_token = _ACTIVE_SERVER_RESPONSE.set(None)
        pending_token = _PENDING_SERVER_REQUEST.set(pending)
        try:
            return original(service, *args, **kwargs)
        finally:
            nested_response_token = pending.get("response_token")
            if nested_response_token is not None:
                _ACTIVE_SERVER_RESPONSE.reset(nested_response_token)
            _PENDING_SERVER_REQUEST.reset(pending_token)
            _ACTIVE_SERVER_RESPONSE.reset(response_token)
            if app_context is not None:
                if had_previous:
                    setattr(app_context, self.RESPONSE_ATTR, previous)
                else:
                    try:
                        delattr(app_context, self.RESPONSE_ATTR)
                    except AttributeError:
                        pass

    def _fetch_server_prompt(
        self,
        male_count: int,
        female_count: int,
        preset: Any,
        *,
        mark_used: bool = True,
        include_used: bool = False,
    ) -> dict[str, Any] | None:
        query = [
            ("male_count", str(male_count)),
            ("female_count", str(female_count)),
            ("mark_used", str(bool(mark_used)).lower()),
            ("include_used", str(bool(include_used)).lower()),
        ]
        preset_value = str(preset or "").strip()
        if preset_value and preset_value != self.RANDOM_PRESET:
            query.append(("preset", preset_value))
        url = f"{self.SERVER_BASE}/api/scenarios/random?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.REQUEST_TIMEOUT) as response:
                if int(getattr(response, "status", 200) or 200) != 200:
                    return None
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("character_prompts"), dict):
                self._log_server_issue("invalid response")
                return None
            self._set_server_status("PromptServer 연결됨")
            # Keep the request limits with the response so late character
            # resolution cannot accidentally consume keys beyond the counts
            # selected in the Prompt Engineering panel.
            result = dict(payload)
            result[self._REQUESTED_MALE_COUNT_KEY] = male_count
            result[self._REQUESTED_FEMALE_COUNT_KEY] = female_count
            return result
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self._set_server_status("PromptServer 연결됨 — 조건에 맞는 상황 없음")
            else:
                self._log_server_issue(f"HTTP {exc.code}")
                self._set_server_status(f"PromptServer 오류 (HTTP {exc.code})")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._log_server_issue(type(exc).__name__)
            self._set_server_status("PromptServer 연결 실패 — 기존 랜덤 생성 유지")
        return None

    def _fetch_presets(self) -> list[dict[str, str]] | None:
        request = urllib.request.Request(
            f"{self.SERVER_BASE}/api/presets",
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.REQUEST_TIMEOUT) as response:
                if int(getattr(response, "status", 200) or 200) != 200:
                    return None
                payload = json.loads(response.read().decode("utf-8"))
            raw_items = payload if isinstance(payload, list) else payload.get("presets", [])
            if not isinstance(raw_items, list):
                return None
            items = []
            for item in raw_items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                items.append({"id": str(item["id"]), "name": str(item.get("name") or item["id"])})
            return items
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _set_server_status(self, status: str) -> None:
        """Persist a small runtime status so the injected UI can explain failures."""
        try:
            current = self.ctx.load_settings({})
            if current.get(self.key(self.SERVER_STATUS_KEY)) == status:
                return
            current[self.key(self.SERVER_STATUS_KEY)] = status
            saver = getattr(self.ctx, "save_settings", None)
            if callable(saver):
                saver(current)
        except Exception:
            pass

    def _refresh_preset_cache(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_preset_refresh < 5.0:
            return
        self._last_preset_refresh = now
        presets = self._fetch_presets()
        if presets is None:
            self._set_server_status("PromptServer 연결 대기 중 — 프리셋 재시도 중")
            return
        encoded = json.dumps(presets, ensure_ascii=False, separators=(",", ":"))
        try:
            current = self.ctx.load_settings({})
            key = self.key(self.PRESET_OPTIONS_KEY)
            if current.get(key) == encoded:
                self._set_server_status("PromptServer 연결됨")
                return
            current[key] = encoded
            saver = getattr(self.ctx, "save_settings", None)
            if callable(saver):
                saver(current)
            self._set_server_status("PromptServer 연결됨")
        except Exception:
            pass

    def _log_server_issue(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_server_log.get(reason, 0.0) < 10.0:
            return
        self._last_server_log[reason] = now
        self.ctx.log(f"서버 랜덤 프롬프트: PromptServer 사용 불가({reason}) — 기존 랜덤 생성을 계속합니다.")

    def _append_base_prompt(self, context):
        if not self._runtime_active() or self._context is None:
            return context
        active_response = _ACTIVE_SERVER_RESPONSE.get()
        response = (
            active_response
            if active_response is not _NO_ACTIVE_RESPONSE
            else getattr(self._context, self.RESPONSE_ATTR, None)
        )
        if not isinstance(response, dict):
            return context
        metadata = getattr(context, "metadata", None)
        if isinstance(metadata, dict):
            # Character resolution can happen after this final hook.  Keep the
            # same successful response available to that late call even when
            # the server scenario has no base_prompt.
            metadata[self.CONTEXT_RESPONSE_KEY] = response
        if getattr(context, self.APPLIED_ATTR, None) == id(response):
            return context

        main_tags = getattr(context, "main_tags", None)
        if not isinstance(main_tags, list):
            try:
                main_tags = list(main_tags or [])
            except (TypeError, ValueError):
                main_tags = []
            setattr(context, "main_tags", main_tags)
        base_prompt = str(response.get("base_prompt") or "").strip()
        try:
            from core.wildcard_processor import split_tags_smart
            tags = [str(tag).strip() for tag in split_tags_smart(base_prompt) if str(tag).strip()]
        except Exception:
            tags = [base_prompt]
        try:
            from core.wildcard_processor import WildcardProcessor
            wildcard_manager = getattr(self._context, "wildcard_manager", None)
            if wildcard_manager is not None:
                tags = WildcardProcessor(wildcard_manager).expand_tags(
                    tags, context, location="postfix"
                )
        except Exception:
            pass

        # A successful server response replaces the random source scene in
        # main_tags.  Keep tags introduced by later pipeline stages (and the
        # normal prefix/postfix quality/artist additions) by removing only the
        # pre-server scene snapshot from the current main list.  This also
        # removes the old scene when the server intentionally returns no base.
        baseline = metadata.get("boost_main_tags") if isinstance(metadata, dict) else None
        if not isinstance(baseline, list):
            try:
                from core.wildcard_processor import split_tags_smart
                baseline = [
                    str(tag).strip()
                    for tag in split_tags_smart(
                        str(getattr(context, "source_row", {}).get("general") or "")
                    )
                    if str(tag).strip()
                ]
            except Exception:
                baseline = []
        remaining = {}
        for raw_tag in baseline:
            tag = str(raw_tag).strip()
            if tag:
                remaining[tag] = remaining.get(tag, 0) + 1
        preserved = []
        for raw_tag in main_tags:
            tag = str(raw_tag).strip()
            if tag == self.SERVER_MARKER:
                continue
            if remaining.get(tag, 0):
                remaining[tag] -= 1
                continue
            preserved.append(raw_tag)
        global_append_tags = getattr(context, "global_append_tags", None)
        if isinstance(global_append_tags, list):
            preserved.extend(global_append_tags)
            global_append_tags.clear()
        postfix = getattr(context, "postfix_tags", None)
        if not isinstance(postfix, list):
            try:
                postfix = list(postfix or [])
            except (TypeError, ValueError):
                postfix = []
            setattr(context, "postfix_tags", postfix)
        postfix[:] = [
            self.SERVER_MARKER,
            *tags,
            *preserved,
            *[tag for tag in postfix if str(tag).strip() != self.SERVER_MARKER],
        ]
        main_tags.clear()
        self._prepend_person_count_tags(context, response)
        setattr(context, self.APPLIED_ATTR, id(response))
        return context

    def _prepend_person_count_tags(self, context, response: dict[str, Any]) -> None:
        """Expose the server-selected character counts without core's random marker."""
        prefix = getattr(context, "prefix_tags", None)
        if not isinstance(prefix, list):
            try:
                prefix = list(prefix or [])
            except (TypeError, ValueError):
                prefix = []
            setattr(context, "prefix_tags", prefix)
        count_tags = []
        for gender, singular, plural in (
            ("male", "boy", "boys"),
            ("female", "girl", "girls"),
        ):
            count = self._response_count_limit(response, gender)
            if not count:
                continue
            if count >= 6:
                count_tags.append(f"6+{plural}")
            elif count == 1:
                count_tags.append(f"1{singular}")
            else:
                count_tags.append(f"{count}{plural}")
        existing = {str(tag).strip().lower() for tag in prefix}
        prefix[:0] = [tag for tag in count_tags if tag.lower() not in existing]

    def _apply_character_settings(self, original, service, settings, *args, **kwargs):
        apply_token = _APPLYING_CHARACTER_SETTINGS.set(True)
        try:
            result = original(service, settings, *args, **kwargs)
        finally:
            _APPLYING_CHARACTER_SETTINGS.reset(apply_token)
        app_context = getattr(service, "context", None) or self._context
        pending = _PENDING_SERVER_REQUEST.get()
        if isinstance(pending, dict) and not pending.get("fetched"):
            pending["fetched"] = True
            characters = settings.get("characters") if isinstance(settings, dict) else None
            # When "Process wildcards on Generate" is enabled, the host defers
            # character expansion until Generate.  The server lookup still needs
            # the resolved character genders now (e.g. __test__, __test__ ->
            # girl, girl -> female_count=2), so resolve a per-request copy only
            # when the host did not provide character params at all.
            if isinstance(settings, dict) and not isinstance(characters, list):
                resolved = self._resolve_character_params_for_server(app_context)
                if isinstance(resolved, dict) and isinstance(resolved.get("characters"), list):
                    settings["characters"] = list(resolved["characters"])
                    for key in ("uc", "character_ids"):
                        value = resolved.get(key)
                        if isinstance(value, list):
                            settings[key] = list(value)
                    characters = settings["characters"]
            male_count, female_count = self._character_gender_counts(characters)
            response = None
            if male_count + female_count:
                response = self._fetch_server_prompt(
                    male_count,
                    female_count,
                    pending.get("preset", self.RANDOM_PRESET),
                    mark_used=bool(pending.get("mark_used", True)),
                    include_used=bool(pending.get("include_used", False)),
                )
            if app_context is not None:
                setattr(app_context, self.RESPONSE_ATTR, response)
            pending["response_token"] = _ACTIVE_SERVER_RESPONSE.set(response)
        tracked_response = self._persisted_response(app_context)
        active_response = _ACTIVE_SERVER_RESPONSE.get()
        response = (
            active_response
            if active_response is not _NO_ACTIVE_RESPONSE
            else getattr(app_context, self.RESPONSE_ATTR, None)
        )
        if not isinstance(settings, dict):
            return result
        if not self._runtime_active() or not isinstance(response, dict):
            if isinstance(tracked_response, dict):
                self._cleanup_stale_snapshot(service, settings, tracked_response)
            return result
        characters = settings.get("characters")
        if not isinstance(characters, list):
            return result
        previous_response = self._previous_context_response(service)
        if isinstance(previous_response, dict) and previous_response is not response:
            characters = self._strip_previous_character_additions(characters, previous_response)
            settings["characters"] = characters
        updated = self._map_character_prompts(characters, response)
        if updated is not None:
            # Keep the host character frames pristine; the official runtime
            # snapshot is refreshed below so Character panel state follows the
            # same per-run overlay.
            settings["characters"] = updated
        if isinstance(response.get("character_prompts"), dict) and settings.get("characters"):
            self._store_server_character_snapshot(service, settings)
        return result

    @classmethod
    def _character_gender_counts(cls, characters: Any) -> tuple[int, int]:
        if not isinstance(characters, list):
            return 0, 0
        try:
            from core.wildcard_processor import split_tags_smart
        except Exception:
            split_tags_smart = lambda text: str(text).split(",")
        counts = {"male": 0, "female": 0}
        for raw_character in characters:
            tags = [str(tag).strip().lower() for tag in split_tags_smart(str(raw_character or ""))]
            spec = cls._gender_spec(tags)
            if spec is None:
                continue
            gender, count = spec
            counts[gender] = min(20, counts[gender] + count)
        return counts["male"], counts["female"]

    def _resolve_character_params_for_server(self, app_context) -> dict[str, Any] | None:
        """Resolve deferred character wildcards for the server count lookup."""
        try:
            from core.character_settings import character_params_from_settings

            mode_getter = getattr(app_context, "get_api_mode", None)
            mode = mode_getter() if callable(mode_getter) else "NAI"
            result = character_params_from_settings(
                app_context,
                mode=mode,
                reuse_current_context=True,
                prefer_snapshot=False,
            )
            return result if isinstance(result, dict) else None
        except Exception:
            # Server integration must not break the normal random prompt path if
            # an older host lacks the canonical character-settings helper.
            return None

    def _character_params(self, original, app_context, *args, **kwargs):
        result = original(app_context, *args, **kwargs)
        if _APPLYING_CHARACTER_SETTINGS.get():
            return result
        if not isinstance(result, dict):
            return result
        if hasattr(app_context, self.RESPONSE_ATTR):
            response = getattr(app_context, self.RESPONSE_ATTR, None)
        else:
            current_context = getattr(app_context, "current_prompt_context", None)
            metadata = getattr(current_context, "metadata", None)
            response = (
                metadata.get(self.CONTEXT_RESPONSE_KEY)
                if isinstance(metadata, dict)
                else None
            )
        tracked_response = self._persisted_response(app_context)
        if not self._runtime_active() or not isinstance(response, dict):
            if isinstance(tracked_response, dict) and isinstance(result.get("characters"), list):
                cleaned = self._strip_previous_character_additions(
                    result["characters"], tracked_response
                )
                output = dict(result)
                output["characters"] = cleaned
                self._store_character_snapshot(app_context, output)
                self._clear_persisted_response(app_context)
                return output
            return result
        characters = result.get("characters")
        if not isinstance(characters, list):
            return result

        previous_response = tracked_response
        cleaned = (
            self._strip_previous_character_additions(characters, previous_response)
            if isinstance(previous_response, dict) and previous_response is not response
            else list(characters)
        )
        updated = self._map_character_prompts(cleaned, response)
        if updated is None:
            if cleaned != characters and isinstance(response.get("character_prompts"), dict):
                output = dict(result)
                output["characters"] = cleaned
                setattr(app_context, self.PERSISTED_RESPONSE_ATTR, response)
                return output
            return result
        output = dict(result)
        output["characters"] = updated
        setattr(app_context, self.PERSISTED_RESPONSE_ATTR, response)
        return output

    def _previous_context_response(self, service):
        app_context = getattr(service, "context", None) or self._context
        return self._persisted_response(app_context)

    def _persisted_response(self, app_context):
        tracked = getattr(app_context, self.PERSISTED_RESPONSE_ATTR, None)
        if isinstance(tracked, dict):
            return tracked
        current_context = getattr(app_context, "current_prompt_context", None)
        metadata = getattr(current_context, "metadata", None)
        return metadata.get(self.CONTEXT_RESPONSE_KEY) if isinstance(metadata, dict) else None

    def _store_server_character_snapshot(self, service, settings: dict[str, Any]) -> None:
        app_context = getattr(service, "context", None) or self._context
        if app_context is None:
            return
        if self._store_character_snapshot(app_context, settings):
            setattr(app_context, self.PERSISTED_RESPONSE_ATTR, self._current_response_for_context(app_context))

    def _store_character_snapshot(self, app_context, settings: dict[str, Any]) -> bool:
        if app_context is None:
            return False
        mode_getter = getattr(app_context, "get_api_mode", None)
        try:
            mode = mode_getter() if callable(mode_getter) else "NAI"
            from core.character_settings import (
                clear_character_roll_snapshot,
                store_character_roll_snapshot,
            )
            params = {
                "characters": list(settings.get("characters") or []),
                "uc": list(settings.get("uc") or []),
                "character_ids": list(settings.get("character_ids") or []),
            }
            if params["characters"]:
                return store_character_roll_snapshot(app_context, params, mode) is not None
            clear_character_roll_snapshot(app_context, mode)
            return True
        except Exception:
            # Snapshot storage is a UI/runtime convenience; generation must not
            # fail if an older host omits the optional helper.
            return False

    def _cleanup_stale_snapshot(self, service, settings, response) -> None:
        characters = settings.get("characters")
        if isinstance(characters, list):
            settings["characters"] = self._strip_previous_character_additions(
                characters, response
            )
        app_context = getattr(service, "context", None) or self._context
        self._store_character_snapshot(app_context, settings)
        self._clear_persisted_response(app_context)

    def _clear_persisted_response(self, app_context) -> None:
        try:
            delattr(app_context, self.PERSISTED_RESPONSE_ATTR)
        except AttributeError:
            pass

    def _current_response_for_context(self, app_context):
        active = _ACTIVE_SERVER_RESPONSE.get()
        if active is not _NO_ACTIVE_RESPONSE:
            return active
        if hasattr(app_context, self.RESPONSE_ATTR):
            return getattr(app_context, self.RESPONSE_ATTR, None)
        current_context = getattr(app_context, "current_prompt_context", None)
        metadata = getattr(current_context, "metadata", None)
        return metadata.get(self.CONTEXT_RESPONSE_KEY) if isinstance(metadata, dict) else None

    @staticmethod
    def _strip_previous_character_additions(
        characters: list[Any], response: dict[str, Any]
    ) -> list[str]:
        prompts = response.get("character_prompts")
        if not isinstance(prompts, dict):
            return list(characters)
        additions = sorted(
            {str(value).strip() for value in prompts.values() if str(value or "").strip()},
            key=len,
            reverse=True,
        )
        if not additions:
            return list(characters)
        cleaned = []
        for raw_character in characters:
            prompt = str(raw_character or "").rstrip()
            while prompt:
                matched = False
                for addition in additions:
                    if prompt == addition:
                        prompt = ""
                        matched = True
                        break
                    suffix = ", " + addition
                    if prompt.endswith(suffix):
                        prompt = prompt[: -len(suffix)].rstrip()
                        matched = True
                        break
                if not matched:
                    break
            cleaned.append(prompt)
        return cleaned

    @classmethod
    def _map_character_prompts(
        cls, characters: list[Any], response: dict[str, Any]
    ) -> list[str] | None:
        prompts = response.get("character_prompts")
        if not isinstance(prompts, dict):
            return None

        try:
            from core.wildcard_processor import split_tags_smart
        except Exception:
            split_tags_smart = lambda text: str(text).split(",")

        requested_limits = {
            "male": cls._response_count_limit(response, "male"),
            "female": cls._response_count_limit(response, "female"),
        }
        available = cls._available_character_prompts(prompts, requested_limits)
        next_index = {"male": 1, "female": 1}
        explicit: list[tuple[str, int] | None] = []
        for raw_prompt in characters:
            tags = [str(tag).strip().lower() for tag in split_tags_smart(str(raw_prompt or ""))]
            explicit.append(cls._gender_spec(tags))

        # First reserve explicit gender slots.  This prevents an untagged slot
        # earlier in the list from stealing a response intended for a later
        # explicitly tagged slot.
        assignments: dict[int, list[str]] = {}
        used_keys: set[str] = set()
        for index, spec in enumerate(explicit):
            if spec is None:
                continue
            gender, count = spec
            additions = []
            for _ in range(count):
                key = f"{gender}{next_index[gender]}"
                next_index[gender] += 1
                addition = available.get(key)
                if addition:
                    additions.append(addition)
                    used_keys.add(key)
            assignments[index] = additions

        # Then fill untagged/ambiguous slots in stable server-key order.
        fallback_keys = [
            key for key in available
            if key not in used_keys
        ]
        fallback_index = 0
        for index, spec in enumerate(explicit):
            if spec is not None:
                continue
            if fallback_index >= len(fallback_keys):
                assignments[index] = []
                continue
            key = fallback_keys[fallback_index]
            fallback_index += 1
            assignments[index] = [available[key]]

        changed = False
        updated = list(characters)
        for index, additions in assignments.items():
            prompt = str(characters[index] or "").rstrip()
            for addition in additions:
                if not addition or prompt == addition or prompt.endswith(", " + addition):
                    continue
                prompt = f"{prompt}, {addition}" if prompt else addition
                changed = True
            updated[index] = prompt

        if not changed:
            return None
        return updated

    @classmethod
    def _gender_spec(cls, tags: list[str]) -> tuple[str, int] | None:
        for tag in tags:
            match = cls._GENDER_TAG_RE.fullmatch(tag)
            if not match:
                continue
            raw_count = match.group("count") or "1"
            # `6+girls` means the six-character bucket; do not let the plus
            # form make the server response disappear from the slot.
            count = int(raw_count.rstrip("+"))
            return ("male" if match.group("gender").lower().startswith("boy") else "female", max(1, count))
        return None

    @classmethod
    def _response_count_limit(cls, response: dict[str, Any], gender: str) -> int | None:
        key = (
            cls._REQUESTED_MALE_COUNT_KEY
            if gender == "male"
            else cls._REQUESTED_FEMALE_COUNT_KEY
        )
        value = response.get(key)
        try:
            return max(0, min(20, int(value))) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _available_character_prompts(
        prompts: dict[str, Any], limits: dict[str, int | None]
    ) -> dict[str, str]:
        found: dict[tuple[str, int], str] = {}
        key_re = re.compile(r"^(male|female)(\d+)$", re.IGNORECASE)
        for raw_key, raw_value in prompts.items():
            match = key_re.fullmatch(str(raw_key).strip())
            value = str(raw_value or "").strip()
            if not match or not value:
                continue
            gender = match.group(1).lower()
            index = int(match.group(2))
            limit = limits.get(gender)
            if index < 1 or (limit is not None and index > limit):
                continue
            found[(gender, index)] = value
        return {
            f"{gender}{index}": found[(gender, index)]
            for gender in ("male", "female")
            for index in sorted(index for g, index in found if g == gender)
        }
