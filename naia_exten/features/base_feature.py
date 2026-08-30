from __future__ import annotations

from typing import Any


class BaseFeature:
    """
    Base class for one independent convenience feature.

    Features are registered once with NAIA. Runtime enable/disable is read from
    the host-managed settings.json, so callbacks/hooks can turn into no-ops
    instantly without needing unsupported settings-change callbacks.
    """

    id = ""
    name = "Unnamed Feature"
    description = ""
    category = "General"
    order = 100
    default_enabled = False
    panel_toggle_visible = True

    def __init__(self):
        self.ext = None

    @property
    def ctx(self):
        return self.ext.ctx

    @property
    def enabled_key(self) -> str:
        return f"feature__{self.id}__enabled"

    def key(self, local_key: str) -> str:
        return f"feature__{self.id}__{local_key}"

    def _attach(self, extension) -> None:
        self.ext = extension

    def register(self) -> None:
        """
        Register event subscriptions/hooks once.

        For callbacks and hooks, check self.is_enabled() and return without
        changing anything when disabled.
        """

    def unregister(self) -> None:
        """Optional cleanup hook used before development hot reload."""

    def is_enabled(self) -> bool:
        settings = self.ctx.load_settings(
            {self.enabled_key: self.default_enabled}
        )
        return bool(settings.get(self.enabled_key, self.default_enabled))

    def load_settings(self, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = {
            self.enabled_key: self.default_enabled,
        }
        for key, value in (defaults or {}).items():
            merged[self.key(key)] = value
        return self.ctx.load_settings(merged)

    def value(self, local_key: str, default=None):
        settings = self.ctx.load_settings(
            {self.key(local_key): default}
        )
        return settings.get(self.key(local_key), default)

    def panel_fields(self) -> list[dict]:
        """
        Return fields using LOCAL keys.

        Example:
            {
              "key": "count",
              "type": "int",
              "default": 3,
              "label": "생성 수",
              "visible_when": {"field": "__enabled__", "in": [True]}
            }

        FeatureManager prefixes the keys automatically.
        """

        return []

    def normalize_panel_field(self, raw: dict, *, index: int = 1) -> dict:
        field = dict(raw or {})
        local_key = str(field.get("key") or "").strip()
        if not local_key:
            raise ValueError(f"{self.id}: panel field key is empty")

        field["key"] = self.key(local_key)
        field.setdefault("section", self.category)
        field.setdefault("order", self.order * 100 + index)
        field.setdefault("scope", "module")

        visible = field.get("visible_when")
        if isinstance(visible, dict):
            visible = dict(visible)
            controller = str(visible.get("field") or "").strip()
            if controller == "__enabled__":
                visible["field"] = self.enabled_key
            elif controller and not controller.startswith("feature__"):
                visible["field"] = self.key(controller)
            field["visible_when"] = visible

        return field

    def handle_action(self, full_key: str) -> None:
        """Override for action fields."""
