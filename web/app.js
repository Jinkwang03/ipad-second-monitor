'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const stage = $('stage');
  const canvas = $('screen');
  const ctx = canvas.getContext('2d', { alpha: false, desynchronized: true });
  const cursorEl = $('cursor');
  const statusEl = $('status');
  const keyForm = $('keyform');
  const panel = $('panel');
  const handle = $('handle');
  const statsEl = $('stats');
  const noticeEl = $('notice');

  const store = {
    get(k) { try { return localStorage.getItem('ipd.' + k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem('ipd.' + k, v); } catch (e) { /* storage blocked */ } },
  };

  const params = new URLSearchParams(location.search);
  let key = params.get('key') || store.get('key') || '';
  const opts = {
    touchAsMouse: store.get('touchAsMouse') === '1',
    cmdAsCtrl: store.get('cmdAsCtrl') !== '0',
    showStats: store.get('showStats') === '1',
    autoFullscreen: store.get('autoFullscreen') !== '0',
  };

  let ws = null;
  let W = 0, H = 0;               // size of the PC display, in its pixels
  let scale = 1, offX = 0, offY = 0;
  let sizeGen = 0;
  let display = null;             // latest 'size' message
  let drawChain = Promise.resolve();
  let streaming = false;
  let reconnectTimer = 0, reconnectDelay = 500, pingTimer = 0;
  const stats = { frames: 0, bytes: 0, since: performance.now(), rtt: 0, delay: null, text: '–' };

  // ---------------------------------------------------------------- status

  function setStatus(text, busy = true) {
    if (!text) { statusEl.hidden = true; return; }
    $('status-text').textContent = text;
    statusEl.classList.toggle('idle', !busy);
    statusEl.hidden = false;
  }

  function showKeyForm(msg) {
    setStatus(null);
    $('keyform-msg').textContent = msg || 'Enter the access key shown on your PC.';
    keyForm.hidden = false;
  }

  keyForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const value = $('key-input').value.trim();
    if (!value) return;
    key = value;
    $('key-input').blur();
    connect();
  });

  // ---------------------------------------------------------------- layout

  function layout() {
    if (!W || !H) return;
    const vw = window.innerWidth, vh = window.innerHeight;
    scale = Math.min(vw / W, vh / H);
    offX = (vw - W * scale) / 2;
    offY = (vh - H * scale) / 2;
    const s = canvas.style;
    s.width = W * scale + 'px';
    s.height = H * scale + 'px';
    s.left = offX + 'px';
    s.top = offY + 'px';
    scheduleCursor();
    updatePanel();
    updateFullscreenUi();
  }

  window.addEventListener('resize', () => { layout(); sendHi(); });

  // ------------------------------------------------------------ connection

  function send(obj, sock = ws) {
    if (sock && sock === ws && sock.readyState === 1) sock.send(JSON.stringify(obj));
  }

  function sendHi() {
    send({ t: 'hi', vw: innerWidth, vh: innerHeight, sw: screen.width, sh: screen.height, dpr: devicePixelRatio || 1 });
  }

  function connect() {
    clearTimeout(reconnectTimer);
    if (ws) { const old = ws; ws = null; try { old.close(); } catch (e) { /* ignore */ } }
    if (!key) { showKeyForm(); return; }
    keyForm.hidden = true;
    if (!streaming) setStatus('Connecting to your PC…');
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const sock = new WebSocket(`${proto}//${location.host}/ws?key=${encodeURIComponent(key)}`);
    sock.binaryType = 'arraybuffer';
    ws = sock;

    sock.onopen = () => {
      reconnectDelay = 500;
      sendHi();
      clearInterval(pingTimer);
      pingTimer = setInterval(() => send({ t: 'ping', ts: performance.now(), rtt: stats.rtt }, sock), 2000);
      keepAwake();
    };
    sock.onmessage = (ev) => {
      if (sock !== ws) return;
      if (typeof ev.data === 'string') onText(JSON.parse(ev.data));
      else onFrame(ev.data, sock);
    };
    sock.onclose = (ev) => {
      if (sock !== ws) return;
      ws = null;
      clearInterval(pingTimer);
      resetInput();
      streaming = false;
      if (ev.code === 4001) {
        key = '';
        store.set('key', '');
        showKeyForm('That key was not accepted. Check the key shown on your PC.');
        return;
      }
      setStatus('Lost connection to the PC.\nReconnecting…');
      reconnectTimer = setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 5000);
    };
  }

  function onText(m) {
    switch (m.t) {
      case 'hello':
        if (m.saveDir) saveDir = m.saveDir;
        store.set('key', key);
        if (params.get('key') !== key) {  // keep "Add to Home Screen" pointing at a working key
          params.set('key', key);
          history.replaceState(null, '', '?' + params.toString());
        }
        break;
      case 'size':
        display = m;
        W = m.w; H = m.h;
        sizeGen++;
        canvas.width = W;
        canvas.height = H;
        ctx.fillStyle = '#000';
        ctx.fillRect(0, 0, W, H);
        layout();
        if (m.mirror) showNotice(m.hint); else noticeEl.hidden = true;
        break;
      case 'cs':
        shapes.set(m.id, { url: `url("data:image/png;base64,${m.png}")`, w: m.w, h: m.h,
                           hx: m.hx, hy: m.hy, k: m.k || 1, inv: !!m.inv });
        if (cursor.s === m.id) { appliedShape = 0; scheduleCursor(); }
        break;
      case 'c':
        cursor.x = m.x; cursor.y = m.y; cursor.v = !!m.v;
        if (m.s) cursor.s = m.s;
        scheduleCursor();
        break;
      case 'pong':
        stats.rtt = performance.now() - m.ts;
        break;
      case 'lat':                            // screen-to-iPad delay measured by the PC
        stats.delay = m.ms;
        break;
      case 'offer':                          // files dropped on the PC's "Drop files here" box
        if (m.files && m.files.length) showInbox(null, m.files, 'Sent from the PC');
        break;
    }
  }

  let noticeTimer = 0, noticeShownFor = null;
  function showNotice(hint) {
    if (noticeShownFor === hint) return;   // once per reason, not on every reconnect
    noticeShownFor = hint;
    $('notice-text').textContent = "Windows has no second display yet, so this shows your PC's main screen.\n"
      + (hint || 'Install the virtual display driver on the PC (see README) to extend onto the iPad.');
    noticeEl.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { noticeEl.hidden = true; }, 20000);
  }
  $('notice-close').addEventListener('click', () => { noticeEl.hidden = true; });

  // ---------------------------------------------------------------- frames

  const decode = typeof createImageBitmap === 'function'
    ? (blob) => createImageBitmap(blob)
    : (blob) => new Promise((resolve, reject) => {
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
        img.onerror = (e) => { URL.revokeObjectURL(url); reject(e); };
        img.src = url;
      });

  // Binary frame: u8 type, u8 flags, u16 count, u32 id, then per rectangle
  // u16 x, y, w, h, u32 length and that many bytes of JPEG (little endian).
  function onFrame(buf, sock) {
    const dv = new DataView(buf);
    if (dv.getUint8(0) !== 1) return;
    const count = dv.getUint16(2, true);
    const id = dv.getUint32(4, true);
    const rects = [];
    let off = 8;
    for (let i = 0; i < count; i++) {
      const x = dv.getUint16(off, true), y = dv.getUint16(off + 2, true);
      const w = dv.getUint16(off + 4, true), h = dv.getUint16(off + 6, true);
      const len = dv.getUint32(off + 8, true);
      off += 12;
      rects.push({ x, y, w, h, blob: new Blob([new Uint8Array(buf, off, len)], { type: 'image/jpeg' }) });
      off += len;
    }
    stats.bytes += buf.byteLength;
    const gen = sizeGen;
    const decoded = Promise.all(rects.map((r) => decode(r.blob)));  // decode in parallel...
    drawChain = drawChain                                           // ...but draw strictly in order
      .then(() => decoded)
      .then((images) => {
        // Fast-moving areas arrive at half size; drawing into the full rectangle scales them back.
        if (gen === sizeGen) images.forEach((img, i) => ctx.drawImage(img, rects[i].x, rects[i].y, rects[i].w, rects[i].h));
        images.forEach((img) => img.close && img.close());
        stats.frames++;
        if (!streaming && gen === sizeGen) { streaming = true; setStatus(null); }
      })
      .catch((err) => { console.warn('frame decode failed', err); send({ t: 'key' }, sock); })
      .then(() => send({ t: 'ack', id }, sock));
  }

  // ---------------------------------------------------------------- cursor

  const shapes = new Map();
  const cursor = { x: 0, y: 0, v: false, s: 0 };
  let cursorRaf = 0, appliedShape = 0, appliedK = 0;

  function scheduleCursor() {
    if (!cursorRaf) cursorRaf = requestAnimationFrame(drawCursor);
  }

  function drawCursor() {
    cursorRaf = 0;
    const sh = shapes.get(cursor.s);
    if (!cursor.v || !sh || !W) { cursorEl.style.display = 'none'; return; }
    const k = scale * sh.k;
    if (appliedShape !== cursor.s || appliedK !== k) {
      appliedShape = cursor.s;
      appliedK = k;
      cursorEl.style.backgroundImage = sh.url;
      cursorEl.style.width = sh.w * k + 'px';
      cursorEl.style.height = sh.h * k + 'px';
      cursorEl.classList.toggle('inv', sh.inv);
    }
    cursorEl.style.transform =
      `translate(${offX + cursor.x * scale - sh.hx * k}px, ${offY + cursor.y * scale - sh.hy * k}px)`;
    cursorEl.style.display = 'block';
  }

  // ----------------------------------------------------------------- input

  const PEN_GRACE_MS = 800;   // touches this soon after pen activity are treated as a resting palm
  let penUntil = 0;
  const touches = new Set();  // touch pointer ids currently forwarded
  let mouseTouch = null;      // "touch acts as mouse": the finger driving the mouse

  function toRemote(e) {
    const x = (e.clientX - offX) / (W * scale);
    const y = (e.clientY - offY) / (H * scale);
    return [Math.min(Math.max(x, 0), 1), Math.min(Math.max(y, 0), 1)];
  }

  function forward(e, kind) {
    if (!W || !ws) return;
    let pt = e.pointerType === 'pen' ? 'p' : e.pointerType === 'touch' ? 't' : 'm';
    let buttons = e.buttons;
    const now = performance.now();
    if (pt === 'p') penUntil = now + PEN_GRACE_MS;
    if (pt === 't') {
      if (kind === 'd') {
        if (now < penUntil) return;
        touches.add(e.pointerId);
      } else if (!touches.has(e.pointerId)) {
        return;
      }
      const ending = kind === 'u' || kind === 'c';
      if (ending) touches.delete(e.pointerId);
      if (opts.touchAsMouse) {
        if (kind === 'd' && mouseTouch === null) mouseTouch = e.pointerId;
        if (e.pointerId !== mouseTouch) return;
        if (ending) mouseTouch = null;
        pt = 'm';
        buttons = ending ? 0 : 1;
      }
    }
    if (kind === 'l' && pt !== 'p') return;
    const coalesced = kind === 'm' && e.getCoalescedEvents ? e.getCoalescedEvents() : null;
    for (const ev of (coalesced && coalesced.length ? coalesced : [e])) {
      const [x, y] = toRemote(ev);
      const msg = { t: 'p', k: kind, pt, id: e.pointerId, x: +x.toFixed(5), y: +y.toFixed(5), b: buttons };
      if (pt === 'p') {
        msg.p = +(ev.pressure || 0).toFixed(3);
        msg.tx = ev.tiltX | 0;
        msg.ty = ev.tiltY | 0;
      }
      send(msg);
    }
  }

  function resetInput() {
    touches.clear();
    mouseTouch = null;
    heldKeys.clear();
  }

  let fsTap = null;   // the tap being used to go full screen; it is not sent to Windows

  stage.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    if (W && wantsFullscreen()) {
      fsTap = e.pointerId;
      if (e.pointerType === 'mouse') enterFullscreen();   // a mouse press counts as a user gesture
      return;
    }
    try { stage.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    forward(e, 'd');
  });
  stage.addEventListener('pointermove', (e) => {
    e.preventDefault();
    if (e.pointerId !== fsTap) forward(e, 'm');
  });
  stage.addEventListener('pointerup', (e) => {
    e.preventDefault();
    if (e.pointerId === fsTap) {
      fsTap = null;
      if (e.pointerType !== 'mouse') enterFullscreen();   // for touch and pen only the lift counts
      return;
    }
    forward(e, 'u');
  });
  stage.addEventListener('pointercancel', (e) => {
    if (e.pointerId === fsTap) { fsTap = null; return; }
    forward(e, 'c');
  });
  stage.addEventListener('pointerleave', (e) => forward(e, 'l'));

  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (!W) return;
    const [x, y] = toRemote(e);
    send({ t: 'w', x, y, dx: e.deltaX, dy: e.deltaY, m: e.deltaMode });
  }, { passive: false });

  // Keep iPadOS from scrolling, zooming, selecting or showing callouts.
  const isUi = (t) => t instanceof Element && !!t.closest('#panel, #keyform, #handle, #notice, #transfer, #inbox');
  for (const type of ['touchstart', 'touchmove', 'touchend', 'gesturestart', 'gesturechange',
                      'gestureend', 'contextmenu', 'selectstart', 'dblclick']) {
    document.addEventListener(type, (e) => { if (!isUi(e.target)) e.preventDefault(); }, { passive: false });
  }

  // Hardware keyboard: forward physical key codes; Windows applies its own layout/IME.
  const heldKeys = new Set();
  const MODIFIERS = /^(Shift|Control|Alt|Meta)(Left|Right)$/;
  const isTyping = (t) => t instanceof Element && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA');
  const mapCode = (code) => (opts.cmdAsCtrl && code.startsWith('Meta')) ? code.replace('Meta', 'Control') : code;

  function releaseKeys(filter = () => true) {
    for (const code of [...heldKeys]) {
      if (!filter(code)) continue;
      heldKeys.delete(code);
      send({ t: 'k', c: code, d: 0 });
    }
  }

  document.addEventListener('keydown', (e) => {
    if (isTyping(e.target) || !ws || !W || !e.code) return;
    e.preventDefault();
    const code = mapCode(e.code);
    heldKeys.add(code);
    send({ t: 'k', c: code, d: 1 });
  });

  document.addEventListener('keyup', (e) => {
    if (isTyping(e.target) || !e.code) return;
    const code = mapCode(e.code);
    if (heldKeys.has(code)) {
      e.preventDefault();
      heldKeys.delete(code);
      send({ t: 'k', c: code, d: 0 });
    }
    // Safari often skips keyup for keys pressed while ⌘ was held, so let them go with ⌘.
    if (e.code.startsWith('Meta')) releaseKeys((c) => !MODIFIERS.test(c));
  });

  window.addEventListener('blur', () => releaseKeys());
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { releaseKeys(); return; }
    keepAwake();
    if (!ws) { reconnectDelay = 500; connect(); }
  });

  async function keepAwake() {
    try { if ('wakeLock' in navigator) await navigator.wakeLock.request('screen'); } catch (e) { /* not allowed */ }
  }

  // ------------------------------------------------------------------ menu

  let fadeTimer = 0;
  function wakeHandle() {
    handle.classList.remove('faded');
    clearTimeout(fadeTimer);
    fadeTimer = setTimeout(() => handle.classList.add('faded'), 4000);
  }

  handle.addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    updatePanel();
    wakeHandle();
  });
  $('panel-close').addEventListener('click', () => { panel.hidden = true; });

  function nativeSize() {
    const dpr = devicePixelRatio || 1;
    const long = Math.max(screen.width, screen.height), short = Math.min(screen.width, screen.height);
    const landscape = innerWidth >= innerHeight;
    return [Math.round((landscape ? long : short) * dpr), Math.round((landscape ? short : long) * dpr), dpr];
  }

  function updatePanel() {
    if (panel.hidden) return;
    const [nw, nh, dpr] = nativeSize();
    $('info-ipad').textContent = `${nw} × ${nh}`;
    $('info-display').textContent = display ? `${display.w} × ${display.h} at ${display.scale}%` : '–';
    $('info-stats').textContent = ws ? stats.text : 'disconnected';
    const tip = $('info-tip');
    if (display && display.mirror) {
      tip.textContent = 'No second display on the PC yet, so this mirrors the main screen. '
        + (display.hint || 'Install the virtual display driver (see README) to extend instead.');
    } else if (display && (display.w !== nw || display.h !== nh)) {
      tip.textContent = `For the sharpest picture, set this display to ${nw} × ${nh} `
        + `with ${Math.round(dpr * 100)}% scale in Windows display settings.`;
    } else {
      tip.textContent = '';
    }
    tip.hidden = !tip.textContent;
  }

  function bindToggle(id, name, onChange) {
    const el = $(id);
    el.checked = opts[name];
    el.addEventListener('change', () => {
      opts[name] = el.checked;
      store.set(name, el.checked ? '1' : '0');
      if (onChange) onChange();
    });
  }

  bindToggle('opt-mouse', 'touchAsMouse');
  bindToggle('opt-cmd', 'cmdAsCtrl', () => releaseKeys());
  bindToggle('opt-stats', 'showStats', () => { statsEl.hidden = !opts.showStats; });
  bindToggle('opt-fs', 'autoFullscreen', () => updateFullscreenUi());
  statsEl.hidden = !opts.showStats;

  // ----------------------------------------------------------- full screen
  // iPadOS lets a page hide Safari's bars and the status bar with the Fullscreen API,
  // but only in response to a tap. So while we're not full screen, the first tap goes
  // full screen instead of being sent to Windows.
  const root = document.documentElement;
  const fsHint = $('fs-hint');
  const fsButton = $('btn-fullscreen');
  let fsRefused = false;    // the browser said no (e.g. inside some Home Screen apps): stop asking
  let fsDeclined = false;   // the user left full screen from our menu: don't pull them back in
  let fsCheckTimer = 0;

  const fsSupported = () => !!((document.fullscreenEnabled || document.webkitFullscreenEnabled)
    && (root.requestFullscreen || root.webkitRequestFullscreen));
  const fsActive = () => !!(document.fullscreenElement || document.webkitFullscreenElement);
  const wantsFullscreen = () => opts.autoFullscreen && !fsRefused && !fsDeclined && fsSupported() && !fsActive();

  function enterFullscreen() {
    const request = root.requestFullscreen || root.webkitRequestFullscreen;
    try {
      const p = request.call(root);
      if (p && p.catch) p.catch(() => { fsRefused = true; updateFullscreenUi(); });
    } catch (err) {
      fsRefused = true;
    }
    // The prefixed API reports nothing on failure; if we aren't full screen soon, give up.
    clearTimeout(fsCheckTimer);
    fsCheckTimer = setTimeout(() => { if (!fsActive()) { fsRefused = true; updateFullscreenUi(); } }, 1500);
    updateFullscreenUi();
  }

  function updateFullscreenUi() {
    fsHint.hidden = !(W && wantsFullscreen());
    fsButton.hidden = !fsSupported();
    fsButton.textContent = fsActive() ? 'Exit full screen' : 'Full screen';
  }

  for (const type of ['fullscreenchange', 'webkitfullscreenchange']) {
    document.addEventListener(type, () => { updateFullscreenUi(); layout(); });
  }
  for (const type of ['fullscreenerror', 'webkitfullscreenerror']) {
    document.addEventListener(type, () => { fsRefused = true; updateFullscreenUi(); });
  }
  fsButton.addEventListener('click', () => {
    if (fsActive()) {
      fsDeclined = true;
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    } else {
      fsRefused = fsDeclined = false;
      enterFullscreen();
    }
  });
  updateFullscreenUi();
  $('btn-reconnect').addEventListener('click', () => {
    panel.hidden = true;
    reconnectDelay = 500;
    connect();
  });

  // -------------------------------------------------------- photos & files
  // iPad -> PC: pick (or drag in) photos and files; they're uploaded to the PC's save folder.
  // PC -> iPad: files copied on the PC (Ctrl+C in File Explorer) can be saved on the iPad.
  const fileInput = $('file-input');
  const transfer = $('transfer');
  const inbox = $('inbox');
  const dropEl = $('drop');
  let saveDir = 'Downloads\\iPad Display';
  let lastSaved = [];     // names the PC gave the files we just sent
  let sending = false;

  const apiUrl = (path, extra = {}) => `${path}?${new URLSearchParams({ key, ...extra })}`;
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const fmtSize = (n) => n >= 1e9 ? (n / 1e9).toFixed(1) + ' GB'
    : n >= 1e6 ? (n / 1e6).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1e3)) + ' KB';

  function showTransfer(title, text, progress = null, actions = false) {
    $('transfer-title').textContent = title;
    $('transfer-text').textContent = text;
    $('transfer-bar-wrap').hidden = progress === null;
    $('transfer-bar').style.width = `${Math.round((progress || 0) * 100)}%`;
    $('transfer-actions').hidden = !actions;
    transfer.hidden = false;
  }

  function uploadOne(file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', apiUrl('/upload', { name: file.name || 'photo.jpg', mtime: String(file.lastModified || '') }));
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded); };
      xhr.onload = () => {
        if (xhr.status === 200) resolve(JSON.parse(xhr.responseText));
        else reject(new Error(xhr.status === 403 ? 'The access key was not accepted.' : `The PC answered ${xhr.status}.`));
      };
      xhr.onerror = () => reject(new Error('The connection to the PC was lost.'));
      xhr.send(file);
    });
  }

  async function sendFiles(files) {
    if (sending || !files.length) return;
    sending = true;
    const total = files.reduce((sum, f) => sum + f.size, 0) || 1;
    let done = 0;
    const saved = [];
    try {
      for (const [i, file] of files.entries()) {
        const title = files.length > 1 ? `Sending ${i + 1} of ${files.length} to the PC` : 'Sending to the PC';
        showTransfer(title, file.name, done / total);
        const result = await uploadOne(file, (loaded) => showTransfer(title, file.name, (done + loaded) / total));
        done += file.size;
        saved.push(result.name);
      }
      lastSaved = saved;
      showTransfer(`Sent ${plural(saved.length, 'file')} to the PC`,
                   `Saved in ${saveDir} (${fmtSize(done)}).`, null, true);
    } catch (err) {
      lastSaved = saved;
      showTransfer('Sending stopped', `${err.message} ${saved.length} of ${files.length} files were saved.`,
                   null, saved.length > 0);
    } finally {
      sending = false;
    }
  }

  async function savedAction(action) {
    try {
      const r = await fetch(apiUrl('/saved'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, names: lastSaved }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || `The PC answered ${r.status}.`);
      $('transfer-text').textContent = action === 'copy'
        ? `Copied ${plural(data.count, 'file')}. On the PC, press Ctrl+V to paste into a folder or app.`
        : 'Opened File Explorer on the PC.';
    } catch (err) {
      $('transfer-text').textContent = `The PC couldn't do that: ${err.message}`;
    }
  }

  $('btn-send').addEventListener('click', () => { panel.hidden = true; fileInput.click(); });
  fileInput.addEventListener('change', () => {
    const files = [...fileInput.files];
    fileInput.value = '';
    sendFiles(files);
  });
  $('btn-show-pc').addEventListener('click', () => savedAction('show'));
  $('btn-copy-pc').addEventListener('click', () => savedAction('copy'));
  $('transfer-close').addEventListener('click', () => { transfer.hidden = true; });

  // Drag photos in from the Photos or Files app (Split View / Stage Manager).
  // Safari on iPad doesn't say what is being dragged until the drop, so accept every drag
  // that comes in and look at its contents only when it lands.
  let dropHideTimer = 0;
  function showDropZone(e) {
    e.preventDefault();                      // without this the page refuses the drop
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    dropEl.hidden = false;
    clearTimeout(dropHideTimer);             // dragover repeats while hovering; if it stops,
    dropHideTimer = setTimeout(() => { dropEl.hidden = true; }, 600);  // the drag has left
  }
  document.addEventListener('dragenter', showDropZone);
  document.addEventListener('dragover', showDropZone);
  document.addEventListener('dragend', () => { dropEl.hidden = true; });

  function droppedFiles(dt) {
    const files = [...(dt.files || [])];
    if (!files.length && dt.items) {         // some drags only expose files as items
      for (const item of dt.items) {
        const file = item.kind === 'file' ? item.getAsFile() : null;
        if (file) files.push(file);
      }
    }
    return files;
  }

  document.addEventListener('drop', (e) => {
    e.preventDefault();
    clearTimeout(dropHideTimer);
    dropEl.hidden = true;
    const files = e.dataTransfer ? droppedFiles(e.dataTransfer) : [];
    if (files.length) sendFiles(files);
    else showTransfer('Nothing to send', 'Only photos and files can be sent to the PC. Try dragging them from the Photos or Files app.');
  });

  function showInbox(message, files = [], title = 'Files copied on the PC') {
    $('inbox-title').textContent = title;
    const list = $('inbox-list');
    list.textContent = '';
    const photos = files.some((f) => f.image);
    $('inbox-msg').textContent = message || (photos
      ? 'Touch and hold a photo, then tap "Save to Photos". Download saves a file to the Files app (Downloads).'
      : 'Download saves a file to the Files app (Downloads).');
    for (const f of files) {
      const item = document.createElement('div');
      item.className = 'item';
      if (f.image) {
        const img = document.createElement('img');
        img.src = apiUrl(`/clipboard/${f.id}`);
        img.alt = f.name;
        item.append(img);
      }
      const meta = document.createElement('div');
      meta.className = 'meta';
      const name = document.createElement('span');
      name.textContent = f.name;
      const size = document.createElement('small');
      size.textContent = fmtSize(f.size);
      const link = document.createElement('a');
      link.className = 'button';
      link.href = apiUrl(`/clipboard/${f.id}`, { dl: '1' });
      link.setAttribute('download', f.name);
      link.textContent = 'Download';
      meta.append(name, size, link);
      item.append(meta);
      list.append(item);
    }
    inbox.hidden = false;
  }

  $('btn-receive').addEventListener('click', async () => {
    panel.hidden = true;
    showInbox('Checking what is copied on the PC…');
    try {
      const r = await fetch(apiUrl('/clipboard'));
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || `The PC answered ${r.status}.`);
      if (data.files.length) showInbox(null, data.files);
      else if (data.folders) showInbox('Only folders are copied on the PC. Open the folder, select the files inside and press Ctrl+C, then tap "Get from PC" again.');
      else showInbox('Nothing is copied on the PC. In File Explorer, select files and press Ctrl+C, then tap "Get from PC" again.');
    } catch (err) {
      showInbox(`Couldn't check the PC: ${err.message}`);
    }
  });
  $('inbox-close').addEventListener('click', () => { inbox.hidden = true; });

  function formatRate(bytesPerSec) {
    return bytesPerSec >= 1e6 ? (bytesPerSec / 1e6).toFixed(1) + ' MB/s' : Math.round(bytesPerSec / 1e3) + ' KB/s';
  }

  setInterval(() => {
    const now = performance.now(), dt = (now - stats.since) / 1000;
    const delay = stats.delay === null ? '' : ` · delay ${stats.delay} ms`;
    stats.text = `${Math.round(stats.frames / dt)} upd/s · ${formatRate(stats.bytes / dt)}${delay} · ping ${Math.round(stats.rtt)} ms`;
    stats.frames = 0;
    stats.bytes = 0;
    stats.since = now;
    statsEl.textContent = stats.text;
    updatePanel();
  }, 1000);

  wakeHandle();
  connect();
})();
