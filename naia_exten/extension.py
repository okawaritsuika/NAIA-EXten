from __future__ import annotations

import json
from pathlib import Path

from .feature_manager import FeatureManager
from .hot_reload import FeatureHotReloadWatcher
from .patch_manager import PatchManager
from .host_bridge import HostBridge


class NAIAExten:
    """Integrated extension runtime with safe feature-level development hot reload."""

    PANEL_TITLE = "NAIA 추가 편의기능"

    PANEL_LAYOUT_CSS_MARKER = "/* NAIA_EXTEN_PANEL_TOGGLE_LAYOUT_V3 */"
    PANEL_LAYOUT_JS_MARKER = "/* NAIA_EXTEN_PANEL_TOGGLE_LAYOUT_RUNTIME_V3 */"
    PANEL_LAYOUT_STYLE_ID = "naiaExtenPanelToggleLayoutV3"
    PANEL_LAYOUT_CSS = r'''/* NAIA_EXTEN_PANEL_TOGGLE_LAYOUT_V3 */
/* Keep EXten's four visible feature toggles on one clean label/switch row.
   Select by extension + field key so NAIA's own extension UI is untouched. */
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__character_same_wildcard__enabled),
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__server_random_prompt__enabled),
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__multi_parquet_pool__enabled),
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__gsqe_probability__enabled) {
  display: grid !important;
  grid-template-columns: minmax(0, 1fr) auto !important;
  align-items: center !important;
  column-gap: 10px !important;
}
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__character_same_wildcard__enabled) > label,
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__server_random_prompt__enabled) > label,
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__multi_parquet_pool__enabled) > label,
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__gsqe_probability__enabled) > label {
  display: inline-flex !important;
  align-items: center;
  gap: 4px;
  min-width: 0;
  white-space: nowrap !important;
}
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__character_same_wildcard__enabled) > input[type="checkbox"],
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__server_random_prompt__enabled) > input[type="checkbox"],
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__multi_parquet_pool__enabled) > input[type="checkbox"],
.ext-quick-popup .ext-field:has(> #extquick-naia_exten-feature__gsqe_probability__enabled) > input[type="checkbox"] {
  grid-column: 2 !important;
  justify-self: end !important;
  margin: 0 !important;
}
.ext-quick-popup .naia-exten-settings-toggle {
  width: 100%;
  min-height: 28px;
  margin: 0 0 6px;
  padding: 4px 8px;
  border: 1px solid var(--border-dim);
  border-radius: 7px;
  background: var(--bg-elevated);
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 11px;
  font-weight: 700;
  text-align: left;
}
.ext-quick-popup .naia-exten-settings-toggle:hover {
  border-color: var(--accent);
  color: var(--text-primary);
}
.ext-quick-popup .naia-exten-settings-hidden {
  display: none !important;
}
'''

    @classmethod
    def _panel_layout_js(cls) -> str:
        css = json.dumps(cls.PANEL_LAYOUT_CSS, ensure_ascii=False)
        return f'''{cls.PANEL_LAYOUT_JS_MARKER}
(() => {{
  if (window.__naiaExtenPanelToggleLayoutV3) return;
  window.__naiaExtenPanelToggleLayoutV3 = true;

  const styleId = {json.dumps(cls.PANEL_LAYOUT_STYLE_ID)};
  let style = document.getElementById(styleId);
  if (!style) {{
    style = document.createElement('style');
    style.id = styleId;
    document.head.appendChild(style);
  }}
  style.textContent = {css};

  const COLLAPSIBLE_SECTIONS = new Set([
    'Development',
    'NAID4 Character',
    'Prompt Engineering',
    'Search / Parquet',
    'Tag Filter',
  ]);
  let settingsCollapsed = false;
  let popupAnchorRect = null;

  function applyFieldVisibility(fields) {{
    const columns = fields.classList.contains('ext-fields-two-col')
      ? [...fields.children].filter(child => child.classList.contains('ext-fields-col'))
      : [fields];
    columns.forEach(column => {{
      let hideSection = false;
      for (const child of column.children) {{
        if (child.classList.contains('ext-section')) {{
          const section = String(child.textContent || '').trim();
          hideSection = COLLAPSIBLE_SECTIONS.has(section);
        }}
        child.classList.toggle(
          'naia-exten-settings-hidden',
          settingsCollapsed && hideSection
        );
      }}
    }});
  }}

  function positionPopup(popup) {{
    if (!popupAnchorRect || popup.style.display === 'none') return;
    const rect = popupAnchorRect;
    const width = popup.offsetWidth;
    const height = popup.offsetHeight;
    let left = rect.left;
    let top = rect.bottom + 8;
    if (top + height > window.innerHeight - 8) {{
      top = Math.max(8, rect.top - height - 8);
    }}
    if (left + width > window.innerWidth - 8) {{
      left = Math.max(8, window.innerWidth - 8 - width);
    }}
    popup.style.left = `${{left}}px`;
    popup.style.top = `${{top}}px`;
  }}

  function applySettingsState(popup) {{
    const body = popup.querySelector('.ext-quick-body');
    const fields = body?.querySelector('.ext-fields');
    if (!body || !fields) return;

    let toggle = body.querySelector('.naia-exten-settings-toggle');
    if (!toggle) {{
      toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'naia-exten-settings-toggle';
      toggle.addEventListener('click', () => {{
        settingsCollapsed = !settingsCollapsed;
        applySettingsState(popup);
        requestAnimationFrame(() => positionPopup(popup));
      }});
      fields.insertAdjacentElement('beforebegin', toggle);
    }}

    const label = settingsCollapsed ? '▸ 설정 펼치기' : '▾ 설정 접기';
    if (toggle.textContent !== label) toggle.textContent = label;
    toggle.setAttribute('aria-expanded', String(!settingsCollapsed));
    applyFieldVisibility(fields);
    body.querySelectorAll('.ext-fields-note').forEach(note =>
      note.classList.toggle('naia-exten-settings-hidden', settingsCollapsed));
  }}

  function watchPopup() {{
    const popup = document.getElementById('extQuickPopup');
    if (!popup || popup.dataset.naiaExtenSettingsToggle === '1') return;
    popup.dataset.naiaExtenSettingsToggle = '1';
    new MutationObserver(() => queueMicrotask(() => {{
      applySettingsState(popup);
      requestAnimationFrame(() => positionPopup(popup));
    }}))
      .observe(popup, {{childList: true, subtree: true}});
    applySettingsState(popup);
  }}

  document.addEventListener('click', event => {{
    const anchor = event.target.closest(
      '.ext-tool-btn[data-ext="naia_exten"], .ext-launcher-item[data-ext="naia_exten"]'
    );
    if (anchor) popupAnchorRect = anchor.getBoundingClientRect();
  }}, true);

  watchPopup();
  new MutationObserver(watchPopup).observe(document.body, {{childList: true}});
}})();
'''

    HOT_RELOAD_KEY = "core__hot_reload_enabled"
    HOT_RELOAD_ACTION = "core__reload_features_now"

    def __init__(self, ctx):
        self.ctx = ctx
        self.patches = PatchManager()
        self.host = HostBridge(ctx)
        self.features = FeatureManager(self)

        features_root = Path(__file__).resolve().parent / "features"
        self._hot_reload = FeatureHotReloadWatcher(
            root=features_root,
            is_enabled=self._hot_reload_enabled,
            on_reload=self._auto_reload_features,
            log=self.ctx.log,
        )

    def register(self) -> None:
        self.features.discover()
        self.features.register_all()
        self.refresh_panel()
        self._hot_reload.start()

        # Panel metadata may depend on API mode/options in future features.
        # run_when_disarmed=True is an official UI-metadata use case in API v1.
        try:
            self.ctx.subscribe(
                "api_mode_changed",
                self._on_host_options_changed,
                run_when_disarmed=True,
            )
            self.ctx.subscribe(
                "api_options_refreshed",
                self._on_host_options_changed,
                run_when_disarmed=True,
            )
        except TypeError:
            # Compatibility fallback for older API-v1 hosts.
            self.ctx.subscribe("api_mode_changed", self._on_host_options_changed)
            self.ctx.subscribe("api_options_refreshed", self._on_host_options_changed)

        self.ctx.log(f"ready — {len(self.features.features)} feature(s) discovered")

    def _on_host_options_changed(self, _payload=None) -> None:
        self.refresh_panel()

    def _core_panel_fields(self) -> list[dict]:
        return [
            {
                "key": self.HOT_RELOAD_KEY,
                "type": "bool",
                "default": True,
                "label": "핫픽스 자동 반영",
                "help": (
                    "켜두면 naia_exten/features 아래 Python 파일을 저장한 뒤 "
                    "약 1초간 변경이 멈췄을 때 feature를 자동으로 다시 불러옵니다. "
                    "문법/import/register 오류가 나면 기존 정상 feature로 롤백합니다."
                ),
                "section": "Development",
                "order": 1,
                "apply": "immediate",
                "scope": "module",
            },
            {
                "key": self.HOT_RELOAD_ACTION,
                "type": "action",
                "label": "↻ Exten 지금 다시 불러오기",
                "help": (
                    "features 폴더의 코드를 즉시 다시 발견하고 등록합니다. "
                    "NAIA 전체 재시작은 하지 않습니다."
                ),
                "section": "Development",
                "order": 2,
                "scope": "module",
            },
        ]

    def refresh_panel(self) -> None:
        # This is extension-owned presentation only. Register it here (rather
        # than in a feature) so a feature hot reload restores it after
        # PatchManager clears and rebuilds the web injection set.
        self.patches.add_web_injection(
            owner="__naia_exten_panel_layout__",
            file_name="style.css",
            marker=self.PANEL_LAYOUT_CSS_MARKER,
            content=self.PANEL_LAYOUT_CSS,
        )
        # Install the same rule from app.js as well. Electron can retain an
        # older stylesheet in the current page, while this extension-owned
        # runtime style is appended after the host CSS on the normal reload.
        self.patches.add_web_injection(
            owner="__naia_exten_panel_layout__",
            file_name="app.js",
            marker=self.PANEL_LAYOUT_JS_MARKER,
            content=self._panel_layout_js(),
        )
        fields = self._core_panel_fields() + self.features.build_panel_fields()
        self.ctx.register_panel(
            fields=fields,
            title=self.PANEL_TITLE,
            on_action=self._handle_panel_action,
        )

    def _handle_panel_action(self, key: str) -> None:
        key = str(key)
        if key == self.HOT_RELOAD_ACTION:
            self.reload_features("수동 요청")
            return
        self.features.handle_action(key)

    def _hot_reload_enabled(self) -> bool:
        settings = self.ctx.load_settings({self.HOT_RELOAD_KEY: True})
        return bool(settings.get(self.HOT_RELOAD_KEY, True))

    def _auto_reload_features(self, reason: str) -> bool:
        return self.reload_features(f"자동 감지: {reason}")

    def reload_features(self, reason: str = "") -> bool:
        self.ctx.log(f"feature hot reload requested: {reason or 'manual'}")
        ok, message = self.features.reload_all()

        # Rebuild the panel even after rollback so action routing/metadata match
        # whichever feature set is currently alive.
        try:
            self.refresh_panel()
        except Exception as exc:
            self.ctx.log(f"panel refresh after hot reload failed: {exc}")

        if ok:
            self.ctx.log(f"feature hot reload OK: {message}")
            try:
                self.ctx.show_toast("NAIA 추가 편의기능 핫픽스 반영 완료", "success")
            except Exception:
                pass
            return True

        self.ctx.log(f"feature hot reload cancelled/rolled back: {message}")
        try:
            self.ctx.show_toast(
                f"핫픽스 반영 실패 — 기존 코드 유지: {message}",
                "error",
            )
        except Exception:
            pass
        return False
