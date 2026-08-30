from __future__ import annotations

from pathlib import Path

from .feature_manager import FeatureManager
from .hot_reload import FeatureHotReloadWatcher
from .patch_manager import PatchManager
from .host_bridge import HostBridge


class NAIAExten:
    """Integrated extension runtime with safe feature-level development hot reload."""

    PANEL_TITLE = "NAIA 추가 편의기능"

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
