from __future__ import annotations

import json
import random
from typing import Any

from .base_feature import BaseFeature


class GSQEProbabilityFeature(BaseFeature):
    """Choose a rating first by user weights, then pop a random row in that rating."""

    id = "gsqe_probability"
    name = "GSQE 확률 분배"
    description = (
        "Tag Filter의 G/S/Q/E 랜덤 선택 확률을 직접 지정합니다. "
        "행 개수 비율이 아니라 설정한 비율로 rating을 먼저 뽑은 뒤 해당 rating에서 랜덤 추출합니다."
    )
    category = "Tag Filter"
    order = 30
    default_enabled = True

    RATINGS = ("g", "s", "q", "e")
    DEFAULT_WEIGHTS = {"g": 25, "s": 25, "q": 25, "e": 25}

    _PANEL_JS_MARKER = "/* NAIA_EXTEN_GSQE_PROBABILITY_V2 */"
    _PANEL_JS = r'''/* NAIA_EXTEN_GSQE_PROBABILITY_V2 */
(() => {
  if (window.__naiaExtenGSQEProbabilityV2) return;
  window.__naiaExtenGSQEProbabilityV2 = true;

  const EXT_ID = 'naia_exten';
  const ENABLED_KEY = 'feature__gsqe_probability__enabled';
  const FEATURE_PREFIX = 'feature__gsqe_probability__';
  const ROOT_ID = 'naiaExtenGSQEProbability';
  const STYLE_ID = 'naiaExtenGSQEProbabilityStyle';
  const R = ['g', 's', 'q', 'e'];
  const LABEL = {g: 'G', s: 'S', q: 'Q', e: 'E'};
  const DEFAULTS = {g: 25, s: 25, q: 25, e: 25};
  const MIN_PART = 1;

  let weights = {...DEFAULTS};
  let dragIndex = -1;
  let dragPointer = null;
  let saveTimer = null;
  let savingUntil = 0;
  let flashTimer = null;
  let lastSavedWeights = null;
  let initialized = false;

  function extensionState() {
    try {
      const list = Array.isArray(lastExtensionsState?.extensions)
        ? lastExtensionsState.extensions
        : [];
      return list.find(item => item?.id === EXT_ID) || null;
    } catch (_) {
      return null;
    }
  }

  function requestExtensionState() {
    try {
      if (typeof requestModuleState === 'function') requestModuleState('extensions');
    } catch (_) {}
  }

  function featureEnabled(ext) {
    const value = ext?.settings?.[ENABLED_KEY];
    return value === undefined ? true : Boolean(value);
  }

  function readRawWeights() {
    const ext = extensionState();
    const settings = ext?.settings || {};
    try {
      const bundled = settings[`${FEATURE_PREFIX}weights_json`];
      const parsed = typeof bundled === 'string' ? JSON.parse(bundled) : bundled;
      if (parsed && typeof parsed === 'object') return normalizeWeights(parsed);
    } catch (_) {}
    const out = {};
    for (const r of R) {
      const raw = Number(settings[`${FEATURE_PREFIX}${r}_pct`]);
      out[r] = Number.isFinite(raw) ? Math.max(0, raw) : DEFAULTS[r];
    }
    return normalizeWeights(out);
  }

  function normalizeWeights(raw) {
    const vals = R.map(r => Math.max(0, Number(raw?.[r]) || 0));
    let total = vals.reduce((a, b) => a + b, 0);
    if (total <= 0) return {...DEFAULTS};

    const scaled = vals.map(v => (v * 100) / total);
    const floored = scaled.map(v => Math.floor(v));
    let remain = 100 - floored.reduce((a, b) => a + b, 0);
    const order = scaled
      .map((v, i) => ({i, frac: v - Math.floor(v)}))
      .sort((a, b) => b.frac - a.frac);
    for (let n = 0; n < remain; n += 1) floored[order[n % order.length].i] += 1;

    return {g: floored[0], s: floored[1], q: floored[2], e: floored[3]};
  }

  function setLocalExtensionSettings(next) {
    const ext = extensionState();
    if (!ext) return;
    if (!ext.settings || typeof ext.settings !== 'object') ext.settings = {};
    for (const r of R) ext.settings[`${FEATURE_PREFIX}${r}_pct`] = Number(next[r]) || 0;
    ext.settings[`${FEATURE_PREFIX}weights_json`] = JSON.stringify(next);
  }

  function saveWeights() {
    const changed = R.filter(r => (
      !lastSavedWeights || Number(lastSavedWeights[r]) !== Number(weights[r])
    ));
    if (!changed.length) return;

    savingUntil = Date.now() + 700;
    setLocalExtensionSettings(weights);
    let saved = false;
    try {
      // Save the four values as one setting so one drag causes one disk write
      // and one Extensions-state broadcast instead of four full panel renders.
      saved = setModuleParam(
        'extensions',
        `setting:${EXT_ID}:${FEATURE_PREFIX}weights_json`,
        JSON.stringify(weights)
      ) !== false;
    } catch (_) {
      saved = false;
    }
    if (saved) {
      lastSavedWeights = {...weights};
      showSavedFlash();
    }
  }

  function scheduleSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      saveTimer = null;
      saveWeights();
    }, 80);
  }

  function installStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      #${ROOT_ID} {
        position: relative;
        width: 100%;
        margin: 9px 0 3px;
        padding-top: 15px;
        user-select: none;
        -webkit-user-select: none;
      }
      #${ROOT_ID} .naia-gsqe-title-row {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 8px;
        margin-bottom: 5px;
      }
      #${ROOT_ID} .naia-gsqe-title {
        color: var(--text-muted);
        font-size: 9px;
        font-weight: 700;
        letter-spacing: .35px;
        text-transform: uppercase;
      }
      #${ROOT_ID} .naia-gsqe-hint {
        color: var(--text-muted);
        font-size: 8px;
        opacity: .82;
      }
      #${ROOT_ID} .naia-gsqe-flash {
        position: absolute;
        top: -6px;
        right: 0;
        padding: 2px 6px;
        border: 1px solid color-mix(in srgb, #69d47d 55%, transparent);
        border-radius: 999px;
        background: color-mix(in srgb, #245c30 88%, transparent);
        color: #d8f7de;
        font-size: 9px;
        opacity: 0;
        pointer-events: none;
        transform: translateY(-3px);
        transition: opacity .12s ease, transform .12s ease;
      }
      #${ROOT_ID} .naia-gsqe-flash.show {
        opacity: 1;
        transform: translateY(0);
      }
      #${ROOT_ID} .naia-gsqe-bar-wrap {
        position: relative;
        width: 100%;
        height: 22px;
      }
      #${ROOT_ID} .naia-gsqe-bar {
        position: absolute;
        inset: 2px 0 2px;
        display: flex;
        overflow: hidden;
        border: 1px solid color-mix(in srgb, var(--border-dim) 80%, #fff 20%);
        border-radius: 6px;
        background: var(--bg-elevated);
        box-shadow: inset 0 0 0 1px rgba(0,0,0,.12);
      }
      #${ROOT_ID} .naia-gsqe-seg {
        min-width: 0;
        height: 100%;
        transition: opacity .12s ease;
      }
      #${ROOT_ID} .naia-gsqe-seg[data-r="g"] { background: #2e7d32; }
      #${ROOT_ID} .naia-gsqe-seg[data-r="s"] { background: #1565c0; }
      #${ROOT_ID} .naia-gsqe-seg[data-r="q"] { background: #e65100; }
      #${ROOT_ID} .naia-gsqe-seg[data-r="e"] { background: #c62828; }
      #${ROOT_ID} .naia-gsqe-seg.inactive { opacity: .28; }
      #${ROOT_ID} .naia-gsqe-handle {
        position: absolute;
        z-index: 5;
        top: -1px;
        width: 16px;
        height: 24px;
        margin-left: -8px;
        cursor: ew-resize;
        touch-action: none;
      }
      #${ROOT_ID} .naia-gsqe-handle::before {
        content: '▼';
        position: absolute;
        left: 50%;
        top: -14px;
        transform: translateX(-50%);
        color: var(--text-secondary);
        font-size: 9px;
        line-height: 1;
        text-shadow: 0 1px 2px rgba(0,0,0,.55);
      }
      #${ROOT_ID} .naia-gsqe-handle::after {
        content: '';
        position: absolute;
        left: 50%;
        top: 1px;
        bottom: 1px;
        width: 2px;
        transform: translateX(-50%);
        border-radius: 2px;
        background: rgba(255,255,255,.92);
        box-shadow: 0 0 0 1px rgba(0,0,0,.38), 0 0 5px rgba(0,0,0,.45);
      }
      #${ROOT_ID} .naia-gsqe-handle:hover::after,
      #${ROOT_ID} .naia-gsqe-handle.dragging::after {
        width: 3px;
        background: #fff;
      }
      #${ROOT_ID} .naia-gsqe-values {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 3px;
        margin-top: 4px;
      }
      #${ROOT_ID} .naia-gsqe-value {
        text-align: center;
        font-size: 10px;
        font-variant-numeric: tabular-nums;
        font-weight: 700;
        color: var(--text-secondary);
        white-space: nowrap;
      }
      #${ROOT_ID} .naia-gsqe-value b { color: var(--text-primary); }
      #${ROOT_ID} .naia-gsqe-value.inactive { opacity: .4; }
      #${ROOT_ID}.naia-gsqe-unavailable { opacity: .5; pointer-events: none; }
    `;
    document.head.appendChild(style);
  }

  function showSavedFlash() {
    const flash = document.querySelector(`#${ROOT_ID} .naia-gsqe-flash`);
    if (!flash) return;
    if (flashTimer) clearTimeout(flashTimer);
    flash.textContent = '비율 적용됨';
    flash.classList.add('show');
    flashTimer = setTimeout(() => {
      flash.classList.remove('show');
      flashTimer = null;
    }, 900);
  }

  function activeRatings() {
    const set = new Set();
    document.querySelectorAll('.tag-filter-rating-row .rating-btn[data-r]').forEach(btn => {
      if (btn.classList.contains('active')) set.add(String(btn.dataset.r || '').toLowerCase());
    });
    return set;
  }

  function boundaries() {
    return [weights.g, weights.g + weights.s, weights.g + weights.s + weights.q];
  }

  function render() {
    const root = document.getElementById(ROOT_ID);
    if (!root) return;
    const ext = extensionState();
    const available = Boolean(ext && ext.status === 'loaded' && ext.enabled !== false);
    const enabled = available && featureEnabled(ext);
    // Keep the probability UI mounted while this feature is OFF. The
    // extension-level switch is the only one that hides injected UI.
    root.classList.toggle('naia-gsqe-unavailable', !enabled);
    root.style.display = available ? '' : 'none';
    if (!ext || !available) requestExtensionState();

    const active = activeRatings();
    root.querySelectorAll('.naia-gsqe-seg').forEach(seg => {
      const r = seg.dataset.r;
      seg.style.width = `${weights[r]}%`;
      seg.classList.toggle('inactive', active.size > 0 && !active.has(r));
    });
    root.querySelectorAll('.naia-gsqe-value').forEach(el => {
      const r = el.dataset.r;
      const n = Number(weights[r]) || 0;
      el.innerHTML = `<b>${LABEL[r]}</b> ${n}%`;
      el.classList.toggle('inactive', active.size > 0 && !active.has(r));
    });
    const bs = boundaries();
    root.querySelectorAll('.naia-gsqe-handle').forEach((handle, i) => {
      handle.style.left = `${bs[i]}%`;
    });
  }

  function updateBoundary(index, clientX) {
    const root = document.getElementById(ROOT_ID);
    const wrap = root?.querySelector('.naia-gsqe-bar-wrap');
    if (!wrap) return;
    const rect = wrap.getBoundingClientRect();
    if (!rect.width) return;
    const p = Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100));

    let b = boundaries();
    const lower = index === 0 ? MIN_PART : b[index - 1] + MIN_PART;
    const upper = index === 2 ? 100 - MIN_PART : b[index + 1] - MIN_PART;
    b[index] = Math.max(lower, Math.min(upper, Math.round(p)));

    weights = {
      g: b[0],
      s: b[1] - b[0],
      q: b[2] - b[1],
      e: 100 - b[2],
    };
    render();
  }

  function onPointerMove(event) {
    if (dragIndex < 0 || (dragPointer !== null && event.pointerId !== dragPointer)) return;
    event.preventDefault();
    updateBoundary(dragIndex, event.clientX);
  }

  function stopDrag(event) {
    if (dragIndex < 0) return;
    if (dragPointer !== null && event?.pointerId != null && event.pointerId !== dragPointer) return;
    const root = document.getElementById(ROOT_ID);
    root?.querySelectorAll('.naia-gsqe-handle').forEach(h => h.classList.remove('dragging'));
    dragIndex = -1;
    dragPointer = null;
    saveWeights();
  }

  function startDrag(index, event) {
    dragIndex = index;
    dragPointer = event.pointerId;
    event.currentTarget.classList.add('dragging');
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch (_) {}
    updateBoundary(index, event.clientX);
    event.preventDefault();
  }

  function ensureUI() {
    const row = document.querySelector('.tag-filter-rating-row');
    const ratingBar = row?.querySelector('.module-rating-bar');
    if (!row || !ratingBar) return;

    installStyle();
    let root = document.getElementById(ROOT_ID);
    if (!root) {
      root = document.createElement('div');
      root.id = ROOT_ID;
      root.innerHTML = `
        <span class="naia-gsqe-flash" role="status" aria-live="polite"></span>
        <div class="naia-gsqe-title-row">
          <span class="naia-gsqe-title">GSQE Probability</span>
          <span class="naia-gsqe-hint">▼ 경계 드래그 · OFF 등급 제외</span>
        </div>
        <div class="naia-gsqe-bar-wrap">
          <div class="naia-gsqe-bar">
            <span class="naia-gsqe-seg" data-r="g"></span>
            <span class="naia-gsqe-seg" data-r="s"></span>
            <span class="naia-gsqe-seg" data-r="q"></span>
            <span class="naia-gsqe-seg" data-r="e"></span>
          </div>
          <span class="naia-gsqe-handle" data-index="0" role="slider" aria-label="G와 S 확률 경계"></span>
          <span class="naia-gsqe-handle" data-index="1" role="slider" aria-label="S와 Q 확률 경계"></span>
          <span class="naia-gsqe-handle" data-index="2" role="slider" aria-label="Q와 E 확률 경계"></span>
        </div>
        <div class="naia-gsqe-values">
          <span class="naia-gsqe-value" data-r="g"></span>
          <span class="naia-gsqe-value" data-r="s"></span>
          <span class="naia-gsqe-value" data-r="q"></span>
          <span class="naia-gsqe-value" data-r="e"></span>
        </div>`;
      row.insertAdjacentElement('afterend', root);

      root.querySelectorAll('.naia-gsqe-handle').forEach((handle, index) => {
        handle.addEventListener('pointerdown', event => startDrag(index, event));
        handle.addEventListener('keydown', event => {
          if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
          const delta = event.key === 'ArrowLeft' ? -1 : 1;
          const b = boundaries();
          const rect = root.querySelector('.naia-gsqe-bar-wrap').getBoundingClientRect();
          const px = rect.left + ((b[index] + delta) / 100) * rect.width;
          updateBoundary(index, px);
          scheduleSave();
          event.preventDefault();
        });
        handle.tabIndex = 0;
      });
    }

    if (!initialized) {
      weights = readRawWeights();
      lastSavedWeights = {...weights};
      initialized = true;
    }
    render();
  }

  window.addEventListener('pointermove', onPointerMove, {passive: false});
  window.addEventListener('pointerup', stopDrag);
  window.addEventListener('pointercancel', stopDrag);

  // Rating on/off changes effective candidates; dim disabled ratings immediately.
  document.addEventListener('click', event => {
    if (event.target?.closest?.('.tag-filter-rating-row .rating-btn')) {
      queueMicrotask(render);
    }
  }, true);

  try {
    const previousRenderExtensions = renderExtensions;
    renderExtensions = function(m) {
      const result = previousRenderExtensions(m);
      queueMicrotask(() => {
        if (Date.now() >= savingUntil) {
          weights = readRawWeights();
          lastSavedWeights = {...weights};
        }
        ensureUI();
      });
      return result;
    };
  } catch (_) {}

  queueMicrotask(ensureUI);
  setTimeout(ensureUI, 250);
  setTimeout(ensureUI, 1000);
})();
'''

    def __init__(self):
        super().__init__()
        self._context = None

    def panel_fields(self):
        fields = [
            {
                "key": "weights_json",
                "type": "text",
                "default": "",
                "label": "GSQE 확률 묶음",
                "help": "Tag Filter 확률 바의 내부 저장값입니다.",
                "visible_when": {"field": "__internal_never__", "in": ["1"]},
                "order": self.order * 100,
            }
        ]
        for index, rating in enumerate(self.RATINGS, start=1):
            fields.append(
                {
                    "key": f"{rating}_pct",
                    "type": "int",
                    "default": self.DEFAULT_WEIGHTS[rating],
                    "min": 0,
                    "max": 100,
                    "step": 1,
                    "label": f"{rating.upper()} 확률",
                    "help": "Tag Filter의 GSQE 확률 바에서 조절합니다.",
                    # Register the setting with the host so setModuleParam validates/saves it,
                    # but do not duplicate these fields in the generic NAIA EXten panel.
                    "visible_when": {"field": "__internal_never__", "in": ["1"]},
                    "order": self.order * 100 + index,
                }
            )
        return fields

    def register(self) -> None:
        app_context = self.ext.host.app_context
        if app_context is None:
            self.ctx.log("GSQE 확률 분배: NAIA context를 찾지 못해 비활성화됩니다.")
            return

        self._context = app_context
        self._patch_random_selection()
        self._patch_tag_filter_selection()
        self._patch_prefetch_selection()
        self._patch_frontend()
        self.ctx.log("GSQE probability feature registered")

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

    def _weights(self) -> dict[str, float]:
        defaults = {f"{rating}_pct": value for rating, value in self.DEFAULT_WEIGHTS.items()}
        defaults["weights_json"] = ""
        settings = self.load_settings(defaults)
        bundled = settings.get(self.key("weights_json"))
        try:
            if isinstance(bundled, str) and bundled.strip():
                bundled = json.loads(bundled)
        except (TypeError, ValueError, json.JSONDecodeError):
            bundled = None
        if not isinstance(bundled, dict):
            bundled = {}

        values: dict[str, float] = {}
        for rating in self.RATINGS:
            try:
                value = float(
                    bundled.get(
                        rating,
                        settings.get(self.key(f"{rating}_pct"), self.DEFAULT_WEIGHTS[rating]),
                    )
                )
            except (TypeError, ValueError):
                value = float(self.DEFAULT_WEIGHTS[rating])
            values[rating] = max(0.0, value)
        if sum(values.values()) <= 0:
            return {rating: float(value) for rating, value in self.DEFAULT_WEIGHTS.items()}
        return values

    @classmethod
    def _normalize_active_ratings(cls, active_ratings: Any) -> set[str]:
        if not active_ratings:
            return set(cls.RATINGS)
        try:
            raw = set(active_ratings)
        except TypeError:
            raw = {active_ratings}
        normalized = {str(item).strip().lower() for item in raw}
        return {rating for rating in cls.RATINGS if rating in normalized}

    def _choose_rating(
        self,
        active_ratings: Any,
        counts: dict[str, Any] | None,
        *,
        excluded: set[str] | None = None,
        weights: dict[str, float] | None = None,
    ) -> str | None:
        active = self._normalize_active_ratings(active_ratings)
        excluded = excluded or set()
        weights = weights or self._weights()

        candidates: list[tuple[str, float]] = []
        for rating in self.RATINGS:
            if rating not in active or rating in excluded:
                continue
            try:
                count = int((counts or {}).get(rating, 0) or 0)
            except (TypeError, ValueError):
                count = 0
            if count <= 0:
                continue
            weight = max(0.0, float(weights.get(rating, 0.0) or 0.0))
            if weight > 0:
                candidates.append((rating, weight))

        if not candidates:
            return None

        total = sum(weight for _, weight in candidates)
        target = random.random() * total
        for rating, weight in candidates:
            target -= weight
            if target < 0:
                return rating
        return candidates[-1][0]

    def _pop_weighted_random_row(self, search_results, active_ratings: Any):
        """Pop with rating weights, retrying another rating if a stale count has no valid row."""
        if search_results is None:
            return None

        # Read settings once per selection. Previously retries and the
        # multi-parquet branch repeatedly parsed settings.json on the hot path.
        weights = self._weights()

        # When multi-parquet equal mode is ON, source choice has priority: choose
        # one parquet uniformly first, then apply the user's GSQE weights inside
        # that parquet. This keeps A/B at 50/50 even when their row counts or
        # rating distributions differ dramatically.
        try:
            multi = self.ext.features.get("multi_parquet_pool")
            if multi is not None and multi.equal_probability_enabled():
                row = multi.pop_equal_row(
                    search_results,
                    active_ratings,
                    rating_weights=weights,
                )
                if row is not None:
                    return row
        except Exception:
            pass

        get_counts = getattr(search_results, "get_count_by_rating", None)
        pop = getattr(search_results, "pop_random_row", None)
        if not callable(get_counts) or not callable(pop):
            return None

        counts = get_counts() or {}
        excluded: set[str] = set()
        for _ in self.RATINGS:
            rating = self._choose_rating(
                active_ratings,
                counts,
                excluded=excluded,
                weights=weights,
            )
            if rating is None:
                break
            row = pop({rating})
            if row is not None:
                return row
            excluded.add(rating)
        return None

    # ------------------------------------------------------------------
    # Normal Random / Generate source selection
    # ------------------------------------------------------------------

    def _patch_random_selection(self) -> None:
        from core.prompt_generation_service import PromptGenerationService, PromptSourcePreparation

        def replace(original, service, search_results, settings, active_ratings=None, source_row_override=None):
            if not self._runtime_active() or service.app_context is not self._context:
                return original(service, search_results, settings, active_ratings, source_row_override)
            if settings.get("wildcard_standalone", False) or source_row_override is not None:
                return original(service, search_results, settings, active_ratings, source_row_override)

            row = self._pop_weighted_random_row(search_results, active_ratings)
            if row is None:
                return original(service, search_results, settings, active_ratings, source_row_override)
            return PromptSourcePreparation(
                source_row=row,
                remaining_count=search_results.get_count(),
            )

        self.ext.patches.wrap_method(
            owner=self.id,
            target=PromptGenerationService,
            method_name="prepare_next_source",
            replace=replace,
        )

    # ------------------------------------------------------------------
    # Active Tag Filter source selection
    # ------------------------------------------------------------------

    def _patch_tag_filter_selection(self) -> None:
        from core.headless_random_prompt_service import HeadlessRandomPromptService

        def replace(original, service, active_ratings):
            if not self._runtime_active() or service.context is not self._context:
                return original(service, active_ratings)

            tag_filter = service._active_tag_filter_state()
            if not isinstance(tag_filter, dict):
                return original(service, active_ratings)

            counts = tag_filter.get("rating_counts")
            if not isinstance(counts, dict):
                return original(service, active_ratings)

            weights = self._weights()

            # Same source-first rule for an active Tag Filter. The allowed id set
            # is already computed by NAIA, so the multi-parquet feature can choose
            # an equal-probability source without re-running the tag search.
            try:
                multi = self.ext.features.get("multi_parquet_pool")
                ids = tag_filter.get("ids")
                search_results = getattr(service.context, "search_results", None)
                if (
                    multi is not None
                    and multi.equal_probability_enabled()
                    and search_results is not None
                    and isinstance(ids, set)
                    and ids
                ):
                    row = multi.pop_equal_row(
                        search_results,
                        active_ratings,
                        allowed_ids=ids,
                        rating_weights=weights,
                    )
                    if row is not None:
                        if service._consume_active_tag_filter_row(tag_filter, row):
                            return row, service._tag_filter_update_payload(tag_filter), ""
            except Exception:
                pass

            excluded: set[str] = set()
            for _ in self.RATINGS:
                rating = self._choose_rating(
                    active_ratings,
                    counts,
                    excluded=excluded,
                    weights=weights,
                )
                if rating is None:
                    break
                result = original(service, {rating})
                try:
                    row, _payload, error = result
                except Exception:
                    return result
                if row is not None and not error:
                    return result
                excluded.add(rating)

            return original(service, active_ratings)

        self.ext.patches.wrap_method(
            owner=self.id,
            target=HeadlessRandomPromptService,
            method_name="_pop_active_tag_filter_source_row",
            replace=replace,
        )

    # ------------------------------------------------------------------
    # Auto-Generate prefetch (this path pops a row before prepare_next_source)
    # ------------------------------------------------------------------

    def _patch_prefetch_selection(self) -> None:
        from core.headless_random_prompt_service import HeadlessRandomPromptService

        def replace(original, service, active_ratings=None):
            if not self._runtime_active() or service.context is not self._context:
                return original(service, active_ratings)
            try:
                settings = service._random_settings(None)
                if settings.get("wildcard_standalone", False):
                    return None
                if not service._ensure_search_results(settings):
                    return None
                search_results = getattr(service.context, "search_results", None)
                if search_results is None:
                    return None
                normalized = service._normalize_ratings(active_ratings)
                row = self._pop_weighted_random_row(search_results, normalized)
                if row is not None:
                    return row
                return search_results.pop_random_row(normalized)
            except Exception:
                return original(service, active_ratings)

        self.ext.patches.wrap_method(
            owner=self.id,
            target=HeadlessRandomPromptService,
            method_name="reserve_next_random_row",
            replace=replace,
        )

    # ------------------------------------------------------------------
    # Tag Filter popup UI
    # ------------------------------------------------------------------

    def _patch_frontend(self) -> None:
        self.ext.patches.add_web_injection(
            owner=self.id,
            file_name="app.js",
            marker=self._PANEL_JS_MARKER,
            content=self._PANEL_JS,
        )
