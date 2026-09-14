const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const feature = fs.readFileSync(path.join(__dirname, '../naia_exten/features/generation_progress.py'), 'utf8');
const script = feature.split("INJECTED_JS = JS_MARKER + r'''")[1].split("'''")[0];
// Exercise cleanup against NAIA's real interval/finish-timeout implementation.
const nativeSource = fs.readFileSync(path.resolve(__dirname,
  '../../../../resources/naia-backend/app/web/remote/js/features/generationProgress.mjs'), 'utf8')
  .replace('export function', 'function');

async function setup(selected = '부드럽게', durations = []) {
  let now = 1000;
  let nextId = 1;
  const intervals = new Map();
  const timeouts = new Map();
  const createdAnimations = [];
  const elements = new Map(['genProgress', 'genProgressBar', 'genProgressBar2'].map(id => {
    const classes = new Set();
    return [id, {
      style: {},
      classList: {
        add: (...items) => items.forEach(item => classes.add(item)),
        remove: (...items) => items.forEach(item => classes.delete(item)),
        toggle: (item, enabled) => enabled ? classes.add(item) : classes.delete(item),
        contains: item => classes.has(item),
      },
      animate(frames, timing) {
        const animation = {id, frames, timing, currentTime: 0, cancelled: false,
          cancel() { this.cancelled = true; }};
        createdAnimations.push(animation);
        return animation;
      },
    }];
  }));
  const ext = {id: 'naia_exten', status: 'loaded', enabled: true,
    settings: selected === null ? {} : {feature__generation_progress__mode: selected}};
  const context = vm.createContext({
    console,
    Date: {now: () => now},
    window: {
      setInterval: callback => { const id = nextId++; intervals.set(id, callback); return id; },
      clearInterval: id => intervals.delete(id),
      setTimeout: (callback, delay) => { const id = nextId++; timeouts.set(id, {callback, at: now + delay}); return id; },
      clearTimeout: id => timeouts.delete(id),
    },
    document: {getElementById: id => elements.get(id), createElement: () => ({}), head: {appendChild() {}}},
    generating: false,
    genStartTime: now,
    genDurations: durations,
    lastExtensionsState: {state: {extensions: [ext]}},
    extensionsPanel: {onState: () => 'host-state-result'},
    extensionsPanelReady: Promise.resolve(),
    generationProgressReady: Promise.resolve(),
  });
  vm.runInContext(nativeSource + '\ngenerationProgress = createGenerationProgress({document, window, '
    + 'getGenStartTime: () => genStartTime, getDurations: () => genDurations});', context);
  vm.runInContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  return {
    context, ext, intervals, timeouts, elements, createdAnimations,
    activeAnimations: () => createdAnimations.filter(animation => !animation.cancelled),
    start() { context.genStartTime = now; context.generating = true; context.generationProgress.start(); },
    finish() { context.generating = false; context.generationProgress.finish(); },
    state() { return context.extensionsPanel.onState(context.lastExtensionsState); },
    mode(value) { ext.settings.feature__generation_progress__mode = value; this.state(); },
    advance(ms) {
      now += ms;
      for (const [id, timer] of [...timeouts]) {
        if (timer.at <= now) { timeouts.delete(id); timer.callback(); }
      }
    },
  };
}

test('default smooth display uses animation timelines with NAIA estimates and no interval', async () => {
  const app = await setup(null, [6000, 10000]);
  app.start();
  assert.equal(app.intervals.size, 0);
  assert.equal(app.timeouts.size, 0);
  assert.equal(app.activeAnimations().length, 2);
  assert.equal(app.activeAnimations()[0].timing.duration, 8000);
  assert.equal(app.activeAnimations()[1].timing.delay, 8000);
  assert.equal(app.activeAnimations()[0].frames[1].transform, 'scaleX(1)');
  const before = [...app.activeAnimations()];
  assert.equal(app.state(), 'host-state-result');
  assert.deepEqual(app.activeAnimations(), before, 'unrelated state updates must not restart animation');
});

test('switching native -> hidden -> smooth retains elapsed time and removes native timers', async () => {
  const app = await setup('NAIA 기본');
  app.start();
  assert.equal(app.intervals.size, 1);
  app.advance(6000);
  app.mode('숨기기');
  assert.equal(app.intervals.size, 0);
  assert.equal(app.timeouts.size, 0);
  assert.equal(app.activeAnimations().length, 0);
  assert.equal(app.elements.get('genProgress').classList.contains('naia-exten-progress-hidden'), true);
  app.mode('부드럽게');
  assert.equal(app.activeAnimations()[0].currentTime, 6000);
  app.advance(500);
  assert.equal(app.elements.get('genProgress').classList.contains('active'), true);
  app.ext.enabled = false;
  app.state();
  assert.equal(app.activeAnimations().length, 0);
  assert.equal(app.intervals.size, 1);
  assert.equal(app.elements.get('genProgress').classList.contains('naia-exten-progress-smooth'), false);
});

test('completion resets its animations but cannot clear a subsequent generation', async () => {
  const app = await setup();
  app.start();
  app.advance(15000);
  app.finish();
  assert.equal(app.activeAnimations()[1].frames[0].transform, 'scaleX(0.25)');
  assert.equal(app.timeouts.size, 1);
  app.advance(100);
  app.start();
  assert.equal(app.timeouts.size, 0);
  app.advance(500);
  assert.equal(app.elements.get('genProgress').classList.contains('active'), true);
  app.finish();
  app.advance(400);
  assert.equal(app.activeAnimations().length, 0);
  assert.equal(app.timeouts.size, 0);
  assert.equal(app.elements.get('genProgress').classList.contains('active'), false);
});

test('hidden mode stays hidden through completion; unavailability restores the native display', async () => {
  const app = await setup('숨기기');
  app.start();
  app.finish();
  assert.equal(app.intervals.size + app.timeouts.size + app.activeAnimations().length, 0);
  assert.equal(app.elements.get('genProgress').classList.contains('naia-exten-progress-hidden'), true);
  app.context.lastExtensionsState.state.extensions = [];
  app.state();
  assert.equal(app.elements.get('genProgress').classList.contains('naia-exten-progress-hidden'), false);
  app.start();
  assert.equal(app.intervals.size, 1);
  app.finish();
  app.advance(400);
  assert.equal(app.intervals.size + app.timeouts.size, 0);
});

test('mode changes during completion cancel the old reset; duplicate injection adds no hooks', async () => {
  const app = await setup();
  const start = app.context.generationProgress.start;
  const onState = app.context.extensionsPanel.onState;
  vm.runInContext(script, app.context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.context.generationProgress.start, start);
  assert.equal(app.context.extensionsPanel.onState, onState);
  app.start();
  app.finish();
  app.mode('숨기기');
  app.advance(500);
  assert.equal(app.timeouts.size, 0);
  assert.equal(app.elements.get('genProgress').classList.contains('naia-exten-progress-hidden'), true);
});
