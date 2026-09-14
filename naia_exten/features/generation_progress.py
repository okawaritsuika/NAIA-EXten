from __future__ import annotations

from .base_feature import BaseFeature


class GenerationProgressFeature(BaseFeature):
    """Control the generation indicator without changing generation requests."""

    id = "generation_progress"
    name = "생성 진행 막대"
    description = "화면 위쪽 생성 진행 막대를 부드럽게 표시하거나 숨깁니다."
    category = "화면 표시"
    order = 5
    default_enabled = True
    panel_toggle_visible = False

    JS_MARKER = "/* NAIA_EXTEN_GENERATION_PROGRESS_V1 */"
    INJECTED_JS = JS_MARKER + r'''
(() => {
  if (window.__naiaExtenGenerationProgressV1) return;
  window.__naiaExtenGenerationProgressV1 = true;

  const EXT = 'naia_exten';
  const KEY = 'feature__generation_progress__mode';
  const ENABLED_KEY = 'feature__generation_progress__enabled';
  const SMOOTH = '부드럽게';
  const HIDDEN = '숨기기';
  const NATIVE = 'NAIA 기본';

  // Wait for the host controllers, then wrap only their presentation methods.
  // No polling, DOM observer, or generation/API hook is needed.
  if (typeof generationProgressReady === 'undefined'
      || typeof extensionsPanelReady === 'undefined') return;
  Promise.all([generationProgressReady, extensionsPanelReady]).then(() => {
    const wrap = document.getElementById('genProgress');
    const bars = ['genProgressBar', 'genProgressBar2'].map(id => document.getElementById(id));
    if (!generationProgress || !extensionsPanel || !wrap || bars.some(bar => !bar)) return;

    const style = document.createElement('style');
    style.textContent = `
      #genProgress.naia-exten-progress-hidden { display: none !important; }
      #genProgress.naia-exten-progress-smooth > div {
        width: 100% !important;
        transform: scaleX(0);
        transform-origin: left center;
        transition: none !important;
        will-change: transform;
      }
    `;
    document.head.appendChild(style);

    const originalStart = generationProgress.start.bind(generationProgress);
    const originalFinish = generationProgress.finish.bind(generationProgress);
    const originalOnState = extensionsPanel.onState.bind(extensionsPanel);
    let mode = NATIVE;
    let running = Boolean(generating);
    let estimated = 12000;
    let animations = [];
    let finishTimer = null;

    function selectedMode() {
      const state = lastExtensionsState?.state || lastExtensionsState;
      const ext = state?.extensions?.find(item => item.id === EXT);
      if (!ext || ext.status !== 'loaded' || ext.enabled === false || ext.blocked
          || ext.settings?.[ENABLED_KEY] === false) return NATIVE;
      const selected = ext.settings?.[KEY] ?? SMOOTH;
      if (selected === SMOOTH && bars.every(bar => typeof bar.animate === 'function')) return SMOOTH;
      return selected === HIDDEN ? HIDDEN : NATIVE;
    }

    function cancelAnimations() {
      animations.forEach(animation => animation.cancel());
      animations = [];
    }

    function clearPresentation() {
      cancelAnimations();
      if (finishTimer !== null) window.clearTimeout(finishTimer);
      finishTimer = null;
      // originalFinish clears the host's private interval. Cancel its delayed
      // reset too, so switching modes cannot erase a newly started animation.
      if (window._progressFinishTimeout) {
        window.clearTimeout(window._progressFinishTimeout);
        window._progressFinishTimeout = null;
      }
      wrap.classList.remove('active', 'naia-exten-progress-smooth', 'naia-exten-progress-hidden');
      bars.forEach(bar => {
        bar.style.transition = 'none';
        bar.style.width = '0%';
      });
    }

    function animate(bar, from, to, duration, delay = 0) {
      const animation = bar.animate(
        [{transform: `scaleX(${from})`}, {transform: `scaleX(${to})`}],
        {duration, delay, easing: 'linear', fill: 'both'}
      );
      animations.push(animation);
      return animation;
    }

    function startCurrent() {
      if (mode === NATIVE) { originalStart(); return; }
      if (mode === HIDDEN) return;
      wrap.classList.add('active', 'naia-exten-progress-smooth');
      // Preserve NAIA's estimate and elapsed time, including mid-generation
      // preference changes. Transform timelines run without JS on every frame.
      estimated = genDurations.length
        ? genDurations.reduce((sum, value) => sum + value, 0) / genDurations.length
        : 12000;
      estimated = Math.max(1, estimated);
      const elapsed = Math.max(0, Date.now() - genStartTime);
      animate(bars[0], 0, 1, estimated).currentTime = elapsed;
      animate(bars[1], 0, 1, estimated, estimated).currentTime = elapsed;
    }

    function changeMode(next) {
      if (mode === NATIVE) originalFinish();
      clearPresentation();
      mode = next;
      wrap.classList.toggle('naia-exten-progress-hidden', mode === HIDDEN);
      if (running) startCurrent();
    }

    generationProgress.start = function() {
      running = true;
      changeMode(selectedMode());
    };

    generationProgress.finish = function() {
      running = false;
      if (mode === NATIVE) { originalFinish(); return; }
      if (mode === HIDDEN) return;
      cancelAnimations();
      if (finishTimer !== null) window.clearTimeout(finishTimer);
      const elapsed = Math.max(0, Date.now() - genStartTime);
      const primary = Math.min(elapsed / estimated, 1);
      const secondary = Math.max(0, Math.min((elapsed - estimated) / estimated, 1));
      animate(bars[0], primary, 1, 180);
      animate(bars[1], secondary, secondary, 180);
      finishTimer = window.setTimeout(clearPresentation, 400);
    };

    function syncMode() {
      const next = selectedMode();
      if (next !== mode) changeMode(next);
    }

    extensionsPanel.onState = function(state) {
      const result = originalOnState(state);
      syncMode();
      return result;
    };
    syncMode();
  }).catch(error => console.warn('NAIA EXten progress display unavailable', error));
})();
'''

    def panel_fields(self) -> list[dict]:
        return [{
            "key": "mode",
            "type": "select",
            "label": "생성 진행 막대",
            "options": ["부드럽게", "숨기기", "NAIA 기본"],
            "default": "부드럽게",
            "apply": "immediate",
            "help": "화면 위쪽 막대의 표시 방식입니다. 생성 중에도 바꿀 수 있으며 선택은 저장됩니다.",
        }]

    def register(self) -> None:
        self.ext.patches.add_web_injection(
            owner=self.id, file_name="app.js", marker=self.JS_MARKER, content=self.INJECTED_JS,
        )
