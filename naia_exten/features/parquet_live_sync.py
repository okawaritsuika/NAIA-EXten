from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pandas as pd

from .base_feature import BaseFeature


class ParquetLiveSyncFeature(BaseFeature):
    """
    Removes only actually-consumed search rows from the loaded custom parquet.

    Design notes:
    - We do NOT save context.search_results wholesale after every pop. That frame
      can be temporarily narrowed by rating / Tag Filter and would accidentally
      delete hidden rows from disk.
    - Instead, successful SearchResultModel pop_* calls give us the exact consumed
      row. We remove that row by id from the tracked parquet.
    - Browser uploads do not expose the original OS path. When live sync is ON,
      an uploaded "load" parquet is copied into NAIA's custom_tags directory under
      the same filename and that managed copy becomes the sync target.
    """

    id = "parquet_live_sync"
    name = "Parquet 실시간 동기화"
    description = (
        "Search에서 불러온 custom parquet을 작업 원본으로 추적하고, "
        "랜덤 프롬프트에 실제 사용된 행만 parquet에서 자동 삭제합니다. "
        "Rating/Tag Filter로 잠시 제외된 행은 삭제하지 않습니다."
    )
    category = "Search / Parquet"
    order = 10
    default_enabled = False
    panel_toggle_visible = False

    TARGET_KEY = "target_filename"
    TARGETS_KEY = "target_filenames"

    def __init__(self):
        super().__init__()
        self._context = None
        self._target_path: Path | None = None
        self._target_paths: list[Path] = []

        self._state_lock = threading.RLock()
        self._pending_ids: set[Any] = set()
        self._pending_rows: list[dict[str, Any]] = []
        self._worker_running = False
        self._target_generation = 0

        # Prevent nested pop wrappers from scheduling the same consumed id twice.
        self._seen_row_keys: set[str] = set()

    def panel_fields(self):
        return [
            {
                "key": "clear_target",
                "type": "action",
                "label": "동기화 대상 초기화",
                "help": (
                    "현재 기억 중인 parquet 동기화 대상을 해제합니다. "
                    "다음 Parquet 불러오기부터 새 대상이 잡힙니다."
                ),
                "visible_when": {
                    "field": "__naia_exten_internal_never__",
                    "in": ["1"],
                },
            }
        ]

    def handle_action(self, full_key: str) -> None:
        if full_key != self.key("clear_target"):
            return

        self._clear_target("사용자 요청")
        self.ctx.show_toast(
            "Parquet 동기화 대상을 초기화했습니다.",
            "info",
        )

    def register(self) -> None:
        app_context = self.ext.host.app_context
        if app_context is None:
            self.ctx.log(
                "Parquet 실시간 동기화: NAIA 내부 context를 찾지 못해 비활성화됩니다."
            )
            return

        self._context = app_context
        self._restore_target_from_settings()
        self._patch_search_backend()
        self._patch_search_result_consumption()
        self._patch_search_panel_frontend()

        self.ctx.log("Parquet 실시간 동기화 feature registered")

    # ------------------------------------------------------------------
    # Search panel frontend bridge
    # ------------------------------------------------------------------

    _SEARCH_PANEL_JS_MARKER = "/* NAIA_EXTEN_PARQUET_SEARCH_PANEL_V2 */"
    _SEARCH_PANEL_INJECTED_JS = '/* NAIA_EXTEN_PARQUET_SEARCH_PANEL_V2 */\n(() => {\n  if (window.__naiaExtenParquetSearchPanelV2) return;\n  window.__naiaExtenParquetSearchPanelV2 = true;\n\n  const EXT_ID = \'naia_exten\';\n  const SETTING_KEY = \'feature__parquet_live_sync__enabled\';\n  const ROW_ID = \'naiaExtenParquetSyncSearchRow\';\n  const STYLE_ID = \'naiaExtenParquetSyncSearchStyle\';\n\n  function extensionState() {\n    const list = Array.isArray(lastExtensionsState?.extensions)\n      ? lastExtensionsState.extensions\n      : [];\n    return list.find(item => item?.id === EXT_ID) || null;\n  }\n\n  function enabledValue() {\n    const ext = extensionState();\n    return Boolean(ext?.settings?.[SETTING_KEY]);\n  }\n\n  function requestExtensionState() {\n    try {\n      if (typeof requestModuleState === \'function\') {\n        requestModuleState(\'extensions\');\n      }\n    } catch (_) {}\n  }\n\n  function setEnabled(checked) {\n    const ext = extensionState();\n    if (ext) {\n      if (!ext.settings || typeof ext.settings !== \'object\') ext.settings = {};\n      ext.settings[SETTING_KEY] = Boolean(checked);\n    }\n    setModuleParam(\n      \'extensions\',\n      `setting:${EXT_ID}:${SETTING_KEY}`,\n      Boolean(checked)\n    );\n  }\n\n  function installStyle() {\n    if (document.getElementById(STYLE_ID)) return;\n\n    const style = document.createElement(\'style\');\n    style.id = STYLE_ID;\n    style.textContent = `\n      #${ROW_ID} {\n        display: flex;\n        align-items: center;\n        justify-content: space-between;\n        gap: 12px;\n        min-height: 34px;\n        margin: 8px 0 10px;\n        padding: 7px 10px;\n        border: 1px solid var(--border-dim);\n        border-radius: 8px;\n        background: color-mix(in srgb, var(--bg-surface) 82%, transparent);\n      }\n      #${ROW_ID} .naia-exten-parquet-sync-label {\n        min-width: 0;\n        color: var(--text-primary);\n        font-size: 12px;\n        font-weight: 650;\n        line-height: 1.3;\n      }\n      #${ROW_ID} .naia-exten-parquet-sync-switch {\n        position: relative;\n        display: inline-block;\n        flex: 0 0 auto;\n        width: 42px;\n        height: 23px;\n        cursor: pointer;\n      }\n      #${ROW_ID} .naia-exten-parquet-sync-switch input {\n        position: absolute;\n        opacity: 0;\n        width: 1px;\n        height: 1px;\n        pointer-events: none;\n      }\n      #${ROW_ID} .naia-exten-parquet-sync-track {\n        position: absolute;\n        inset: 0;\n        border: 1px solid var(--border-dim);\n        border-radius: 999px;\n        background: var(--bg-elevated);\n        transition: background .15s ease, border-color .15s ease;\n      }\n      #${ROW_ID} .naia-exten-parquet-sync-track::after {\n        content: \'\';\n        position: absolute;\n        top: 2px;\n        left: 2px;\n        width: 17px;\n        height: 17px;\n        border-radius: 50%;\n        background: var(--text-muted);\n        transition: transform .15s ease, background .15s ease;\n      }\n      #${ROW_ID} input:checked + .naia-exten-parquet-sync-track {\n        background: color-mix(in srgb, var(--accent) 72%, var(--bg-elevated));\n        border-color: var(--accent);\n      }\n      #${ROW_ID} input:checked + .naia-exten-parquet-sync-track::after {\n        transform: translateX(19px);\n        background: #fff;\n      }\n      #${ROW_ID} input:focus-visible + .naia-exten-parquet-sync-track {\n        outline: 2px solid var(--accent);\n        outline-offset: 2px;\n      }\n      #${ROW_ID}.naia-exten-unavailable {\n        opacity: .58;\n      }\n    `;\n    document.head.appendChild(style);\n  }\n\n  function syncRowState() {\n    const row = document.getElementById(ROW_ID);\n    if (!row) return;\n\n    const input = row.querySelector(\'input[type="checkbox"]\');\n    if (!input) return;\n\n    const ext = extensionState();\n    const available = Boolean(ext && ext.status === \'loaded\');\n\n    input.disabled = !available;\n    input.checked = available ? enabledValue() : false;\n    row.classList.toggle(\'naia-exten-unavailable\', !available);\n\n    if (!available) requestExtensionState();\n  }\n\n  function ensureRow() {\n    if (currentModuleId !== \'search\') return;\n    if (!moduleBody) return;\n\n    const topRow = moduleBody.querySelector(\'.search-top-row\');\n    if (!topRow) return;\n\n    installStyle();\n\n    let row = document.getElementById(ROW_ID);\n    if (!row) {\n      row = document.createElement(\'div\');\n      row.id = ROW_ID;\n\n      const label = document.createElement(\'span\');\n      label.className = \'naia-exten-parquet-sync-label\';\n      label.textContent = \'Parquet 실시간 동기화 활성화\';\n\n      const switchLabel = document.createElement(\'label\');\n      switchLabel.className = \'naia-exten-parquet-sync-switch\';\n      switchLabel.title =\n        \'불러온 custom parquet에서 랜덤 프롬프트에 실제 사용된 행을 자동 삭제합니다.\';\n\n      const input = document.createElement(\'input\');\n      input.type = \'checkbox\';\n      input.setAttribute(\'aria-label\', \'Parquet 실시간 동기화 활성화\');\n      input.addEventListener(\'change\', () => {\n        setEnabled(input.checked);\n        syncRowState();\n      });\n\n      const track = document.createElement(\'span\');\n      track.className = \'naia-exten-parquet-sync-track\';\n\n      switchLabel.append(input, track);\n      row.append(label, switchLabel);\n      topRow.insertAdjacentElement(\'afterend\', row);\n    }\n\n    syncRowState();\n  }\n\n  // Search panel is rebuilt when the module is opened, so re-insert the row\n  // whenever moduleBody changes.\n  if (moduleBody) {\n    new MutationObserver(() => {\n      queueMicrotask(ensureRow);\n    }).observe(moduleBody, { childList: true, subtree: true });\n  }\n\n  // Keep the Search switch synchronized when the Extensions state changes,\n  // including changes made from the NAIA EXten panel.\n  try {\n    const originalRenderExtensions = renderExtensions;\n    renderExtensions = function(m) {\n      const result = originalRenderExtensions(m);\n      queueMicrotask(() => {\n        ensureRow();\n        syncRowState();\n      });\n      return result;\n    };\n  } catch (_) {}\n\n  // Initial/open-panel fallback.\n  queueMicrotask(ensureRow);\n  setTimeout(ensureRow, 250);\n  setTimeout(ensureRow, 1000);\n})();\n'

    def _patch_search_panel_frontend(self) -> None:
        """Inject the Parquet sync switch into the Search module body."""
        try:
            # The feature switch controls syncing only. Keep its Search
            # checkbox visible while the extension-level switch controls the
            # injected UI itself.
            injected_js = self._SEARCH_PANEL_INJECTED_JS.replace(
                "const available = Boolean(ext && ext.status === 'loaded');",
                "const available = Boolean(ext && ext.status === 'loaded' && ext.enabled !== false);\n"
                "    row.style.display = available ? '' : 'none';",
            ).replace(
                "if (!available) requestExtensionState();",
                "if (!ext) requestExtensionState();",
            )
            injected_js = injected_js.replace(
                ").observe(moduleBody, { childList: true, subtree: true });",
                ").observe(moduleBody, { childList: true });",
            )
            wrapper_start = injected_js.find(
                "  // Keep the Search switch synchronized when the Extensions state changes,"
            )
            wrapper_end = injected_js.find(
                "  // Initial/open-panel fallback.",
                wrapper_start,
            )
            if wrapper_start >= 0 and wrapper_end > wrapper_start:
                injected_js = (
                    injected_js[:wrapper_start]
                    + "  // Keep NAIA's global Extensions renderer untouched.\n"
                    + injected_js[wrapper_end:]
                )
            self.ext.patches.add_web_injection(
                owner=self.id,
                file_name="app.js",
                marker=self._SEARCH_PANEL_JS_MARKER,
                content=injected_js,
            )
        except Exception as exc:
            self.ctx.log(f"Parquet sync Search UI unavailable: {exc}")

    # ------------------------------------------------------------------
    # Runtime gates / settings
    # ------------------------------------------------------------------

    def _runtime_active(self) -> bool:
        if not self.is_enabled():
            return False

        # Our method patches live outside ExtensionContext's safe callback wrapper,
        # so respect the host's global "Activate This Script" switch too.
        record = getattr(self.ctx, "_record", None)
        if record is not None:
            try:
                return bool(record.is_active)
            except Exception:
                return False
        return True

    def _load_all_settings(self) -> dict[str, Any]:
        return self.ctx.load_settings({})

    def _save_hidden_setting(self, local_key: str, value: Any) -> None:
        settings = self._load_all_settings()
        settings[self.key(local_key)] = value
        self.ctx.save_settings(settings)

    def _restore_target_from_settings(self) -> None:
        if self._context is None:
            return

        raw_targets = self.value(self.TARGETS_KEY, [])
        names: list[str] = []
        if isinstance(raw_targets, (list, tuple)):
            names = [str(item or "").strip() for item in raw_targets]
        names = [name for name in names if name]

        # Backward compatibility with v0.2.5 and older settings.
        if not names:
            legacy = str(self.value(self.TARGET_KEY, "") or "").strip()
            if legacy:
                names = [legacy]

        root = self._custom_parquet_dir()
        paths = []
        for name in names:
            path = root / Path(name).name
            if path.is_file() and path not in paths:
                paths.append(path)
        if paths:
            self._set_targets(paths, persist=False, log=False)

    # ------------------------------------------------------------------
    # Host patch registration
    # ------------------------------------------------------------------

    def _patch_search_backend(self) -> None:
        import app.backend.server.search_commands as search_commands
        import app.backend.server.params_workflow_routes as params_workflow_routes

        self.ext.patches.wrap_method(
            owner=self.id,
            target=search_commands,
            method_name="load_or_merge_custom_parquet",
            after=self._after_saved_parquet_load,
        )
        self.ext.patches.wrap_method(
            owner=self.id,
            target=search_commands,
            method_name="run_search_command",
            before=self._before_full_archive_search,
        )
        self.ext.patches.wrap_method(
            owner=self.id,
            target=params_workflow_routes,
            method_name="_apply_uploaded_search_parquet",
            after=self._after_uploaded_parquet_load,
        )

    def _patch_search_result_consumption(self) -> None:
        from core.search_result_model import SearchResultModel

        for method_name in (
            "pop_random_row",
            "pop_random_row_matching",
            "pop_random_row_matching_tags",
            "pop_random_row_with_id_filter",
        ):
            if not callable(getattr(SearchResultModel, method_name, None)):
                continue
            self.ext.patches.wrap_method(
                owner=self.id,
                target=SearchResultModel,
                method_name=method_name,
                after=self._after_row_pop,
            )

    # ------------------------------------------------------------------
    # Track which custom parquet owns the current dataset
    # ------------------------------------------------------------------

    def _after_saved_parquet_load(
        self,
        result,
        context,
        filename,
        *args,
        **kwargs,
    ):
        merge = bool(kwargs.get("merge", False))

        path = self._custom_parquet_dir(context) / Path(str(filename or "")).name
        if not path.is_file():
            return result

        if merge:
            # If the current pool is already backed by tracked saved parquets,
            # extend that ownership set so multi-parquet pools remain live-syncable.
            with self._state_lock:
                existing = list(self._target_paths)
            if existing:
                self._set_targets(existing + [path])
            return result

        try:
            _state, toast = result
            if isinstance(toast, dict) and toast.get("level") == "error":
                return result
        except Exception:
            pass

        self._set_target(path)
        return result

    def _after_uploaded_parquet_load(
        self,
        result,
        context,
        content,
        action,
        filename,
        *args,
        **kwargs,
    ):
        action = str(action or "").strip().lower()

        if action == "merge":
            self._clear_target("업로드 Parquet 합치기")
            return result
        if action != "load":
            return result

        self._clear_target("새 업로드 Parquet 불러오기")

        if not self._runtime_active():
            return result

        safe_name = Path(str(filename or "uploaded.parquet")).name
        if not safe_name.lower().endswith(".parquet") or not content:
            return result

        path = self._custom_parquet_dir(context) / safe_name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_bytes(path, bytes(content))
            self._set_target(path)
            self.ctx.log(
                f"Parquet sync target (managed upload copy): {path.name}"
            )
        except Exception as exc:
            self.ctx.log(
                f"Parquet sync: uploaded copy save failed: {exc}"
            )
        return result

    def _before_full_archive_search(self, *args, **kwargs):
        self._clear_target("전체 Search 실행")

    def _set_target(
        self,
        path: Path,
        *,
        persist: bool = True,
        log: bool = True,
    ) -> None:
        self._set_targets([Path(path)], persist=persist, log=log)

    def _set_targets(
        self,
        paths: list[Path] | tuple[Path, ...],
        *,
        persist: bool = True,
        log: bool = True,
    ) -> None:
        unique: list[Path] = []
        seen: set[str] = set()
        for raw in paths:
            path = Path(raw)
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)

        with self._state_lock:
            self._target_generation += 1
            self._target_paths = unique
            self._target_path = unique[0] if unique else None
            self._pending_ids.clear()
            self._pending_rows.clear()
            self._seen_row_keys.clear()

        if persist:
            self._save_hidden_setting(self.TARGETS_KEY, [path.name for path in unique])
            self._save_hidden_setting(self.TARGET_KEY, unique[0].name if len(unique) == 1 else "")
        if log:
            if len(unique) == 1:
                self.ctx.log(f"Parquet sync target: {unique[0].name}")
            elif unique:
                self.ctx.log(
                    "Parquet sync targets: "
                    + ", ".join(path.name for path in unique)
                )

    def set_targets(self, paths: list[Path] | tuple[Path, ...]) -> None:
        """Public feature interop hook used by the multi-parquet pool feature."""
        self._set_targets(paths)

    def _clear_target(self, reason: str = "") -> None:
        with self._state_lock:
            had_target = bool(self._target_paths or self._target_path)
            self._target_generation += 1
            self._target_paths = []
            self._target_path = None
            self._pending_ids.clear()
            self._pending_rows.clear()
            self._seen_row_keys.clear()

        self._save_hidden_setting(self.TARGETS_KEY, [])
        self._save_hidden_setting(self.TARGET_KEY, "")
        if had_target and reason:
            self.ctx.log(f"Parquet sync target cleared: {reason}")

    # ------------------------------------------------------------------
    # Consume hook
    # ------------------------------------------------------------------

    def _after_row_pop(self, popped_row, model, *args, **kwargs):
        if model is not getattr(self._context, "search_results", None):
            return popped_row
        if popped_row is None or not self._runtime_active():
            return popped_row

        with self._state_lock:
            targets = [path for path in self._target_paths if path.is_file()]

        if not targets:
            return popped_row

        row_key, row_id, row_fallback = self._row_identity(popped_row)
        if not row_key:
            return popped_row

        with self._state_lock:
            if row_key in self._seen_row_keys:
                return popped_row
            self._seen_row_keys.add(row_key)

        self._remove_from_host_snapshots(row_id, row_fallback)

        with self._state_lock:
            if row_id is not None:
                self._pending_ids.add(row_id)
            elif row_fallback is not None:
                self._pending_rows.append(row_fallback)
            generation = self._target_generation

            if not self._worker_running:
                self._worker_running = True
                threading.Thread(
                    target=self._sync_worker,
                    args=(generation,),
                    name="naia-exten-parquet-sync",
                    daemon=True,
                ).start()

        return popped_row

    def _row_identity(self, row):
        try:
            if "id" in row.index and pd.notna(row.get("id")):
                row_id = row.get("id")
                return f"id:{row_id!r}", row_id, None
        except Exception:
            pass

        try:
            data = row.to_dict()
        except Exception:
            return "", None, None

        keys = (
            "general",
            "character",
            "copyright",
            "artist",
            "meta",
            "rating",
        )
        fallback = {key: data.get(key) for key in keys if key in data}
        if not fallback:
            return "", None, None
        key_text = repr(sorted((key, repr(value)) for key, value in fallback.items()))
        return f"row:{key_text}", None, fallback

    # ------------------------------------------------------------------
    # Snapshot + parquet update
    # ------------------------------------------------------------------

    def _remove_from_host_snapshots(
        self,
        row_id: Any | None,
        row_fallback: dict[str, Any] | None,
    ) -> None:
        context = self._context
        if context is None:
            return

        for attr in (
            "search_results_snapshot",
            "search_results_master_base_snapshot",
        ):
            frame = getattr(context, attr, None)
            if frame is None or getattr(frame, "empty", True):
                continue
            try:
                updated = self._remove_rows_from_frame(
                    frame,
                    {row_id} if row_id is not None else set(),
                    [row_fallback] if row_fallback is not None else [],
                )
                if updated is not frame:
                    setattr(context, attr, updated)
            except Exception as exc:
                self.ctx.log(
                    f"Parquet sync: memory snapshot update failed ({attr}): {exc}"
                )

    def _sync_worker(self, generation: int) -> None:
        try:
            while True:
                with self._state_lock:
                    if generation != self._target_generation:
                        return

                    targets = list(self._target_paths)
                    pending_ids = set(self._pending_ids)
                    pending_rows = list(self._pending_rows)
                    self._pending_ids.clear()
                    self._pending_rows.clear()

                if not targets:
                    return
                if not pending_ids and not pending_rows:
                    return

                try:
                    changed_any = False
                    for target in targets:
                        if not target.is_file():
                            continue
                        frame = pd.read_parquet(target)
                        before = len(frame)
                        frame = self._remove_rows_from_frame(
                            frame,
                            pending_ids,
                            pending_rows,
                        )
                        after = len(frame)

                        if after != before:
                            self._atomic_write_parquet(target, frame)
                            changed_any = True
                            self.ctx.log(
                                f"Parquet sync: {target.name} "
                                f"{before:,} → {after:,} rows"
                            )

                    if changed_any:
                        self._sync_last_search_cache(
                            pending_ids,
                            pending_rows,
                        )
                except Exception as exc:
                    with self._state_lock:
                        if generation == self._target_generation:
                            self._pending_ids.update(pending_ids)
                            self._pending_rows[0:0] = pending_rows
                    self.ctx.log(f"Parquet sync write failed: {exc}")
                    return

                with self._state_lock:
                    if generation != self._target_generation:
                        return
                    if not self._pending_ids and not self._pending_rows:
                        return
        finally:
            restart = False
            restart_generation = None
            with self._state_lock:
                self._worker_running = False
                if (
                    self._target_paths
                    and (self._pending_ids or self._pending_rows)
                ):
                    self._worker_running = True
                    restart = True
                    restart_generation = self._target_generation

            if restart:
                threading.Thread(
                    target=self._sync_worker,
                    args=(restart_generation,),
                    name="naia-exten-parquet-sync",
                    daemon=True,
                ).start()

    def _sync_last_search_cache(
        self,
        pending_ids: set[Any],
        pending_rows: list[dict[str, Any]],
    ) -> None:
        context = self._context
        if context is None:
            return

        path_getter = getattr(context, "last_search_parquet_path", None)
        path = path_getter() if callable(path_getter) else None
        if path is None:
            return
        path = Path(path)

        frame = getattr(context, "search_results_master_base_snapshot", None)
        if frame is not None and not getattr(frame, "empty", True):
            self._atomic_write_parquet(path, frame)
            return

        if not path.is_file():
            return
        frame = pd.read_parquet(path)
        frame = self._remove_rows_from_frame(
            frame,
            pending_ids,
            pending_rows,
        )
        self._atomic_write_parquet(path, frame)

    @staticmethod
    def _remove_rows_from_frame(
        frame: pd.DataFrame,
        ids: set[Any],
        fallback_rows: list[dict[str, Any]],
    ) -> pd.DataFrame:
        if frame is None or frame.empty:
            return frame

        result = frame
        changed = False

        if ids and "id" in result.columns:
            mask = ~result["id"].isin(ids)
            if not bool(mask.all()):
                result = result.loc[mask].copy()
                changed = True

        for row_data in fallback_rows:
            if result.empty:
                break
            mask = pd.Series(True, index=result.index)
            usable = False
            for column, expected in row_data.items():
                if column not in result.columns:
                    continue
                usable = True
                series = result[column]
                if pd.isna(expected):
                    mask &= series.isna()
                else:
                    mask &= series.astype(str) == str(expected)
            if not usable:
                continue
            matches = result.index[mask]
            if len(matches):
                result = result.drop(index=matches[0])
                changed = True

        if changed:
            return result.reset_index(drop=True)
        return frame

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _custom_parquet_dir(self, context=None) -> Path:
        context = context or self._context
        getter = getattr(context, "custom_parquet_dir", None)
        if callable(getter):
            return Path(getter())

        existing_save_path = getattr(context, "_existing_save_path", None)
        if callable(existing_save_path):
            return Path(existing_save_path("custom_tags"))

        raise RuntimeError("NAIA custom parquet directory is unavailable")

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".exten.tmp")
        try:
            tmp.write_bytes(content)
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    @staticmethod
    def _atomic_write_parquet(path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".exten.tmp")
        try:
            frame.to_parquet(tmp, index=False)
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
