from __future__ import annotations

from pathlib import Path
from typing import Any

import random

from .base_feature import BaseFeature


class MultiParquetPoolFeature(BaseFeature):
    """Pick several saved custom parquets and install them as one random pool."""

    id = "multi_parquet_pool"
    name = "다중 Parquet 랜덤 풀"
    description = (
        "Search 가이드에서 여러 custom parquet을 고르고 하나의 랜덤 풀로 합칩니다. "
        "선택 목록은 저장되며, Parquet 실시간 동기화가 켜져 있으면 선택한 원본들을 함께 추적합니다."
    )
    category = "Search / Parquet"
    order = 20
    default_enabled = True

    SELECTED_KEY = "selected_parquets"
    APPLY_NONCE_KEY = "apply_nonce"
    APPLIED_NONCE_KEY = "applied_nonce"
    STATUS_KEY = "status"
    EQUAL_KEY = "equal_probability"
    APPLY_ACTION = "apply_selection"

    # Small internal source-code column. It survives Search/Tag Filter slicing so
    # equal-per-parquet sampling can remain correct without loading a second copy
    # of every selected parquet into memory.
    SOURCE_COL = "__naia_exten_mp_src"

    def __init__(self):
        super().__init__()
        self._context = None

    def panel_fields(self) -> list[dict]:
        hidden = {
            "field": "__naia_exten_internal_never__",
            "in": ["1"],
        }
        return [
            {
                "key": self.SELECTED_KEY,
                "type": "list",
                "default": [],
                "label": "선택된 Parquet",
                "visible_when": hidden,
            },
            {
                "key": self.APPLY_NONCE_KEY,
                "type": "text",
                "default": "",
                "label": "다중 Parquet 적용 요청",
                "visible_when": hidden,
            },
            {
                "key": self.APPLIED_NONCE_KEY,
                "type": "text",
                "default": "",
                "label": "다중 Parquet 적용 완료",
                "visible_when": hidden,
            },
            {
                "key": self.STATUS_KEY,
                "type": "text",
                "default": "",
                "label": "다중 Parquet 상태",
                "visible_when": hidden,
            },
            {
                "key": self.EQUAL_KEY,
                "type": "bool",
                "default": False,
                "label": "Parquet별 균등 확률",
                "visible_when": hidden,
            },
            {
                "key": self.APPLY_ACTION,
                "type": "action",
                "label": "다중 Parquet 적용",
                "visible_when": hidden,
            },
        ]

    def register(self) -> None:
        self._context = self.ext.host.app_context
        if self._context is None:
            self.ctx.log("다중 Parquet 랜덤 풀: NAIA 내부 context를 찾지 못했습니다.")
            return
        # PatchManager composes this selector outside parquet_live_sync's observer,
        # so equal source selection also works when GSQE is disabled while the
        # actually consumed row still reaches live-sync bookkeeping.
        self._patch_equal_random_selection()
        self._patch_search_panel_frontend()
        self.ctx.log("다중 Parquet 랜덤 풀 feature registered")

    def handle_action(self, full_key: str) -> None:
        if full_key != self.key(self.APPLY_ACTION):
            return
        self._apply_selected_pool()

    def _save_values(self, **local_values: Any) -> None:
        settings = self.ctx.load_settings({})
        for local_key, value in local_values.items():
            settings[self.key(local_key)] = value
        self.ctx.save_settings(settings)

    def _apply_selected_pool(self) -> None:
        nonce = str(self.value(self.APPLY_NONCE_KEY, "") or "")
        requested = self.value(self.SELECTED_KEY, [])
        if not isinstance(requested, (list, tuple)):
            requested = []

        context = self._context
        if context is None:
            self._save_values(
                **{
                    self.STATUS_KEY: "NAIA 검색 context를 찾지 못했습니다.",
                    self.APPLIED_NONCE_KEY: nonce,
                }
            )
            return

        available = set(context.custom_parquet_names())
        selected: list[str] = []
        for item in requested:
            name = Path(str(item or "").strip()).name
            if name and name in available and name not in selected:
                selected.append(name)

        try:
            if not selected:
                self._save_values(**{self.STATUS_KEY: "선택된 Parquet이 없습니다."})
                self.ctx.show_toast("선택된 Parquet이 없습니다.", "info")
                return

            import pandas as pd
            from app.backend.server.search_runtime import (
                install_custom_parquet_frame,
                normalize_custom_parquet_frame,
                search_state_with_runner_save,
            )
            from core.parquet_chunk_loader import (
                make_search_load_progress,
                read_parquet_chunked,
            )

            root = Path(context.custom_parquet_dir())
            frames = []
            source_names: dict[int, str] = {}
            self._save_values(**{self.STATUS_KEY: f"Parquet {len(selected)}개 불러오기 준비 중…"})
            for index, name in enumerate(selected, 1):
                self._save_values(
                    **{self.STATUS_KEY: f"Parquet 불러오는 중 ({index}/{len(selected)}): {name}"}
                )
                path = root / name
                progress, done = make_search_load_progress(context)
                try:
                    frame = read_parquet_chunked(path, progress=progress)
                    frame = normalize_custom_parquet_frame(frame)
                    if frame is not None and not getattr(frame, "empty", True):
                        # uint16 is enough for far more files than the picker can
                        # reasonably use and costs only 2 bytes/row.  Keep the
                        # marker through concat/filtering so runtime selection can
                        # choose a parquet first without holding duplicate frames.
                        source_code = index - 1
                        frame = frame.copy()
                        frame[self.SOURCE_COL] = source_code
                        source_names[source_code] = name
                        frames.append(frame)
                finally:
                    done()

            if not frames:
                self._save_values(**{self.STATUS_KEY: "선택한 Parquet에서 불러올 행이 없습니다."})
                self.ctx.show_toast("선택한 Parquet에서 불러올 행이 없습니다.", "warning")
                return

            if len(frames) == 1:
                combined = frames[0]
            else:
                self._save_values(**{self.STATUS_KEY: "Parquet 합치는 중…"})
                combined = pd.concat(frames, ignore_index=True)
                self._save_values(**{self.STATUS_KEY: "중복 제거 및 랜덤 풀 정리 중…"})
                combined = normalize_custom_parquet_frame(combined)

            self._save_values(**{self.STATUS_KEY: "랜덤 풀 적용 중…"})
            install_custom_parquet_frame(context, combined)
            context._naia_exten_multi_parquet_source_names = dict(source_names)
            search_state_with_runner_save(context)

            # Keep source ownership aligned with the merged pool. The sync feature
            # remains a no-op while its own toggle is OFF, but restoring these paths
            # means enabling it later is still safe and correct.
            sync_feature = self.ext.features.get("parquet_live_sync")
            if sync_feature is not None and hasattr(sync_feature, "set_targets"):
                sync_feature.set_targets([root / name for name in selected])

            status = f"완료 · {len(selected)}개 Parquet / {len(combined):,}행"
            self._save_values(
                **{
                    self.SELECTED_KEY: selected,
                    self.STATUS_KEY: status,
                }
            )
            self.ctx.show_toast(
                f"Parquet {len(selected)}개 랜덤 풀 적용 ({len(combined):,}행)",
                "success",
            )
        except Exception as exc:
            message = f"오류 · {exc}"
            self._save_values(**{self.STATUS_KEY: message})
            self.ctx.log(f"다중 Parquet 풀 적용 실패: {exc}")
            self.ctx.show_toast(f"다중 Parquet 적용 실패: {exc}", "error")
        finally:
            # The frontend polls this echoed value while an apply is running. This
            # makes long parquet reads visibly progress without any DOM observer.
            self._save_values(**{self.APPLIED_NONCE_KEY: nonce})

    # ------------------------------------------------------------------
    # Equal-per-parquet runtime selection
    # ------------------------------------------------------------------

    def equal_probability_enabled(self) -> bool:
        """Public helper used by GSQEProbabilityFeature for source-first rolls."""
        if not self.is_enabled():
            return False
        return bool(self.value(self.EQUAL_KEY, False))

    @staticmethod
    def _normalize_rating(value: Any) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _normalize_active_ratings(active_ratings: Any) -> set[str] | None:
        if not active_ratings:
            return None
        try:
            raw = set(active_ratings)
        except TypeError:
            raw = {active_ratings}
        out = {str(item or "").strip().lower() for item in raw}
        return {item for item in out if item}

    def _pool_signature(self, model) -> tuple[int, int, int]:
        try:
            model._ensure_bucketized()
        except Exception:
            pass
        buckets = getattr(model, "_buckets", {}) or {}
        total_rows = 0
        for bucket in buckets.values():
            try:
                total_rows += len(bucket.df)
            except Exception:
                pass
        return (id(buckets), len(buckets), total_rows)

    def _build_equal_cache(self, model) -> dict[str, Any] | None:
        """Build compact uint64 row locators grouped by source and rating.

        Each row appears exactly once in the cache.  A 3.5M-row pool therefore
        needs roughly 28 MB for locators (+ ids for tag-filter mode), rather than
        another multi-gigabyte DataFrame copy.
        """
        try:
            import numpy as np
            import pandas as pd

            model._ensure_bucketized()
            buckets = getattr(model, "_buckets", {}) or {}
            signature = self._pool_signature(model)
            pieces: dict[tuple[int, str], list[tuple[Any, Any]]] = {}
            sources_seen: set[int] = set()

            for bucket_id in getattr(model, "_bucket_order", list(buckets.keys())):
                bucket = buckets.get(bucket_id)
                if bucket is None:
                    continue
                frame = getattr(bucket, "df", None)
                if frame is None or getattr(frame, "empty", True) or self.SOURCE_COL not in frame.columns:
                    continue

                nrows = len(frame)
                valid = np.ones(nrows, dtype=bool)
                consumed = getattr(bucket, "consumed_indices", None)
                if consumed:
                    consumed_idx = np.fromiter((int(i) for i in consumed if 0 <= int(i) < nrows), dtype=np.int64)
                    if consumed_idx.size:
                        valid[consumed_idx] = False

                if "general" in frame.columns:
                    general = frame["general"]
                    gtext = general.astype(str).str.strip()
                    valid &= general.notna().to_numpy()
                    valid &= gtext.ne("").to_numpy()
                    valid &= ~gtext.str.lower().isin({"nan", "none", "null"}).to_numpy()

                src = pd.to_numeric(frame[self.SOURCE_COL], errors="coerce").fillna(-1).to_numpy(dtype=np.int64, copy=False)
                if "rating" in frame.columns:
                    ratings = frame["rating"].astype(str).str.strip().str.lower().to_numpy()
                else:
                    ratings = np.full(nrows, "", dtype=object)

                if "id" in frame.columns:
                    ids = pd.to_numeric(frame["id"], errors="coerce").fillna(-1).to_numpy(dtype=np.int64, copy=False)
                else:
                    ids = np.full(nrows, -1, dtype=np.int64)

                local_indices = np.arange(nrows, dtype=np.uint64)
                bucket_prefix = np.uint64(int(bucket_id) & 0xFFFFFFFF) << np.uint64(32)
                locators = bucket_prefix | local_indices

                valid_sources = np.unique(src[valid])
                for source_code in valid_sources:
                    source_code = int(source_code)
                    if source_code < 0:
                        continue
                    sources_seen.add(source_code)
                    src_mask = valid & (src == source_code)
                    rating_values = np.unique(ratings[src_mask])
                    for rating in rating_values:
                        rating_key = str(rating or "").strip().lower()
                        mask = src_mask & (ratings == rating)
                        if not bool(mask.any()):
                            continue
                        pieces.setdefault((source_code, rating_key), []).append(
                            (locators[mask].copy(), ids[mask].copy())
                        )

            pools: dict[tuple[int, str], dict[str, Any]] = {}
            for key, arrays in pieces.items():
                loc_parts = [item[0] for item in arrays]
                id_parts = [item[1] for item in arrays]
                loc = loc_parts[0] if len(loc_parts) == 1 else np.concatenate(loc_parts)
                row_ids = id_parts[0] if len(id_parts) == 1 else np.concatenate(id_parts)
                pools[key] = {"loc": loc, "ids": row_ids, "n": int(len(loc))}

            cache = {
                "signature": signature,
                "pools": pools,
                "sources": sorted(sources_seen),
                "filter_key": None,
                "filter_pools": None,
            }
            setattr(model, "_naia_exten_mp_equal_cache", cache)
            return cache
        except Exception as exc:
            self.ctx.log(f"다중 Parquet 균등 캐시 생성 실패: {exc}")
            return None

    def _equal_cache(self, model) -> dict[str, Any] | None:
        if model is None:
            return None
        try:
            signature = self._pool_signature(model)
            cache = getattr(model, "_naia_exten_mp_equal_cache", None)
            if not isinstance(cache, dict) or cache.get("signature") != signature:
                cache = self._build_equal_cache(model)
            return cache
        except Exception:
            return None

    @staticmethod
    def _pool_count(pool: dict[str, Any] | None) -> int:
        try:
            return max(0, int(pool.get("n") or 0))
        except Exception:
            return 0

    def _build_allowed_filter_pools(self, cache: dict[str, Any], allowed_ids: Any) -> dict[tuple[int, str], dict[str, Any]]:
        import numpy as np

        try:
            normalized = np.fromiter(
                (int(value) for value in allowed_ids),
                dtype=np.int64,
                count=len(allowed_ids),
            )
        except Exception:
            normalized = np.asarray([], dtype=np.int64)
        if normalized.size == 0:
            return {}

        out: dict[tuple[int, str], dict[str, Any]] = {}
        for key, base in (cache.get("pools") or {}).items():
            n = self._pool_count(base)
            if n <= 0:
                continue
            ids = base["ids"][:n]
            mask = np.isin(ids, normalized, assume_unique=False)
            if not bool(mask.any()):
                continue
            out[key] = {
                "loc": base["loc"][:n][mask].copy(),
                "ids": ids[mask].copy(),
                "n": int(mask.sum()),
            }
        return out

    def _candidate_pools(
        self,
        cache: dict[str, Any],
        active_ratings: Any,
        *,
        allowed_ids: Any = None,
    ) -> tuple[dict[tuple[int, str], dict[str, Any]], set[str] | None]:
        ratings = self._normalize_active_ratings(active_ratings)
        pools = cache.get("pools") or {}
        if allowed_ids is not None:
            key = id(allowed_ids)
            if cache.get("filter_key") != key or not isinstance(cache.get("filter_pools"), dict):
                cache["filter_key"] = key
                cache["filter_pools"] = self._build_allowed_filter_pools(cache, allowed_ids)
            pools = cache.get("filter_pools") or {}
        return pools, ratings

    def _pop_from_source_pool(
        self,
        model,
        pools: dict[tuple[int, str], dict[str, Any]],
        source_code: int,
        ratings: set[str] | None,
        *,
        rating_weights: dict[str, float] | None = None,
    ):
        groups: list[tuple[str, dict[str, Any], float]] = []
        for (src, rating), pool in pools.items():
            if int(src) != int(source_code):
                continue
            if ratings is not None and rating not in ratings:
                continue
            n = self._pool_count(pool)
            if n <= 0:
                continue
            if rating_weights is None:
                weight = float(n)
            else:
                try:
                    weight = max(0.0, float(rating_weights.get(rating, 0.0) or 0.0))
                except Exception:
                    weight = 0.0
            if weight > 0:
                groups.append((rating, pool, weight))

        if not groups:
            return None

        total_weight = sum(item[2] for item in groups)
        target = random.random() * total_weight
        selected_pool = groups[-1][1]
        for _rating, pool, weight in groups:
            target -= weight
            if target < 0:
                selected_pool = pool
                break

        # A locator can become stale if another feature consumed it through a
        # different path. Drop stale entries lazily and retry without rebuilding
        # a multi-million-row cache.
        while self._pool_count(selected_pool) > 0:
            n = self._pool_count(selected_pool)
            pos = random.randrange(n)
            last = n - 1
            loc = int(selected_pool["loc"][pos])
            if pos != last:
                selected_pool["loc"][pos] = selected_pool["loc"][last]
                selected_pool["ids"][pos] = selected_pool["ids"][last]
            selected_pool["n"] = last

            bucket_id = (loc >> 32) & 0xFFFFFFFF
            row_index = loc & 0xFFFFFFFF
            bucket = getattr(model, "_buckets", {}).get(bucket_id)
            if bucket is None:
                continue
            row = bucket.pop_row_at_index(int(row_index))
            if row is None:
                continue

            if getattr(model, "_rating_counts_cache", None) is not None and "rating" in row:
                rating = self._normalize_rating(row.get("rating"))
                if rating in model._rating_counts_cache:
                    model._rating_counts_cache[rating] = max(0, model._rating_counts_cache[rating] - 1)
            model._mark_bucket_data_changed()
            return row
        return None

    def pop_equal_row(
        self,
        model,
        active_ratings: Any = None,
        *,
        allowed_ids: Any = None,
        rating_weights: dict[str, float] | None = None,
    ):
        """Choose a parquet uniformly first, then a row inside that parquet."""
        if not self.equal_probability_enabled() or model is None:
            return None
        if self._context is None or model is not getattr(self._context, "search_results", None):
            return None

        cache = self._equal_cache(model)
        if not cache:
            return None
        pools, ratings = self._candidate_pools(cache, active_ratings, allowed_ids=allowed_ids)
        if not pools:
            return None

        # Source choice is deliberately unweighted by row count.
        candidates: list[int] = []
        for source_code in cache.get("sources") or []:
            if any(
                int(src) == int(source_code)
                and (ratings is None or rating in ratings)
                and self._pool_count(pool) > 0
                and (rating_weights is None or float(rating_weights.get(rating, 0.0) or 0.0) > 0)
                for (src, rating), pool in pools.items()
            ):
                candidates.append(int(source_code))
        if not candidates:
            return None

        # If a stale source loses its final row, retry another available source.
        random.shuffle(candidates)
        for source_code in candidates:
            row = self._pop_from_source_pool(
                model,
                pools,
                source_code,
                ratings,
                rating_weights=rating_weights,
            )
            if row is not None:
                return row
        return None

    def _patch_equal_random_selection(self) -> None:
        from core.search_result_model import SearchResultModel

        def replace_pop(original, model, active_ratings=None):
            if not self.equal_probability_enabled() or self._context is None or model is not getattr(self._context, "search_results", None):
                return original(model, active_ratings)
            row = self.pop_equal_row(model, active_ratings)
            if row is not None:
                return row
            return original(model, active_ratings)

        def replace_id_filter(original, model, active_ratings=None, allowed_ids=None):
            if not self.equal_probability_enabled() or self._context is None or model is not getattr(self._context, "search_results", None):
                return original(model, active_ratings, allowed_ids)
            if allowed_ids:
                row = self.pop_equal_row(model, active_ratings, allowed_ids=allowed_ids)
                if row is not None:
                    return row
            return original(model, active_ratings, allowed_ids)

        self.ext.patches.wrap_method(
            owner=self.id,
            target=SearchResultModel,
            method_name="pop_random_row",
            replace=replace_pop,
        )
        self.ext.patches.wrap_method(
            owner=self.id,
            target=SearchResultModel,
            method_name="pop_random_row_with_id_filter",
            replace=replace_id_filter,
        )

    # ------------------------------------------------------------------
    # Search panel UI
    # ------------------------------------------------------------------

    _SEARCH_PANEL_JS_MARKER = "/* NAIA_EXTEN_MULTI_PARQUET_POOL_V5 */"
    _SEARCH_PANEL_INJECTED_JS = r'''/* NAIA_EXTEN_MULTI_PARQUET_POOL_V5 */
(() => {
  if (window.__naiaExtenMultiParquetPoolV5) return;
  window.__naiaExtenMultiParquetPoolV5 = true;

  const EXT_ID = 'naia_exten';
  const ENABLED_KEY = 'feature__multi_parquet_pool__enabled';
  const SELECTED_KEY = 'feature__multi_parquet_pool__selected_parquets';
  const APPLY_NONCE_KEY = 'feature__multi_parquet_pool__apply_nonce';
  const APPLIED_NONCE_KEY = 'feature__multi_parquet_pool__applied_nonce';
  const STATUS_KEY = 'feature__multi_parquet_pool__status';
  const EQUAL_KEY = 'feature__multi_parquet_pool__equal_probability';
  const ACTION_KEY = 'feature__multi_parquet_pool__apply_selection';
  const ROOT_ID = 'naiaExtenMultiParquetPoolRow';
  const MODAL_ID = 'naiaExtenMultiParquetModal';
  const STYLE_ID = 'naiaExtenMultiParquetStyle';

  let localSelected = null;
  let pendingNonce = '';
  let cachedFiles = [];
  let applyPollTimer = null;

  function extensionState() {
    const list = Array.isArray(lastExtensionsState?.extensions)
      ? lastExtensionsState.extensions
      : [];
    return list.find(item => item?.id === EXT_ID) || null;
  }

  function featureEnabled() {
    const ext = extensionState();
    if (!ext || ext.status !== 'loaded' || ext.enabled === false) return false;
    const value = ext.settings?.[ENABLED_KEY];
    return value === undefined ? true : Boolean(value);
  }

  function storedSelected() {
    const ext = extensionState();
    const raw = ext?.settings?.[SELECTED_KEY];
    const values = Array.isArray(raw) ? raw : [];
    return [...new Set(values.map(v => String(v || '').trim()).filter(Boolean))];
  }

  function selectedValue() {
    return Array.isArray(localSelected) ? localSelected : storedSelected();
  }

  function equalProbabilityValue() {
    const ext = extensionState();
    return Boolean(ext?.settings?.[EQUAL_KEY]);
  }

  function setEqualProbability(checked) {
    const ext = extensionState();
    if (ext) {
      if (!ext.settings || typeof ext.settings !== 'object') ext.settings = {};
      ext.settings[EQUAL_KEY] = Boolean(checked);
    }
    setModuleParam('extensions', `setting:${EXT_ID}:${EQUAL_KEY}`, Boolean(checked));
  }

  function normalizeFiles(values) {
    return [...new Set((Array.isArray(values) ? values : [])
      .map(v => String(v || '').trim())
      .filter(name => name.toLowerCase().endsWith('.parquet')))]
      .sort((a, b) => a.localeCompare(b, undefined, {numeric: true, sensitivity: 'base'}));
  }

  function cacheAvailableFiles(message) {
    if (Array.isArray(message?.parquets)) cachedFiles = normalizeFiles(message.parquets);
  }

  function availableFiles() {
    if (cachedFiles.length) return cachedFiles.slice();
    // Hot-reload fallback only: read the already-rendered filename nodes. This does
    // not open/read any parquet file and runs only when the picker is clicked.
    const items = moduleBody?.querySelectorAll('.search-parquet-list .search-parquet-item') || [];
    cachedFiles = normalizeFiles([...items].map(el => el.textContent));
    return cachedFiles.slice();
  }

  function requestExtensionState() {
    try { requestModuleState('extensions'); } catch (_) {}
  }

  function installStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      #${ROOT_ID} {
        margin: 0 0 10px;
        padding: 9px 10px 10px;
        border: 1px solid var(--border-dim);
        border-radius: 8px;
        background: color-mix(in srgb, var(--bg-surface) 82%, transparent);
      }
      #${ROOT_ID} .naia-mp-head {
        display:flex; align-items:center; justify-content:space-between; gap:10px;
      }
      #${ROOT_ID} .naia-mp-title {
        font-size:12px; font-weight:700; color:var(--text-primary);
      }
      #${ROOT_ID} .naia-mp-pick {
        flex:0 0 auto; min-height:27px; padding:4px 10px;
        border:1px solid var(--border-dim); border-radius:7px;
        background:var(--bg-elevated); color:var(--text-primary); cursor:pointer;
        font-size:11px; font-weight:650;
      }
      #${ROOT_ID} .naia-mp-pick:hover { border-color:var(--accent); }
      #${ROOT_ID} .naia-mp-summary {
        margin-top:6px; font-size:10px; color:var(--text-dim); line-height:1.35;
      }
      #${ROOT_ID} .naia-mp-equal-row {
        display:flex; align-items:center; justify-content:space-between; gap:10px;
        margin-top:7px; padding-top:7px;
        border-top:1px solid color-mix(in srgb, var(--border-dim) 55%, transparent);
      }
      #${ROOT_ID} .naia-mp-equal-label {
        font-size:11px; font-weight:650; color:var(--text-secondary);
      }
      #${ROOT_ID} .naia-mp-equal-switch {
        position:relative; display:inline-block; flex:0 0 auto; width:38px; height:21px; cursor:pointer;
      }
      #${ROOT_ID} .naia-mp-equal-switch input {
        position:absolute; opacity:0; width:1px; height:1px; pointer-events:none;
      }
      #${ROOT_ID} .naia-mp-equal-track {
        position:absolute; inset:0; border:1px solid var(--border-dim); border-radius:999px;
        background:var(--bg-elevated); transition:background .15s ease,border-color .15s ease;
      }
      #${ROOT_ID} .naia-mp-equal-track::after {
        content:''; position:absolute; width:15px; height:15px; left:2px; top:2px; border-radius:50%;
        background:var(--text-muted); transition:transform .15s ease,background .15s ease;
      }
      #${ROOT_ID} .naia-mp-equal-switch input:checked + .naia-mp-equal-track {
        background:color-mix(in srgb, var(--accent) 72%, var(--bg-elevated)); border-color:var(--accent);
      }
      #${ROOT_ID} .naia-mp-equal-switch input:checked + .naia-mp-equal-track::after {
        transform:translateX(17px); background:#fff;
      }
      #${ROOT_ID} .naia-mp-status {
        display:none; margin-top:6px; padding:5px 7px; border-radius:6px;
        background:var(--bg-elevated); color:var(--text-secondary);
        font-size:10px; line-height:1.35; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
      }
      #${ROOT_ID} .naia-mp-status.show { display:block; }
      #${ROOT_ID} .naia-mp-status.loading::before {
        content:''; display:inline-block; width:8px; height:8px; margin-right:6px;
        border:1.5px solid currentColor; border-right-color:transparent; border-radius:50%;
        vertical-align:-1px; animation:naiaMpSpin .7s linear infinite;
      }
      @keyframes naiaMpSpin { to { transform:rotate(360deg); } }
      #${ROOT_ID} .naia-mp-files {
        display:flex; flex-wrap:wrap; gap:5px; margin-top:7px;
        max-height:76px; overflow:auto;
      }
      #${ROOT_ID} .naia-mp-chip {
        max-width:100%; padding:3px 7px; border-radius:999px;
        border:1px solid var(--border-dim); background:var(--bg-elevated);
        color:var(--text-secondary); font-size:10px; line-height:1.2;
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
      }
      #${ROOT_ID} .naia-mp-empty { color:var(--text-dim); font-size:10px; }
      #${ROOT_ID}.applying { opacity:.68; pointer-events:none; }
      #${ROOT_ID}.naia-mp-feature-off { opacity:.58; }
      #${ROOT_ID} button:disabled { cursor:default; opacity:.55; }

      #${MODAL_ID} {
        position:fixed; inset:0; z-index:10080; display:flex;
        align-items:center; justify-content:center; padding:18px;
        background:rgba(0,0,0,.58);
      }
      #${MODAL_ID} .naia-mp-modal-card {
        width:min(620px, 94vw); max-height:min(720px, 88vh);
        display:flex; flex-direction:column; overflow:hidden;
        border:1px solid var(--border-dim); border-radius:12px;
        background:var(--bg-surface); box-shadow:0 18px 55px rgba(0,0,0,.42);
      }
      #${MODAL_ID} .naia-mp-modal-head {
        display:flex; align-items:center; justify-content:space-between; gap:12px;
        padding:13px 14px 9px;
      }
      #${MODAL_ID} .naia-mp-modal-title { font-size:14px; font-weight:750; color:var(--text-primary); }
      #${MODAL_ID} .naia-mp-modal-sub { margin-top:3px; font-size:10px; color:var(--text-dim); }
      #${MODAL_ID} .naia-mp-close {
        border:0; background:transparent; color:var(--text-secondary); cursor:pointer;
        font-size:19px; line-height:1;
      }
      #${MODAL_ID} .naia-mp-tools {
        display:flex; gap:7px; align-items:center; padding:0 14px 9px;
      }
      #${MODAL_ID} .naia-mp-search {
        flex:1; min-width:0; padding:7px 9px; border:1px solid var(--border-dim);
        border-radius:7px; background:var(--bg-elevated); color:var(--text-primary);
        font-size:11px;
      }
      #${MODAL_ID} .naia-mp-small-btn {
        padding:6px 8px; border:1px solid var(--border-dim); border-radius:7px;
        background:var(--bg-elevated); color:var(--text-secondary); cursor:pointer;
        font-size:10px; white-space:nowrap;
      }
      #${MODAL_ID} .naia-mp-list {
        flex:1; min-height:170px; overflow:auto; padding:2px 14px 10px;
        border-top:1px solid color-mix(in srgb, var(--border-dim) 70%, transparent);
        border-bottom:1px solid color-mix(in srgb, var(--border-dim) 70%, transparent);
      }
      #${MODAL_ID} .naia-mp-item {
        display:flex; align-items:center; gap:9px; padding:7px 4px;
        border-bottom:1px solid color-mix(in srgb, var(--border-dim) 45%, transparent);
        color:var(--text-secondary); font-size:11px; cursor:pointer;
      }
      #${MODAL_ID} .naia-mp-item input { flex:0 0 auto; }
      #${MODAL_ID} .naia-mp-item span { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      #${MODAL_ID} .naia-mp-no-files { padding:24px 4px; text-align:center; color:var(--text-dim); font-size:11px; }
      #${MODAL_ID} .naia-mp-foot {
        display:flex; align-items:center; justify-content:space-between; gap:10px;
        padding:10px 14px 12px;
      }
      #${MODAL_ID} .naia-mp-count { font-size:10px; color:var(--text-dim); }
      #${MODAL_ID} .naia-mp-foot-actions { display:flex; gap:7px; }
      #${MODAL_ID} .naia-mp-cancel, #${MODAL_ID} .naia-mp-apply {
        padding:7px 12px; border-radius:7px; cursor:pointer; font-size:11px; font-weight:650;
      }
      #${MODAL_ID} .naia-mp-cancel {
        border:1px solid var(--border-dim); background:var(--bg-elevated); color:var(--text-secondary);
      }
      #${MODAL_ID} .naia-mp-apply {
        border:1px solid var(--accent); background:color-mix(in srgb, var(--accent) 72%, var(--bg-elevated)); color:#fff;
      }
    `;
    document.head.appendChild(style);
  }

  function setStatus(text, loading = false) {
    const status = document.getElementById(ROOT_ID)?.querySelector('.naia-mp-status');
    if (!status) return;
    const value = String(text || '').trim();
    status.textContent = value;
    status.classList.toggle('show', !!value);
    status.classList.toggle('loading', !!value && loading);
    status.title = value;
  }

  function renderRoot() {
    const root = document.getElementById(ROOT_ID);
    if (!root) return;
    const selected = selectedValue();
    const files = root.querySelector('.naia-mp-files');
    const summary = root.querySelector('.naia-mp-summary');
    const equal = equalProbabilityValue();
    if (summary) {
      summary.textContent = selected.length
        ? `선택 ${selected.length}개 · ${equal ? 'Parquet별 균등 랜덤' : '전체 행 비율 랜덤'}`
        : '선택된 parquet 없음';
    }
    const equalInput = root.querySelector('.naia-mp-equal-switch input');
    if (equalInput) {
      equalInput.checked = equal;
      equalInput.disabled = !featureEnabled() || !selected.length;
    }
    const pick = root.querySelector('.naia-mp-pick');
    if (pick) pick.disabled = !featureEnabled();
    if (files) {
      files.innerHTML = '';
      if (!selected.length) {
        const empty = document.createElement('span');
        empty.className = 'naia-mp-empty';
        empty.textContent = 'Parquet 선택 버튼에서 여러 파일을 고를 수 있습니다.';
        files.appendChild(empty);
      } else {
        selected.forEach(name => {
          const chip = document.createElement('span');
          chip.className = 'naia-mp-chip';
          chip.title = name;
          chip.textContent = name;
          files.appendChild(chip);
        });
      }
    }
  }

  function ensureRoot() {
    const existing = document.getElementById(ROOT_ID);
    const ext = extensionState();
    const available = Boolean(ext && ext.status === 'loaded' && ext.enabled !== false);
    if (!available) {
      closeModal();
      if (existing) existing.style.display = 'none';
      if (!ext) requestExtensionState();
      return;
    }
    if (currentModuleId !== 'search' || !moduleBody) {
      closeModal();
      if (existing) existing.style.display = 'none';
      return;
    }
    const topRow = moduleBody.querySelector('.search-top-row');
    if (!topRow) return;
    installStyle();

    let root = existing;
    const enabled = featureEnabled();
    if (!enabled) closeModal();
    if (!root) {
      root = document.createElement('div');
      root.id = ROOT_ID;
      root.innerHTML = `
        <div class="naia-mp-head">
          <span class="naia-mp-title">다중 Parquet 랜덤 풀</span>
          <button type="button" class="naia-mp-pick">Parquet 선택</button>
        </div>
        <div class="naia-mp-summary"></div>
        <div class="naia-mp-equal-row">
          <span class="naia-mp-equal-label">Parquet별 균등 확률</span>
          <label class="naia-mp-equal-switch" title="ON: parquet 파일을 먼저 동일 확률로 고른 뒤 그 파일 안에서 랜덤 추출">
            <input type="checkbox" aria-label="Parquet별 균등 확률">
            <span class="naia-mp-equal-track"></span>
          </label>
        </div>
        <div class="naia-mp-status"></div>
        <div class="naia-mp-files"></div>
      `;
      root.querySelector('.naia-mp-pick')?.addEventListener('click', openModal);
      root.querySelector('.naia-mp-equal-switch input')?.addEventListener('change', event => {
        const checked = Boolean(event.target?.checked);
        setEqualProbability(checked);
        renderRoot();
        // Pools created before v0.2.8 do not carry source attribution. Turning
        // equal mode ON rebuilds the already-selected pool once, with progress.
        if (checked && selectedValue().length) applySelection(selectedValue());
      });

      const syncRow = document.getElementById('naiaExtenParquetSyncSearchRow');
      (syncRow || topRow).insertAdjacentElement('afterend', root);
    } else {
      root.style.display = '';
      const syncRow = document.getElementById('naiaExtenParquetSyncSearchRow');
      if (syncRow && root.previousElementSibling !== syncRow) {
        syncRow.insertAdjacentElement('afterend', root);
      }
    }
    root.classList.toggle('naia-mp-feature-off', !enabled);
    renderRoot();
  }

  function closeModal() {
    document.getElementById(MODAL_ID)?.remove();
  }

  function updateModalCount(modal) {
    const checked = [...modal.querySelectorAll('.naia-mp-item input:checked')];
    const count = modal.querySelector('.naia-mp-count');
    const apply = modal.querySelector('.naia-mp-apply');
    if (count) count.textContent = `${checked.length}개 선택`;
    if (apply) apply.textContent = checked.length ? `선택 적용 (${checked.length})` : '선택 적용';
  }

  function filterModal(modal) {
    const q = String(modal.querySelector('.naia-mp-search')?.value || '').trim().toLowerCase();
    modal.querySelectorAll('.naia-mp-item').forEach(row => {
      const name = String(row.dataset.name || '').toLowerCase();
      row.style.display = !q || name.includes(q) ? '' : 'none';
    });
  }

  function openModal() {
    closeModal();
    const files = availableFiles();
    if (!files.length) {
      setStatus('Parquet 목록 불러오는 중…', true);
      try { requestModuleState('search'); } catch (_) {}
      if (typeof showToast === 'function') showToast('Parquet 목록을 불러오는 중입니다.', 'info');
      return;
    }
    setStatus('', false);

    const selected = new Set(selectedValue());
    const modal = document.createElement('div');
    modal.id = MODAL_ID;
    modal.innerHTML = `
      <div class="naia-mp-modal-card" role="dialog" aria-modal="true" aria-label="Parquet 선택">
        <div class="naia-mp-modal-head">
          <div><div class="naia-mp-modal-title">Parquet 선택</div>
          <div class="naia-mp-modal-sub">여러 파일을 선택합니다. 균등 확률은 Search 창의 토글로 정합니다.</div></div>
          <button type="button" class="naia-mp-close" title="닫기">×</button>
        </div>
        <div class="naia-mp-tools">
          <input class="naia-mp-search" type="text" placeholder="parquet 이름 검색">
          <button type="button" class="naia-mp-small-btn naia-mp-all">전체 선택</button>
          <button type="button" class="naia-mp-small-btn naia-mp-none">전체 해제</button>
        </div>
        <div class="naia-mp-list"></div>
        <div class="naia-mp-foot">
          <span class="naia-mp-count"></span>
          <div class="naia-mp-foot-actions">
            <button type="button" class="naia-mp-cancel">취소</button>
            <button type="button" class="naia-mp-apply">선택 적용</button>
          </div>
        </div>
      </div>
    `;
    const list = modal.querySelector('.naia-mp-list');
    files.forEach(name => {
      const label = document.createElement('label');
      label.className = 'naia-mp-item';
      label.dataset.name = name;
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = selected.has(name);
      const text = document.createElement('span');
      text.textContent = name;
      label.append(checkbox, text);
      checkbox.addEventListener('change', () => updateModalCount(modal));
      list.appendChild(label);
    });

    modal.addEventListener('pointerdown', event => {
      if (event.target === modal) closeModal();
    });
    modal.querySelector('.naia-mp-close')?.addEventListener('click', closeModal);
    modal.querySelector('.naia-mp-cancel')?.addEventListener('click', closeModal);
    modal.querySelector('.naia-mp-search')?.addEventListener('input', () => filterModal(modal));
    modal.querySelector('.naia-mp-all')?.addEventListener('click', () => {
      modal.querySelectorAll('.naia-mp-item').forEach(row => {
        if (row.style.display !== 'none') row.querySelector('input').checked = true;
      });
      updateModalCount(modal);
    });
    modal.querySelector('.naia-mp-none')?.addEventListener('click', () => {
      modal.querySelectorAll('.naia-mp-item').forEach(row => {
        if (row.style.display !== 'none') row.querySelector('input').checked = false;
      });
      updateModalCount(modal);
    });
    modal.querySelector('.naia-mp-apply')?.addEventListener('click', () => {
      const chosen = [...modal.querySelectorAll('.naia-mp-item input:checked')]
        .map(input => input.closest('.naia-mp-item')?.dataset.name)
        .filter(Boolean);
      applySelection(chosen);
      closeModal();
    });

    document.body.appendChild(modal);
    updateModalCount(modal);
    setTimeout(() => modal.querySelector('.naia-mp-search')?.focus(), 0);
  }

  function applySelection(chosen) {
    const ext = extensionState();
    if (!ext || ext.status !== 'loaded' || ext.enabled === false || !featureEnabled()) {
      requestExtensionState();
      if (typeof showToast === 'function') showToast('NAIA 추가 편의기능 상태를 불러오는 중입니다.', 'info');
      return;
    }

    localSelected = [...new Set(chosen)];
    renderRoot();
    const root = document.getElementById(ROOT_ID);
    root?.classList.add('applying');
    const summary = root?.querySelector('.naia-mp-summary');
    if (summary) summary.textContent = `선택 ${localSelected.length}개 · 풀 적용 중…`;
    setStatus('선택 저장 및 Parquet 불러오기 준비 중…', true);

    if (!ext.settings || typeof ext.settings !== 'object') ext.settings = {};
    ext.settings[SELECTED_KEY] = localSelected;
    pendingNonce = `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
    ext.settings[APPLY_NONCE_KEY] = pendingNonce;

    setModuleParam('extensions', `setting:${EXT_ID}:${SELECTED_KEY}`, localSelected);
    setModuleParam('extensions', `setting:${EXT_ID}:${APPLY_NONCE_KEY}`, pendingNonce);
    setModuleParam('extensions', `setting:${EXT_ID}:${ACTION_KEY}`, true);

    if (applyPollTimer) clearInterval(applyPollTimer);
    applyPollTimer = setInterval(() => {
      if (!pendingNonce) {
        clearInterval(applyPollTimer);
        applyPollTimer = null;
        return;
      }
      requestExtensionState();
    }, 650);
  }

  function onExtensionEcho() {
    const ext = extensionState();
    if (!ext) return;
    if (localSelected === null) renderRoot();

    const backendStatus = String(ext.settings?.[STATUS_KEY] || '').trim();
    if (backendStatus) setStatus(backendStatus, !!pendingNonce && !backendStatus.startsWith('완료') && !backendStatus.startsWith('오류'));

    if (pendingNonce && String(ext.settings?.[APPLIED_NONCE_KEY] || '') === pendingNonce) {
      pendingNonce = '';
      localSelected = null;
      if (applyPollTimer) {
        clearInterval(applyPollTimer);
        applyPollTimer = null;
      }
      document.getElementById(ROOT_ID)?.classList.remove('applying');
      renderRoot();
      setStatus(backendStatus || '랜덤 풀 적용 완료', false);
      try { requestModuleState('search'); } catch (_) {}
    }
  }

  // Hook the Search controller itself after its dynamic import resolves. We must
  // not observe moduleBody: changing our own row would recursively wake an observer
  // and can freeze the page. onSearchState is called for the real Search lifecycle,
  // so one bounded callback per state is enough.
  function installSearchLifecycleHook() {
    try {
      if (!searchPanelControl || searchPanelControl.__naiaExtenMultiParquetHookV4) return !!searchPanelControl;
      const originalOnSearchState = searchPanelControl.onSearchState.bind(searchPanelControl);
      searchPanelControl.onSearchState = function(m) {
        cacheAvailableFiles(m);
        const result = originalOnSearchState(m);
        queueMicrotask(() => {
          ensureRoot();
          onExtensionEcho();
        });
        return result;
      };
      searchPanelControl.__naiaExtenMultiParquetHookV4 = true;
      return true;
    } catch (_) {
      return false;
    }
  }

  try { searchPanelReady?.then(() => installSearchLifecycleHook()); } catch (_) {}
  let hookAttempts = 0;
  const hookTimer = setInterval(() => {
    hookAttempts += 1;
    if (installSearchLifecycleHook() || hookAttempts >= 20) clearInterval(hookTimer);
  }, 100);

  try {
    const originalRenderExtensions = renderExtensions;
    renderExtensions = function(m) {
      const result = originalRenderExtensions(m);
      queueMicrotask(() => {
        ensureRoot();
        onExtensionEcho();
      });
      return result;
    };
  } catch (_) {}

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.getElementById(MODAL_ID)) closeModal();
  });

  queueMicrotask(ensureRoot);
  setTimeout(ensureRoot, 250);
})();
'''

    def _patch_search_panel_frontend(self) -> None:
        try:
            self.ext.patches.add_web_injection(
                owner=self.id,
                file_name="app.js",
                marker=self._SEARCH_PANEL_JS_MARKER,
                content=self._SEARCH_PANEL_INJECTED_JS,
            )
        except Exception as exc:
            self.ctx.log(f"다중 Parquet Search UI unavailable: {exc}")
