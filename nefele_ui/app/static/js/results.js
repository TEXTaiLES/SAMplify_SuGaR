(() => {
  'use strict';

  const POLL_INTERVAL_MS = 5000;

  const statusPill = document.getElementById('statusPill');
  const emptyCard = document.getElementById('emptyCard');
  const filesCard = document.getElementById('filesCard');
  const datasetName = filesCard ? filesCard.dataset.dataset || '' : '';
  const filesList = document.getElementById('filesList');
  const filesCount = document.getElementById('filesCount');
  const stageEls = Array.from(document.querySelectorAll('.stage'));
  const stageMessage = document.getElementById('stageMessage');
  const cancelBtn = document.getElementById('cancelBtn');

  let pollHandle = null;
  // Once we PATCH cancel ourselves we know the next status read should land on
  // 'cancelled'; keep the button hidden in the meantime so the user can't
  // double-click.
  let cancelRequested = false;
  // setInterval fires every POLL_INTERVAL_MS regardless of whether the
  // previous tick()'s fetches have resolved yet. On a slow/hiccuping network
  // two ticks can be in flight at once, and there's no guarantee the older
  // (now-stale) one resolves first — it can land *after* a newer, more
  // correct response and silently overwrite the UI with outdated state (this
  // is what caused "still running" to keep showing next to the finished
  // files). Each tick stamps its own id and only applies its result if no
  // newer tick has started meanwhile.
  let tickSeq = 0;

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  function formatTime(unixSec) {
    const d = new Date(unixSec * 1000);
    return d.toLocaleString();
  }

  function setStatus(label, cls) {
    statusPill.textContent = label;
    statusPill.classList.remove('ready', 'waiting', 'error');
    if (cls) statusPill.classList.add(cls);
  }

  function updateCancelButton(pipelineStatus) {
    if (!cancelBtn) return;
    const terminal = pipelineStatus === 'done'
                  || pipelineStatus === 'error'
                  || pipelineStatus === 'cancelled';
    cancelBtn.hidden = terminal || cancelRequested;
  }

  async function onCancelClick() {
    if (!cancelBtn || cancelRequested) return;
    if (!window.confirm('Cancel the running pipeline for this dataset?')) return;
    cancelRequested = true;
    cancelBtn.disabled = true;
    cancelBtn.hidden = true;
    try {
      const r = await fetch('/results/cancel', {
        method: 'POST',
        cache: 'no-store',
      });
      const d = await r.json().catch(() => null);
      if (!r.ok || !d || !d.ok) {
        const msg = (d && d.error) || `HTTP ${r.status}`;
        setStatus(msg, 'error');
        cancelRequested = false;
        cancelBtn.disabled = false;
        return;
      }
      setStatus('Cancelling…', 'waiting');
      // Trigger an immediate refresh so the user sees the new state without
      // waiting for the next poll tick.
      tick();
    } catch (_) {
      setStatus('Network error', 'error');
      cancelRequested = false;
      cancelBtn.disabled = false;
    }
  }

  if (cancelBtn) cancelBtn.addEventListener('click', onCancelClick);

  // Render exactly one card at a time. Errors collapse both so we don't
  // leave a contradictory "pipeline still running" + "0 files ready" mix.
  function setState(name) {
    emptyCard.hidden = name !== 'empty';
    filesCard.hidden = name !== 'files';
  }

  // Update the SAM2 / COLMAP / SuGaR pills based on the pipeline status.
  function applyPipelineStatus(s) {
    if (!s) return;
    const current = typeof s.current === 'number' ? s.current : -1;
    const isError = s.status === 'error';
    const isDone = s.status === 'done';

    stageEls.forEach((el, idx) => {
      el.classList.remove('pending', 'running', 'done', 'error');
      if (isError && idx === current) {
        el.classList.add('error');
      } else if (isDone || idx < current) {
        el.classList.add('done');
      } else if (idx === current && s.status === 'running') {
        el.classList.add('running');
      } else {
        el.classList.add('pending');
      }
    });

    stageMessage.textContent = s.error || s.message || '';
  }

  function renderFiles(files) {
    filesList.innerHTML = '';
    for (const f of files) {
      const li = document.createElement('li');
      li.innerHTML = `
        <span class="kind-pill ${f.kind}"></span>
        <span class="file-name"></span>
        <span class="file-meta">
          <span class="size"></span>
          <span class="dot-sep">·</span>
          <span class="modified"></span>
        </span>
        <a class="results-download" download>Download</a>`;
      li.querySelector('.kind-pill').textContent = f.kind;
      li.querySelector('.file-name').textContent = f.name;
      li.querySelector('.size').textContent = f.size > 0 ? formatBytes(f.size) : '';
      const modEl = li.querySelector('.modified');
      modEl.textContent = f.modified > 0 ? formatTime(f.modified) : '';
      if (!f.size && !f.modified) li.querySelector('.dot-sep').hidden = true;
      const a = li.querySelector('.results-download');
      a.href = `/results/file/${encodeURI(f.relative)}`;
      filesList.appendChild(li);
    }
    filesCount.textContent = files.length;
  }

  async function fetchPipelineStatus() {
    try {
      const r = await fetch('/pipeline/status', { cache: 'no-store' });
      if (!r.ok) return null;
      const d = await r.json();
      return d.ok ? d : null;
    } catch (_) {
      return null;
    }
  }

  async function tick() {
    const myTick = ++tickSeq;
    try {
      const [filesResp, pipeline] = await Promise.all([
        fetch('/results/files', { cache: 'no-store' }),
        fetchPipelineStatus(),
      ]);
      const data = await filesResp.json().catch(() => null);

      // Both fetches are done — from here on nothing else is awaited, so a
      // single staleness check right here is enough to discard this whole
      // tick if a later one has already started (see tickSeq above).
      if (myTick !== tickSeq) return;

      applyPipelineStatus(pipeline);

      if (!filesResp.ok || !data || !data.ok) {
        const msg = (data && data.error) || `HTTP ${filesResp.status}`;
        setStatus(msg, 'error');
        setState('none');
        updateCancelButton(pipeline && pipeline.status);
        return;
      }
      const pStatus = pipeline && pipeline.status;
      if (pStatus === 'cancelled') {
        setStatus('Cancelled', 'error');
        setState('none');
        if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      } else if (data.ready) {
        // Show the files even if the job's own status later flipped to
        // 'error' (e.g. a successful upload followed by an unrelated
        // failure) — the deliverable exists, that's what matters here.
        setStatus('Ready', 'ready');
        renderFiles(data.files);
        setState('files');
        // The files existing is the ground truth that the pipeline actually
        // completed — show all stages as done even if the job's own status
        // (e.g. a later unrelated error) would otherwise leave them pending.
        stageEls.forEach((el) => {
          el.classList.remove('pending', 'running', 'error');
          el.classList.add('done');
        });
        if (pStatus === 'error') {
          // A working result already exists — the stale error text (from
          // whatever the job did *after* producing it) is more alarming
          // than useful here, so don't show it next to a successful result.
          stageMessage.textContent = '';
        }
        if ((pStatus === 'done' || pStatus === 'error') && pollHandle) {
          clearInterval(pollHandle); pollHandle = null;
        }
      } else if (pStatus === 'error') {
        setStatus('Error', 'error');
        setState('none');
        if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      } else {
        setStatus('Waiting', 'waiting');
        setState('empty');
      }
      updateCancelButton(pStatus);
    } catch (err) {
      if (myTick !== tickSeq) return;
      setStatus('Network error', 'error');
      setState('none');
    }
  }

  tick();
  pollHandle = setInterval(tick, POLL_INTERVAL_MS);
})();
