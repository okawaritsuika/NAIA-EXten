from __future__ import annotations

import os
import hashlib
import json
import uuid
import threading
from pathlib import Path
from typing import Any

import pandas as pd

from .base_feature import BaseFeature
from ..parquet_sync_journal import ConsumptionJournal


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
        "사용한 행은 즉시 기록하고 parquet 삭제는 15초 단위로 모아서 반영합니다. "
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
        self._target_generation = 0
        self._journal = None
        self._selection_lock = threading.RLock()
        self._selection_local = threading.local()
        self._stop = threading.Event()
        self._worker = None
        self._compaction_lock = threading.Lock()
        self._consumed_records = {}
        self.flush_interval = 15.0

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
        self._journal = ConsumptionJournal(Path(self.ctx.ext_dir) / "parquet_consumption.sqlite3")
        # Share this lock across feature hot reloads, including an old worker
        # finishing a large file while the new feature has already registered.
        lock = getattr(app_context, "_naia_exten_compaction_lock", None)
        if lock is None:
            lock = threading.Lock()
            app_context._naia_exten_compaction_lock = lock
        self._compaction_lock = lock
        self._restore_target_from_settings()
        self._patch_search_backend()
        self._patch_search_result_consumption()
        self._patch_search_panel_frontend()
        self._start_worker()

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
                replace=self.run_selection,
            )
        # Rating percentages consult counts before selecting a row. Apply old
        # consumption once to a restored model so those counts remain accurate.
        if callable(getattr(SearchResultModel, "get_count_by_rating", None)):
            self.ext.patches.wrap_method(
                owner=self.id, target=SearchResultModel,
                method_name="get_count_by_rating", before=self._prepare_model,
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

        with self._selection_lock, self._state_lock:
            self._target_generation += 1
            self._target_paths = unique
            self._target_path = unique[0] if unique else None
            self._consumed_records = self._journal.records_for(unique) if self._journal else {}
            self._seen_row_keys = set(self._consumed_records)

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
        with self._selection_lock, self._state_lock:
            had_target = bool(self._target_paths or self._target_path)
            self._target_generation += 1
            self._target_paths = []
            self._target_path = None
            self._consumed_records.clear()
            self._seen_row_keys.clear()

        self._save_hidden_setting(self.TARGETS_KEY, [])
        self._save_hidden_setting(self.TARGET_KEY, "")
        if had_target and reason:
            self.ctx.log(f"Parquet sync target cleared: {reason}")

    # ------------------------------------------------------------------
    # Consume hook
    # ------------------------------------------------------------------

    def run_selection(self, original, model, *args, **kwargs):
        if (model is not getattr(self._context, "search_results", None)
                or getattr(self._selection_local, "depth", 0)):
            return original(model, *args, **kwargs)
        with self._selection_lock:
            self._prepare_model(model)
            self._selection_local.depth = 1
            try:
                # Nested host/multi-pool selectors are covered by the outermost
                # call: record a returned row exactly once, never an intermediate.
                while True:
                    row = original(model, *args, **kwargs)
                    if row is None:
                        return None
                    key, _, _ = self._row_identity(row)
                    if key and key in self._seen_row_keys:
                        continue
                    return self._after_row_pop(row, model)
            finally:
                self._selection_local.depth = 0

    def _prepare_model(self, model, *args, **kwargs):
        if model is not getattr(self._context, "search_results", None):
            return
        with self._selection_lock:
            if not self._consumed_records:
                return
            model._ensure_bucketized()
            buckets = getattr(model, "_buckets", {})
            stamp = (id(self), self._target_generation,
                     tuple((key, id(b.df)) for key, b in buckets.items()))
            if getattr(model, "_naia_sync_stamp", None) == stamp:
                return
            ids = {item[0] for item in self._consumed_records.values() if item[0] is not None}
            fallback = {}
            for item in self._consumed_records.values():
                if item[1] is not None:
                    data = {key: value for key, value in item[1].items() if key != "__naia_remaining__"}
                    key = json.dumps(data, sort_keys=True, default=str)
                    remaining = int(item[1].get("__naia_remaining__", 0))
                    rec = fallback.setdefault(key, [data, remaining])
                    rec[1] = min(rec[1], remaining)
            fallback_indices = {key: set() for key in buckets}
            for data, remaining in fallback.values():
                matches = []
                for key, bucket in buckets.items():
                    indices = bucket.df.index[self._fallback_mask(bucket.df, data)]
                    matches.extend((key, index) for index in indices if index not in bucket.consumed_indices)
                # A compacted file already has <= remaining occurrences. A stale
                # snapshot has more. This avoids deleting a second identical
                # no-ID row merely because the file was loaded after compaction.
                for key, index in matches[:max(0, len(matches) - remaining)]:
                    fallback_indices[key].add(index)
            changed = False
            for key, bucket in buckets.items():
                frame = bucket.df
                mask = frame["id"].isin(ids) if ids and "id" in frame else pd.Series(False, index=frame.index)
                if fallback_indices[key]:
                    mask.loc[list(fallback_indices[key])] = True
                removed = set(frame.index[mask]) - bucket.consumed_indices
                if removed:
                    bucket.consumed_indices.update(removed)
                    bucket.invalidate_caches()
                    changed = True
            if changed:
                model._mark_bucket_data_changed()
                # Equal-source locators may have been built before restoration.
                model._naia_exten_mp_equal_cache = None
            model._naia_sync_stamp = stamp

    def _after_row_pop(self, popped_row, model, *args, **kwargs):
        if (model is not getattr(self._context, "search_results", None)
                or popped_row is None or not self._runtime_active()):
            return popped_row
        with self._state_lock:
            targets = list(self._target_paths)
        if not targets:
            return popped_row
        row_key, row_id, fallback = self._row_identity(popped_row)
        if not row_key or row_key in self._seen_row_keys:
            return popped_row
        if row_id is None:
            # Identical no-ID rows are separate occurrences. Nested calls are
            # already deduplicated by run_selection, so give each pop its own ID.
            row_key += ":" + uuid.uuid4().hex
            matching_limits = []
            for _, previous in self._consumed_records.values():
                if previous is not None and {k: v for k, v in previous.items() if k != "__naia_remaining__"} == fallback:
                    matching_limits.append(int(previous.get("__naia_remaining__", 0)))
            if matching_limits:
                remaining = max(0, min(matching_limits) - 1)
            else:
                frame = getattr(self._context, "search_results_master_base_snapshot", None)
                if frame is not None:
                    remaining = max(0, int(self._fallback_mask(frame, fallback).sum()) - 1)
                else:
                    remaining = sum(int(self._fallback_mask(b.df, fallback).sum())
                                    for b in getattr(model, "_buckets", {}).values()) - 1
            fallback["__naia_remaining__"] = max(0, remaining)
        # Small durable transaction only. No DataFrame copy, Parquet read/write,
        # or snapshot reconstruction takes place on the Random hot path.
        self._journal.record(targets, row_key, row_id, fallback)
        self._seen_row_keys.add(row_key)
        self._consumed_records[row_key] = (row_id, fallback)
        # This model already consumed the row. Stamp it without rescanning all
        # prior tombstones on the next pop; new/restored models are filtered once.
        buckets = getattr(model, "_buckets", {})
        model._naia_sync_stamp = (id(self), self._target_generation,
                                 tuple((key, id(b.df)) for key, b in buckets.items()))
        self._start_worker()
        return popped_row

    def _row_identity(self, row):
        try:
            if "id" in row.index and pd.notna(row.get("id")):
                row_id = row.get("id")
                if callable(getattr(row_id, "item", None)):
                    row_id = row_id.item()
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
        fallback = {key: (None if pd.isna(data.get(key)) else data.get(key))
                    for key in keys if key in data}
        if "id" in data:
            fallback["id"] = None
        if not fallback:
            return "", None, None
        key_text = repr(sorted((key, repr(value)) for key, value in fallback.items()))
        return f"row:{key_text}", None, fallback

    # ------------------------------------------------------------------
    # Snapshot + parquet update
    # ------------------------------------------------------------------

    def _start_worker(self):
        with self._state_lock:
            if self._stop.is_set() or (self._worker and self._worker.is_alive()):
                return
            self._worker = threading.Thread(target=self._sync_worker,
                                            name="naia-exten-parquet-sync", daemon=True)
            self._worker.start()

    def _sync_worker(self):
        # A fixed window batches rapid clicks; failures wait for the next window
        # instead of the old worker's unbounded immediate thread-restart loop.
        while not self._stop.wait(self.flush_interval):
            try:
                self.flush_pending()
            except Exception as exc:
                self.ctx.log(f"Parquet sync pending (will retry): {exc}")

    def unregister(self):
        self._stop.set()
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=2)
        # Unflushed records are already durable and replay on the next register.

    def flush_pending(self):
        with self._compaction_lock:
            for target, records in self._journal.pending().items():
                path = Path(target)
                if not path.is_file():
                    continue  # Keep its journal if temporarily unavailable.
                try:
                    receipt = self._journal.receipt(path)
                    if receipt:
                        digest, sequences = receipt
                        if self._file_digest(path) == digest:
                            # Crash after file replacement but before DB commit:
                            # do not delete a second identical no-ID row on retry.
                            self._journal.acknowledge(sequences)
                            done = set(sequences)
                            records = [record for record in records if record[0] not in done]
                        self._journal.clear_receipt(path)
                        if not records:
                            continue
                    ids = {record[1] for record in records if record[1] is not None}
                    fallback = [record[2] for record in records if record[2] is not None]
                    before_replace = None
                    if fallback:
                        before_replace = lambda tmp: self._journal.stage_replacement(
                            path, self._file_digest(tmp), [record[0] for record in records])
                    removed = self._compact_parquet(path, ids, fallback, before_replace=before_replace)
                    self._journal.acknowledge([record[0] for record in records])
                    self._journal.clear_receipt(path)
                    if removed:
                        self.ctx.log(f"Parquet sync: {path.name}, {removed:,} rows removed (batched)")
                except Exception as exc:
                    self.ctx.log(f"Parquet sync pending for {path.name}: {exc}")

    @staticmethod
    def _fallback_mask(frame, data):
        mask = pd.Series(True, index=frame.index)
        usable = False
        for column, expected in data.items():
            if column not in frame:
                continue
            usable = True
            series = frame[column]
            mask &= series.isna() if pd.isna(expected) else series.astype(str) == str(expected)
        return mask if usable else pd.Series(False, index=frame.index)

    @staticmethod
    def _file_digest(path):
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _compact_parquet(self, path, ids, fallback, *, before_replace=None):
        import pyarrow as pa
        import pyarrow.parquet as pq

        before_stat = path.stat()
        fingerprint = (before_stat.st_size, before_stat.st_mtime_ns)
        tmp = path.with_name(path.name + ".exten.sync.tmp")
        removed = 0
        remaining_fallback = list(fallback)
        try:
            # Bound peak memory to a batch, rather than reading a multi-GB file
            # into pandas beside the already-loaded search pool.
            with pq.ParquetFile(path) as source:
                with pq.ParquetWriter(tmp, source.schema_arrow, compression="snappy") as writer:
                    for batch in source.iter_batches(batch_size=65536):
                        frame = batch.to_pandas()
                        keep = ~frame["id"].isin(ids) if ids and "id" in frame else pd.Series(True, index=frame.index)
                        for data in remaining_fallback[:]:
                            matches = frame.index[keep & self._fallback_mask(frame, data)]
                            if len(matches):
                                keep.loc[matches[0]] = False
                                remaining_fallback.remove(data)
                        count = int((~keep).sum())
                        removed += count
                        # Filter the Arrow batch itself: preserve original field
                        # types/schema metadata and avoid pandas round-trip casts.
                        table = pa.Table.from_batches([batch]).filter(pa.array(keep.to_numpy()))
                        writer.write_table(table)
            after_stat = path.stat()
            if (after_stat.st_size, after_stat.st_mtime_ns) != fingerprint:
                raise RuntimeError("source changed during compaction; journal retained")
            if removed:
                with open(tmp, "r+b") as finished:
                    os.fsync(finished.fileno())
                if before_replace is not None:
                    before_replace(tmp)
                os.replace(tmp, path)
            return removed
        finally:
            if tmp.exists():
                tmp.unlink()

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
