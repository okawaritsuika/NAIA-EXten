from __future__ import annotations

import importlib
import pkgutil
import sys
import threading
import traceback
from collections import OrderedDict
from pathlib import Path

from .features.base_feature import BaseFeature


class FeatureManager:
    """Discovers, registers, exposes, and hot-reloads independent EXten features."""

    FEATURE_MODULE_PREFIX = "naia_exten.features."
    BASE_MODULE_NAME = "naia_exten.features.base_feature"

    def __init__(self, extension):
        self.ext = extension
        self.features: "OrderedDict[str, BaseFeature]" = OrderedDict()
        self.errors: dict[str, str] = {}
        self._action_owner: dict[str, BaseFeature] = {}
        self._discovered = False
        self._reload_lock = threading.RLock()

    def discover(self) -> None:
        if self._discovered:
            return

        import naia_exten.features as features_package

        prefix = features_package.__name__ + "."

        for info in pkgutil.iter_modules(features_package.__path__, prefix):
            short = info.name.rsplit(".", 1)[-1]
            if short == "base_feature" or short.startswith("_"):
                continue

            try:
                module = importlib.import_module(info.name)
                found = self._register_classes_from_module(module)

                # Large features may be packages with their class in feature.py.
                if info.ispkg and not found:
                    try:
                        feature_module = importlib.import_module(info.name + ".feature")
                        self._register_classes_from_module(feature_module)
                    except ModuleNotFoundError as exc:
                        if exc.name != info.name + ".feature":
                            raise
            except Exception:
                self.errors[info.name] = traceback.format_exc()
                print(f"[NAIA EXten] feature discovery failed: {info.name}")

        self.features = OrderedDict(
            sorted(
                self.features.items(),
                key=lambda item: (
                    item[1].order,
                    item[1].category.lower(),
                    item[1].name.lower(),
                ),
            )
        )
        self._discovered = True

    def _register_classes_from_module(self, module) -> bool:
        found = False
        for value in vars(module).values():
            if not isinstance(value, type):
                continue
            if value is BaseFeature or not issubclass(value, BaseFeature):
                continue
            if value.__module__ != module.__name__:
                continue

            instance = value()
            if not instance.id:
                raise ValueError(f"{value.__name__}: feature id is empty")
            if instance.id in self.features:
                raise ValueError(f"duplicate feature id: {instance.id}")

            self.features[instance.id] = instance
            found = True
        return found

    def register_all(self) -> None:
        for feature in self.features.values():
            try:
                feature._attach(self.ext)
                feature.register()
            except Exception:
                # A feature can fail after installing only some of its layers.
                # Remove that owner's partial work without disturbing features
                # already composed on the same host methods.
                self.ext.patches.restore_owner(feature.id)
                self.errors[feature.id] = traceback.format_exc()
                self.ext.ctx.log(f"feature register failed: {feature.id}")

    def unregister_all(self) -> None:
        for feature in reversed(list(self.features.values())):
            try:
                feature.unregister()
            except Exception as exc:
                self.ext.ctx.log(f"feature unregister failed: {feature.id}: {exc}")
            finally:
                self.ext.patches.restore_owner(feature.id)

    def reload_all(self) -> tuple[bool, str]:
        """
        Hot-reload feature modules transactionally enough for development.

        Existing runtime method patches are restored before importing new code.
        If discovery/register fails, the previous module objects/features are put
        back and re-registered so a bad hotfix does not kill the working feature.
        """
        with self._reload_lock:
            preflight_error = self._preflight_sources()
            if preflight_error:
                return False, preflight_error

            old_features = self.features
            old_errors = dict(self.errors)
            old_discovered = self._discovered
            old_modules = self._feature_modules_snapshot()

            self.unregister_all()
            self.ext.patches.restore_all()

            try:
                self._purge_feature_modules()
                self._clear_feature_bytecode()
                importlib.invalidate_caches()

                self.features = OrderedDict()
                self.errors = {}
                self._action_owner.clear()
                self._discovered = False

                self.discover()
                if self.errors:
                    raise RuntimeError(self._format_errors("feature discovery"))

                self.register_all()
                if self.errors:
                    raise RuntimeError(self._format_errors("feature register"))

                return True, f"{len(self.features)} feature(s) reloaded"
            except Exception as exc:
                failed_text = str(exc)
                self.ext.patches.restore_all()
                self._purge_feature_modules()
                sys.modules.update(old_modules)
                importlib.invalidate_caches()

                self.features = old_features
                self.errors = old_errors
                self._discovered = old_discovered
                self._action_owner.clear()

                # Restore the previous working feature patch set.
                try:
                    self.register_all()
                except Exception:
                    pass

                return False, failed_text

    def _preflight_sources(self) -> str | None:
        features_root = Path(__file__).resolve().parent / "features"
        for path in sorted(features_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                source = path.read_text(encoding="utf-8")
                compile(source, str(path), "exec")
            except Exception as exc:
                rel = path.relative_to(features_root).as_posix()
                return f"{rel}: {type(exc).__name__}: {exc}"
        return None

    def _feature_modules_snapshot(self) -> dict[str, object]:
        return {
            name: module
            for name, module in list(sys.modules.items())
            if name.startswith(self.FEATURE_MODULE_PREFIX)
            and name != self.BASE_MODULE_NAME
            and module is not None
        }

    def _purge_feature_modules(self) -> None:
        for name in list(sys.modules):
            if (
                name.startswith(self.FEATURE_MODULE_PREFIX)
                and name != self.BASE_MODULE_NAME
            ):
                sys.modules.pop(name, None)

    @staticmethod
    def _clear_feature_bytecode() -> None:
        features_root = Path(__file__).resolve().parent / "features"
        for cache_dir in features_root.rglob("__pycache__"):
            try:
                for pyc in cache_dir.glob("*.pyc"):
                    try:
                        pyc.unlink()
                    except OSError:
                        pass
            except OSError:
                pass

    def _format_errors(self, phase: str) -> str:
        first_key = next(iter(self.errors), "unknown")
        detail = self.errors.get(first_key, "").strip().splitlines()
        tail = detail[-1] if detail else "unknown error"
        return f"{phase} failed: {first_key}: {tail}"

    def build_panel_fields(self) -> list[dict]:
        fields: list[dict] = []
        self._action_owner.clear()

        if not self.features:
            return fields

        for feature in self.features.values():
            toggle_key = feature.enabled_key

            toggle_field = {
                "key": toggle_key,
                "type": "bool",
                "default": feature.default_enabled,
                "label": f"{feature.name} 활성화",
                "help": feature.description,
                "section": feature.category,
                "order": feature.order * 100,
                "apply": "immediate",
                "scope": "module",
            }
            # Some features expose their switch inside the host module itself.
            # Keep the field registered (so setModuleParam validates/saves it),
            # but hide it from the generic NAIA EXten panel.
            if not bool(getattr(feature, "panel_toggle_visible", True)):
                toggle_field["visible_when"] = {
                    "field": "__naia_exten_internal_never__",
                    "in": ["1"],
                }
            fields.append(toggle_field)

            try:
                custom_fields = feature.panel_fields() or []
            except Exception:
                self.errors[feature.id] = traceback.format_exc()
                custom_fields = []

            for index, raw in enumerate(custom_fields, start=1):
                field = feature.normalize_panel_field(raw, index=index)
                if field.get("type") == "action":
                    self._action_owner[field["key"]] = feature
                fields.append(field)

        return fields

    def handle_action(self, key: str) -> None:
        feature = self._action_owner.get(str(key))
        if feature is None:
            self.ext.ctx.log(f"unknown EXten action: {key}")
            return

        try:
            feature.handle_action(str(key))
        except Exception as exc:
            self.ext.ctx.log(f"feature action failed: {feature.id}: {exc}")
            raise

    def get(self, feature_id: str):
        return self.features.get(feature_id)
