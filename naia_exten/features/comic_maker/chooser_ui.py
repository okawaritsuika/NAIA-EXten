CHOOSER_MARKER = "/* NAIA_EXTEN_COMIC_CHOOSER_V1 */"
CHOOSER_JS = CHOOSER_MARKER + r"""
(() => {
  if (window.__naiaComicChooserV1) return;
  window.__naiaComicChooserV1 = true;
  const EXT = 'naia_exten';
  const PREFIX = 'feature__comic_maker__';
  const ID = 'naiaComicChooser';
  let dialog = null;
  let timer = null;
  let opener = null;
  let localError = '';

  function extension() {
    return (lastExtensionsState?.extensions || []).find(item => item.id === EXT);
  }
  function close() {
    clearInterval(timer);
    timer = null;
    if (dialog) {
      const previous = dialog;
      dialog = null;
      previous.close();
      previous.remove();
    }
    if (opener?.isConnected) opener.focus();
  }
  function sync() {
    if (!dialog) return;
    const ext = extension();
    const field = ext?.panel?.fields?.find(item => item.key === PREFIX + 'chooser_state');
    let data = {};
    try { data = JSON.parse(field?.placeholder || '{}'); } catch (_) {}
    const available = ext?.status === 'loaded' && ext.enabled !== false && !ext.blocked;
    const mode = dialog.querySelector('[name="comic-mode"]:checked').value;
    const start = dialog.querySelector('.comic-start');
    start.disabled = !available || !!data.nai_busy;
    start.textContent = mode === 'large' ? 'NAI로 큰 화면 만들기' : 'NAI로 만화 만들기';
    dialog.querySelector('.comic-message').textContent = localError || (!available
      ? '확장 연결을 확인해주세요.' : data.nai_busy
      ? 'NAI 작업이 진행 중입니다. 완료하거나 해당 작업을 중지한 뒤 시작하세요.' : '');
  }
  function open(button) {
    if (dialog) { dialog.focus(); return; }
    opener = button;
    localError = '';
    dialog = document.createElement('dialog');
    dialog.id = ID;
    dialog.setAttribute('aria-labelledby', 'naiaComicChooserTitle');
    dialog.innerHTML = `
      <form>
        <header><div><span class="comic-eyebrow">COMIC MAKER</span>
          <h2 id="naiaComicChooserTitle">NAI 만화 만들기</h2></div>
          <button type="button" class="comic-close" aria-label="닫기">×</button></header>
        <p class="comic-intro">사용할 방식을 선택하세요. 현재 캐릭터와 NAIA 해상도를 사용합니다.</p>
        <div class="comic-methods">
          <label><input type="radio" name="comic-mode" value="nai" checked>
            <span><strong>일반 만화</strong><small>한 페이지의 여러 패널을 한 장의 이미지로 만듭니다.</small></span></label>
          <label><input type="radio" name="comic-mode" value="large">
            <span><strong>큰 화면</strong><small>각 패널을 현재 NAIA 해상도의 독립된 이미지로 만듭니다.</small></span></label>
        </div>
        <p class="comic-message" role="status" aria-live="polite"></p>
        <footer><button type="button" class="comic-cancel">취소</button>
          <button type="submit" class="comic-start">NAI로 만화 만들기</button></footer>
      </form>`;
    dialog.querySelector('.comic-close').addEventListener('click', close);
    dialog.querySelector('.comic-cancel').addEventListener('click', close);
    // Keep the underlying EXten popup open for progress.
    dialog.addEventListener('mousedown', event => event.stopPropagation());
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close();
    });
    dialog.querySelectorAll('[name="comic-mode"]').forEach(input => input.addEventListener('change', sync));
    dialog.querySelector('form').addEventListener('submit', event => {
      event.preventDefault();
      sync();
      if (dialog.querySelector('.comic-start').disabled) return;
      const mode = dialog.querySelector('[name="comic-mode"]:checked').value;
      const action = mode === 'large' ? 'make_ja' : 'make_nai';
      if (setModuleParam('extensions', `setting:${EXT}:${PREFIX}${action}`, true) !== false) close();
      else { localError = '연결을 확인해주세요.'; sync(); }
    });
    document.body.appendChild(dialog);
    dialog.showModal();
    sync();
    timer = setInterval(() => { requestModuleState('extensions'); sync(); }, 1000);
  }
  const style = document.createElement('style');
  style.textContent = `
    #${ID} { width:min(560px, calc(100vw - 32px)); max-height:calc(100vh - 32px); margin:auto;
      padding:24px; border:1px solid var(--border-dim, #414456); border-radius:16px;
      background:var(--bg-surface, #20222b); color:var(--text-primary, #f0f0f5);
      box-shadow:0 24px 90px #0008; font:13px/1.5 sans-serif; box-sizing:border-box; }
    #${ID}::backdrop { background:#0009; }
    #${ID} [hidden] { display:none !important; }
    #${ID} header, #${ID} footer { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    #${ID} h2 { margin:3px 0 0; font-size:21px; color:inherit; }
    #${ID} .comic-eyebrow { color:var(--accent-light, #b9a2ff); font-size:10px; letter-spacing:1.5px; font-weight:700; }
    #${ID} .comic-intro, #${ID} small { color:var(--text-secondary, #b3b4c3); }
    #${ID} .comic-intro { margin:16px 0; }
    #${ID} .comic-methods { display:grid; gap:10px; }
    #${ID} .comic-methods > label { display:flex; gap:11px; align-items:flex-start; padding:13px;
      border:1px solid var(--border-dim, #414456); border-radius:10px; cursor:pointer; }
    #${ID} .comic-methods > label:has(input:checked) { border-color:var(--accent, #9774ef); background:var(--bg-elevated, #2b293b); }
    #${ID} input[type=radio] { margin:4px 0 0; accent-color:var(--accent, #9774ef); }
    #${ID} strong, #${ID} small { display:block; }
    #${ID} small { font-size:12px; margin-top:4px; }
    #${ID} .comic-message { color:var(--accent-light, #c9b4ff); white-space:pre-wrap; overflow-wrap:anywhere; margin:12px 0; }
    #${ID} button { cursor:pointer; padding:8px 12px; border:1px solid var(--border-dim, #414456);
      border-radius:7px; background:var(--bg-elevated, #2b2d38); color:inherit; font:inherit; }
    #${ID} button:disabled { cursor:default; opacity:.5; }
    #${ID} button:focus-visible { outline:2px solid var(--accent, #9774ef); outline-offset:2px; }
    #${ID} .comic-close { border:0; background:transparent; font-size:22px; padding:0 5px; }
    #${ID} footer { justify-content:flex-end; border-top:1px solid var(--border-dim, #414456); margin-top:16px; padding-top:16px; }
    #${ID} .comic-start { background:var(--accent, #8060d0); color:white; border-color:transparent; font-weight:700; }
  `;
  document.head.appendChild(style);
  document.addEventListener('click', event => {
    const button = event.target.closest?.(`[data-ext="${EXT}"][data-action-field="${PREFIX}make"]`);
    if (!button) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    open(button);
  }, true);
  const originalRender = renderExtensions;
  renderExtensions = function(state) {
    const result = originalRender(state);
    sync();
    return result;
  };
})();
"""
