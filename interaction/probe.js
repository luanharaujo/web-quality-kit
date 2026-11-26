// Runs inside the page (Playwright addInitScript). Watches what the person
// does and what the page shows right after: the control they activated,
// when it first showed a busy state, success words, form values, files.
// The fixture (index.mjs) reads window.__kit; nothing here changes the app.
(cfg) => {
  if (window.__kit) return;
  const SUCCESS = new RegExp(cfg.success, 'gi');
  const LIVE = '[role=status],[role=alert],[role=log],[aria-live]:not([aria-live=off]),output';
  const CONTROL = 'button,[role=button],input[type=submit],input[type=button],input[type=image],a[href],[role=menuitem],[role=switch],[role=checkbox],[role=option],[role=tab],label,summary';

  const k = (window.__kit = {
    lastAt: -1,
    target: null,
    busyAt: -1,
    busyHow: '',
    suppress: false,
    files: 0,
    successBefore: 0,
    imgsBefore: 0,
    live: new WeakMap(),
    fields: [],
    observer: null,
  });

  const now = () => performance.now();
  const successCount = () => {
    const text = document.body ? document.body.innerText : '';
    return (text.match(SUCCESS) || []).length;
  };
  const successSample = () => {
    const text = document.body ? document.body.innerText : '';
    return (text.match(SUCCESS) || []).slice(0, 3).join(', ');
  };

  const isDisabled = (el) =>
    !!el && (el.disabled === true || el.getAttribute('aria-disabled') === 'true');

  function busyNow() {
    // 1) the control (or its form) is disabled and something says "busy"
    const t = k.target;
    if (t && t.isConnected) {
      const form = t.form || t.closest('form');
      const disabled = isDisabled(t) || (form && t !== form && isDisabled(form.querySelector('[type=submit]')));
      const busy = t.closest('[aria-busy=true]') || t.getAttribute('aria-busy') === 'true' ||
        (form && form.getAttribute('aria-busy') === 'true') || document.querySelector('[aria-busy=true]');
      if (disabled && busy) return 'disabled + aria-busy';
    }
    // 2) a live region got new text
    for (const el of document.querySelectorAll(LIVE)) {
      const text = el.textContent.trim();
      if (text && k.live.get(el) !== text) return `${el.getAttribute('role') || 'aria-live'}: "${text.slice(0, 60)}"`;
    }
    return '';
  }

  function watchBusy() {
    if (k.observer) k.observer.disconnect();
    const check = () => {
      if (k.busyAt >= 0) return;
      const how = busyNow();
      if (how) { k.busyAt = now(); k.busyHow = how; }
    };
    k.observer = new MutationObserver(check);
    k.observer.observe(document.documentElement, { attributes: true, childList: true, subtree: true, characterData: true });
    const started = now();
    const tick = () => { check(); if (k.busyAt < 0 && now() - started < 3000) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
    setTimeout(() => k.observer && k.observer.disconnect(), 5000);
  }

  function snapshotFields() {
    const out = [];
    for (const el of document.querySelectorAll('input,select,textarea')) {
      const type = (el.type || '').toLowerCase();
      if (['hidden', 'password', 'file', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
      const value = type === 'checkbox' || type === 'radio' ? String(el.checked) : el.value;
      if (value === '' || value === 'false') continue; // nothing the person typed or chose
      out.push({ el, value, name: el.name || el.id || el.getAttribute('aria-label') || type || el.tagName.toLowerCase() });
    }
    return out;
  }

  function activate(raw) {
    if (k.suppress || !raw || !raw.closest) return;
    const el = raw.closest(CONTROL) || raw;
    if (k.lastAt >= 0 && now() - k.lastAt < 100 && (k.target === el || (k.target && k.target.contains && k.target.contains(el)) || (el.contains && el.contains(k.target)))) return;
    if (k.lastAt >= 0 && now() - k.lastAt < 100 && el.tagName === 'FORM') return; // submit right after the click
    if (k.target && k.target.removeAttribute) k.target.removeAttribute('data-kit-target');
    k.target = el;
    if (el.setAttribute) el.setAttribute('data-kit-target', '');
    k.lastAt = now();
    k.busyAt = -1;
    k.busyHow = '';
    k.live = new WeakMap();
    for (const region of document.querySelectorAll(LIVE)) k.live.set(region, region.textContent.trim());
    k.successBefore = successCount();
    k.imgsBefore = document.images.length;
    k.fields = snapshotFields();
    watchBusy();
    if (typeof window.__kitAction === 'function') window.__kitAction(); // new action for the read checks
  }

  document.addEventListener('pointerdown', (e) => activate(e.target), true);
  document.addEventListener('click', (e) => activate(e.target), true);
  document.addEventListener('submit', (e) => activate(e.submitter || e.target), true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') activate(e.target); }, true);
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (t && t.type === 'file') { activate(t); k.files = t.files ? t.files.length : 0; }
  }, true);
  // A typed or chosen value starts a new action for the read checks only
  // (a filter A, then B, then A again is three actions).
  const newAction = () => { if (!k.suppress && typeof window.__kitAction === 'function') window.__kitAction(); };
  document.addEventListener('input', newAction, true);
  document.addEventListener('change', newAction, true);
  document.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files.length) { activate(e.target); k.files = e.dataTransfer.files.length; }
  }, true);

  k.state = () => ({
    sinceAction: k.lastAt < 0 ? -1 : now() - k.lastAt,
    busyAfter: k.busyAt < 0 ? -1 : Math.max(0, k.busyAt - k.lastAt),
    busyHow: k.busyHow,
    newSuccess: successCount() - k.successBefore,
    successSample: successSample(),
    files: k.files,
    targetTag: k.target ? (k.target.tagName || '').toLowerCase() : '',
  });

  k.progress = () => {
    const bars = [...document.querySelectorAll('[role=progressbar],progress')].filter((b) => {
      if (b.tagName === 'PROGRESS') return b.hasAttribute('value');
      return b.getAttribute('aria-valuenow') !== null && !Number.isNaN(Number(b.getAttribute('aria-valuenow')));
    });
    const spinners = document.querySelectorAll('[role=progressbar]:not([aria-valuenow])').length;
    return { withValue: bars.length, spinners };
  };

  k.confirmation = () => {
    const text = document.body ? document.body.innerText : '';
    const size = /\b\d+([.,]\d+)?\s?(bytes|B|KB|kB|MB|GB)\b/.test(text);
    const type = /\.(png|jpe?g|gif|webp|avif|heic|pdf|mp4|mov|webm|mp3|wav|docx?|xlsx?|csv|zip)\b|\b(PNG|JPE?G|GIF|WEBP|PDF|MP4|MOV|image\/|video\/|audio\/)/i.test(text);
    const newImage = document.images.length > k.imgsBefore ||
      [...document.images].some((i) => /^(blob|data):/.test(i.currentSrc || i.src));
    return { size, type, newImage };
  };

  // Something says "loading": aria-busy anywhere, or a visible status or
  // progress bar (a skeleton should carry aria-busy on its region).
  // Loading signals: the last time something NEW said "loading" (aria-busy
  // turned on, a status/live region got new text, a progress bar appeared).
  // A status that was already on the page does not count.
  k.loadAt = -1;
  k.loadHow = '';
  const LOAD_REGION = '[role=status],[aria-live]:not([aria-live=off])';
  const signal = (how) => { k.loadAt = now(); k.loadHow = how; };
  const onMutations = (list) => {
    for (const m of list) {
      const t = m.target.nodeType === 1 ? m.target : m.target.parentElement;
      if (!t) continue;
      if (m.type === 'attributes' && m.attributeName === 'aria-busy' && t.getAttribute('aria-busy') === 'true') { signal('aria-busy'); return; }
      // While the HTML is still being parsed, new nodes are the page's own
      // markup (a status that is always there), not a loading signal.
      if (document.readyState === 'loading') continue;
      if (m.type === 'childList') {
        for (const n of m.addedNodes) {
          if (n.nodeType !== 1) continue;
          if (n.matches('[aria-busy=true],[role=progressbar],progress') || n.querySelector('[aria-busy=true],[role=progressbar],progress')) { signal('added: busy region or progress bar'); return; }
        }
      }
      const region = t.closest && t.closest(LOAD_REGION);
      if (region && region.textContent.trim()) { signal(`status: "${region.textContent.trim().slice(0, 40)}"`); return; }
    }
  };
  // Observing the document itself works before <html> exists, so the first
  // render's signals are seen too.
  new MutationObserver(onMutations).observe(document, {
    attributes: true, attributeFilter: ['aria-busy'], childList: true, subtree: true, characterData: true,
  });

  k.loadingSince = (t) => {
    if (k.loadAt >= t - 500) return k.loadHow;
    // Busy region already in the first HTML (server-rendered skeleton) while the page is still loading.
    if (t < 3000 && document.querySelector('[aria-busy=true]')) return 'aria-busy from the first HTML';
    return '';
  };

  k.lostFields = () => k.fields
    .filter((f) => {
      if (!f.el.isConnected) return true;
      const type = (f.el.type || '').toLowerCase();
      const value = type === 'checkbox' || type === 'radio' ? String(f.el.checked) : f.el.value;
      return value !== f.value;
    })
    .map((f) => f.name);

  k.retapPoint = () => {
    const t = k.target;
    if (!t || !t.isConnected || !t.getBoundingClientRect || t.type === 'file') return null;
    const r = t.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return null;
    const tag = t.tagName;
    const typing = (tag === 'INPUT' && !['submit', 'button', 'image', 'checkbox', 'radio'].includes(t.type)) || tag === 'TEXTAREA';
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, key: typing };
  };

  // Drag a file over the drop zone (the file input's area) and see if the
  // zone answers: class, attributes, text or border/outline/shadow/colour.
  k.dropzone = async () => {
    const input = document.querySelector('[data-dropzone] input[type=file], input[type=file]');
    const marked = document.querySelector('[data-dropzone]');
    let zone = marked;
    if (!zone && input) {
      zone = input;
      for (let i = 0; i < 6 && zone; i++) {
        const r = zone.getBoundingClientRect();
        if (r.width * r.height >= 1500) break;
        zone = zone.parentElement;
      }
    }
    if (!zone) return { found: false, changed: false };
    const sig = () => {
      const cs = getComputedStyle(zone);
      const attrs = [...zone.attributes].map((a) => `${a.name}=${a.value}`).join('|');
      return [attrs, zone.innerText, cs.borderColor, cs.borderStyle, cs.outlineStyle, cs.outlineColor, cs.boxShadow, cs.backgroundColor].join('§');
    };
    const before = sig();
    const dt = new DataTransfer();
    try { dt.items.add(new File(['kit'], 'kit-check.png', { type: 'image/png' })); } catch { /* old engines */ }
    const fire = (type) => zone.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt }));
    fire('dragenter');
    fire('dragover');
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 120))));
    const changed = sig() !== before;
    fire('dragleave');
    return { found: true, changed };
  };
}
