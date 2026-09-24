(() => {
  'use strict';

  // ── Local upload ──────────────────────────────────────────────────────────
  const form          = document.getElementById('setupForm');
  const nameInput     = document.getElementById('datasetName');
  const dropzone      = document.getElementById('dropzone');
  const fileInput     = document.getElementById('fileInput');
  const fileListEl    = document.getElementById('fileList');
  const submitBtn     = document.getElementById('submitBtn');
  const statusLine    = document.getElementById('statusLine');
  const loadingOverlay = document.getElementById('loadingOverlay');

  // Model (SuGaR / PGSR) is no longer picked at upload time — it's set per
  // dataset on the welcome page. Uploads default to 'sugar'; the user can
  // flip it after the dataset is on disk.
  const DEFAULT_MODEL = 'sugar';

  const staged = new DataTransfer();

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  function refreshList() {
    fileListEl.innerHTML = '';
    const arr = Array.from(staged.files);
    if (arr.length === 0) {
      fileListEl.hidden = true;
      statusLine.textContent = 'No files staged.';
      submitBtn.disabled = !nameInput.value.trim();
      return;
    }
    fileListEl.hidden = false;
    arr.forEach((file, idx) => {
      const li = document.createElement('li');
      li.innerHTML = `
        <span class="name"></span>
        <span class="size"></span>
        <button type="button" class="remove" aria-label="Remove">×</button>`;
      li.querySelector('.name').textContent = file.name;
      li.querySelector('.size').textContent = formatBytes(file.size);
      li.querySelector('.remove').addEventListener('click', () => {
        const next = new DataTransfer();
        Array.from(staged.files).forEach((f, i) => { if (i !== idx) next.items.add(f); });
        staged.items.clear();
        Array.from(next.files).forEach(f => staged.items.add(f));
        fileInput.files = staged.files;
        refreshList();
      });
      fileListEl.appendChild(li);
    });
    statusLine.textContent = `${arr.length} file${arr.length === 1 ? '' : 's'} staged.`;
    submitBtn.disabled = !nameInput.value.trim();
  }

  function addFiles(list) {
    for (const f of list) {
      const isImageMime = f.type && f.type.startsWith('image/');
      const isImageExt  = /\.(jpe?g|png|webp|bmp|tiff?|gif|heic|heif|avif)$/i.test(f.name);
      if (!isImageMime && !isImageExt) continue;
      staged.items.add(f);
    }
    fileInput.files = staged.files;
    refreshList();
  }

  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener('change', () => addFiles(fileInput.files));
  ['dragenter', 'dragover'].forEach(ev =>
    dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(ev =>
    dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove('drag'); }));
  dropzone.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });
  nameInput.addEventListener('input', refreshList);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = nameInput.value.trim();
    if (!name) return;
    const fd = new FormData();
    fd.append('name', name);
    fd.append('model', DEFAULT_MODEL);
    Array.from(staged.files).forEach(f => fd.append('images', f));
    submitBtn.disabled = true;
    loadingOverlay.classList.add('show');
    try {
      const r = await fetch('/setup', { method: 'POST', body: fd });
      const data = await r.json();
      if (!r.ok || !data.ok) {
        alert('Setup failed: ' + (data.error || r.status));
        submitBtn.disabled = false;
        return;
      }
      if (data.failed && data.failed.length) {
        alert(`${data.failed.length} file(s) skipped: ${data.failed.map(f => f.name).join(', ')}`);
      }
      window.location.href = '/';
    } catch (err) {
      alert('Setup failed: ' + err);
      submitBtn.disabled = false;
    } finally {
      loadingOverlay.classList.remove('show');
    }
  });

  refreshList();

  // ── Tab switching ─────────────────────────────────────────────────────────
  const tabs        = document.querySelectorAll('.src-tab');
  const panelLocal  = document.getElementById('panelLocal');
  const panelVideo  = document.getElementById('panelVideo');
  const panelHestia = document.getElementById('panelHestia');

  let hestiaLoaded = false;

  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      tabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const which = tab.dataset.tab;
      panelLocal.hidden  = which !== 'local';
      panelVideo.hidden  = which !== 'video';
      panelHestia.hidden = which !== 'hestia';
      if (which === 'hestia' && !hestiaLoaded) {
        hestiaLoaded = true;
        loadHestiaScans();
      }
    });
  });

  // ── Video upload ──────────────────────────────────────────────────────────
  const videoForm        = document.getElementById('videoForm');
  const videoNameInput   = document.getElementById('videoDatasetName');
  const videoDropzone    = document.getElementById('videoDropzone');
  const videoInput       = document.getElementById('videoInput');
  const videoFileName    = document.getElementById('videoFileName');
  const fpsSlider        = document.getElementById('fpsSlider');
  const fpsValue         = document.getElementById('fpsValue');
  const videoStatusLine  = document.getElementById('videoStatusLine');
  const videoSubmitBtn   = document.getElementById('videoSubmitBtn');

  function refreshVideoState() {
    const hasFile = videoInput.files && videoInput.files.length > 0;
    const hasName = videoNameInput.value.trim().length > 0;
    videoSubmitBtn.disabled = !(hasFile && hasName);
    if (hasFile) {
      const f = videoInput.files[0];
      videoFileName.hidden = false;
      videoFileName.textContent = `Selected: ${f.name} (${formatBytes(f.size)})`;
      videoStatusLine.textContent = 'Video ready to process.';
    } else {
      videoFileName.hidden = true;
      videoStatusLine.textContent = 'No video selected.';
    }
  }

  videoDropzone.addEventListener('click', () => videoInput.click());
  videoDropzone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); videoInput.click(); }
  });
  videoInput.addEventListener('change', refreshVideoState);
  videoNameInput.addEventListener('input', refreshVideoState);

  ['dragenter', 'dragover'].forEach(ev =>
    videoDropzone.addEventListener(ev, (e) => { e.preventDefault(); videoDropzone.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(ev =>
    videoDropzone.addEventListener(ev, (e) => { e.preventDefault(); videoDropzone.classList.remove('drag'); }));
  videoDropzone.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files.length) {
      videoInput.files = e.dataTransfer.files;
      refreshVideoState();
    }
  });

  fpsSlider.addEventListener('input', () => { fpsValue.textContent = fpsSlider.value; });

  const previewCard    = document.getElementById('videoPreviewCard');
  const previewStats   = document.getElementById('videoPreviewStats');
  const framesGrid     = document.getElementById('videoFramesGrid');
  const keepLine       = document.getElementById('videoKeepLine');
  const cancelBtn      = document.getElementById('videoCancelBtn');
  const confirmBtn     = document.getElementById('videoConfirmBtn');
  const overlayLabel   = document.querySelector('#loadingOverlay .overlay-label');

  let pendingDataset = null;          // dataset name currently in preview
  const excluded     = new Set();     // frame filenames the user clicked off

  function classifySharpness(score, lo, hi) {
    const span = Math.max(1, hi - lo);
    const t = (score - lo) / span;
    if (t >= 2 / 3) return 'sharp';
    if (t >= 1 / 3) return 'mid';
    return 'blur';
  }

  function refreshKeepLine(total) {
    const kept = total - excluded.size;
    keepLine.textContent = `Keeping ${kept} of ${total} frame${total === 1 ? '' : 's'}`;
    confirmBtn.disabled = kept === 0;
  }

  function renderPreview(data) {
    pendingDataset = data.dataset;
    excluded.clear();
    framesGrid.innerHTML = '';

    const lo = data.stats.min_sharpness;
    const hi = data.stats.max_sharpness;
    previewStats.textContent =
      `${data.stats.kept} frames kept · mean sharpness ${data.stats.mean_sharpness} ` +
      `(range ${lo} – ${hi})`;

    for (const f of data.frames) {
      const cls = classifySharpness(f.sharpness, lo, hi);
      const tile = document.createElement('div');
      tile.className = 'frame-tile';
      tile.dataset.name = f.name;
      tile.innerHTML =
        `<img alt="" src="data:image/jpeg;base64,${f.thumb_b64}">` +
        `<span class="frame-badge ${cls}">${f.sharpness}</span>` +
        `<span class="frame-name">${f.name}</span>`;
      tile.addEventListener('click', () => {
        if (excluded.has(f.name)) {
          excluded.delete(f.name);
          tile.classList.remove('excluded');
        } else {
          excluded.add(f.name);
          tile.classList.add('excluded');
        }
        refreshKeepLine(data.frames.length);
      });
      framesGrid.appendChild(tile);
    }

    refreshKeepLine(data.frames.length);
    previewCard.hidden = false;
  }

  function hidePreview() {
    previewCard.hidden = true;
    pendingDataset = null;
    excluded.clear();
    framesGrid.innerHTML = '';
  }

  videoForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!videoInput.files || !videoInput.files.length) return;
    const name = videoNameInput.value.trim();
    if (!name) return;

    const fd = new FormData();
    fd.append('name', name);
    fd.append('model', DEFAULT_MODEL);
    fd.append('fps', fpsSlider.value);
    fd.append('video', videoInput.files[0]);

    videoSubmitBtn.disabled = true;
    loadingOverlay.classList.add('show');
    overlayLabel.textContent = 'Extracting & scoring frames…';
    try {
      const r = await fetch('/setup/video', { method: 'POST', body: fd });
      const data = await r.json();
      if (!r.ok || !data.ok) {
        alert('Video processing failed: ' + (data.error || r.status));
        return;
      }
      renderPreview(data);
    } catch (err) {
      alert('Video processing failed: ' + err);
    } finally {
      loadingOverlay.classList.remove('show');
      overlayLabel.textContent = 'Uploading…';
      videoSubmitBtn.disabled = false;
    }
  });

  cancelBtn.addEventListener('click', async () => {
    if (!pendingDataset) { hidePreview(); return; }
    if (!confirm('Discard the extracted frames and start over?')) return;
    try {
      await fetch('/setup/video/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: pendingDataset }),
      });
    } catch (_) {}
    hidePreview();
  });

  confirmBtn.addEventListener('click', async () => {
    if (!pendingDataset) return;
    confirmBtn.disabled = true;
    loadingOverlay.classList.add('show');
    overlayLabel.textContent = 'Uploading to HESTIA…';
    try {
      const r = await fetch('/setup/video/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: pendingDataset,
          drop_frames: Array.from(excluded),
        }),
      });
      const data = await r.json();
      if (!r.ok || !data.ok) {
        alert('Upload failed: ' + (data.error || r.status));
        confirmBtn.disabled = false;
        return;
      }
      window.location.href = '/';
    } catch (err) {
      alert('Upload failed: ' + err);
      confirmBtn.disabled = false;
    } finally {
      loadingOverlay.classList.remove('show');
      overlayLabel.textContent = 'Uploading…';
    }
  });

  refreshVideoState();

  // ── HESTIA inline logic ───────────────────────────────────────────────────
  const hestiaLoading      = document.getElementById('hestiaLoading');
  const hestiaError        = document.getElementById('hestiaError');
  const hestiaList         = document.getElementById('hestiaList');
  const hestiaNamingCard   = document.getElementById('hestiaNamingCard');
  const hestiaDsName       = document.getElementById('hestiaDsName');
  const hestiaNamingCancel = document.getElementById('hestiaNamingCancel');
  const hestiaNamingConfirm= document.getElementById('hestiaNamingConfirm');
  const hestiaDlCard       = document.getElementById('hestiaDlCard');
  const hestiaDlSub        = document.getElementById('hestiaDlSub');
  const hestiaDlFill       = document.getElementById('hestiaDlFill');

  function fmtDate(iso) {
    try { return new Date(iso).toLocaleString(); } catch { return iso; }
  }

  async function loadHestiaScans() {
    hestiaLoading.style.display = 'flex';
    hestiaError.style.display   = 'none';
    hestiaList.style.display    = 'none';
    try {
      const r = await fetch('/hestia/scans', { cache: 'no-store' });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || 'Failed to load scans');
      renderHestiaScans(d.scans || []);
    } catch (e) {
      hestiaLoading.style.display = 'none';
      hestiaError.style.display   = 'block';
      hestiaError.textContent = `Could not reach HESTIA: ${e.message}`;
    }
  }

  function renderHestiaScans(scans) {
    hestiaLoading.style.display = 'none';
    hestiaList.innerHTML = '';
    if (!scans.length) {
      hestiaError.style.display = 'block';
      hestiaError.textContent   = 'No scans found in HESTIA.';
      return;
    }
    for (const s of scans) {
      const li = document.createElement('li');
      li.className = 'hestia-item';
      li.innerHTML = `
        <div class="hestia-icon">📦</div>
        <div class="hestia-meta">
          <div class="hestia-scan-id"></div>
          <div class="hestia-scan-sub"></div>
        </div>
        <button class="hestia-load-btn">Load</button>`;
      li.querySelector('.hestia-scan-id').textContent = s.scan_id;
      li.querySelector('.hestia-scan-sub').textContent =
        `${s.image_count} image${s.image_count === 1 ? '' : 's'} · ${fmtDate(s.timestamp)}`;
      li.querySelector('.hestia-load-btn').addEventListener('click', () => startHestiaLoad(s.scan_id, s.image_count));
      hestiaList.appendChild(li);
    }
    hestiaList.style.display = 'flex';
  }

  // Step 1: show naming card with pre-filled auto name
  function startHestiaLoad(scanId, imageCount) {
    document.querySelectorAll('.hestia-load-btn').forEach(b => b.disabled = true);
    hestiaList.style.display        = 'none';
    hestiaNamingCard.style.display  = 'flex';
    hestiaDsName.value = `scan_${scanId.slice(0, 8)}`;
    hestiaDsName.focus();
    hestiaDsName.select();

    hestiaNamingCancel.onclick = () => {
      hestiaNamingCard.style.display = 'none';
      document.querySelectorAll('.hestia-load-btn').forEach(b => b.disabled = false);
      hestiaList.style.display = 'flex';
    };

    hestiaNamingConfirm.onclick = () => {
      const name = hestiaDsName.value.trim().replace(/[^A-Za-z0-9._\-]/g, '_');
      if (!name) { hestiaDsName.focus(); return; }
      hestiaNamingCard.style.display = 'none';
      _doHestiaDownload(scanId, imageCount, name);
    };

    hestiaDsName.onkeydown = (e) => {
      if (e.key === 'Enter') hestiaNamingConfirm.click();
      if (e.key === 'Escape') hestiaNamingCancel.click();
    };
  }

  // Step 2: actually start the download with the chosen name
  function _doHestiaDownload(scanId, imageCount, datasetName) {
    hestiaDlCard.style.display = 'flex';
    hestiaDlSub.textContent    = 'Starting download…';
    hestiaDlFill.style.width   = '5%';

    fetch('/hestia/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scan_id: scanId, dataset_name: datasetName, model: DEFAULT_MODEL }),
    })
    .then(r => r.json())
    .then(d => {
      if (!d.ok) { showHestiaError(d.error || 'Load failed'); return; }
      pollHestia(scanId, imageCount, d.dataset);
    })
    .catch(e => showHestiaError(e.message));
  }

  function pollHestia(scanId, imageCount, datasetName) {
    let attempts = 0;
    const t = setInterval(async () => {
      attempts++;
      try {
        const r = await fetch(`/hestia/load/status?dataset=${encodeURIComponent(datasetName)}`, { cache: 'no-store' });
        const d = await r.json();
        if (!d.ok) { clearInterval(t); showHestiaError(d.error || 'Status error'); return; }

        const pct = imageCount > 0 ? Math.min(95, Math.round((d.downloaded / imageCount) * 100)) : 50;
        hestiaDlFill.style.width = pct + '%';
        hestiaDlSub.textContent  = `Downloaded ${d.downloaded}${imageCount ? ' / ' + imageCount : ''} images…`;

        if (d.status === 'done') {
          clearInterval(t);
          hestiaDlFill.style.width = '100%';
          hestiaDlSub.textContent  = `Done! Redirecting…`;
          setTimeout(() => { window.location.href = '/welcome'; }, 800);
        } else if (d.status === 'error') {
          clearInterval(t);
          showHestiaError(d.error || 'Download error');
        }
      } catch (_) {
        if (attempts > 120) { clearInterval(t); showHestiaError('Polling timeout'); }
      }
    }, 2000);
  }

  function showHestiaError(msg) {
    hestiaDlCard.style.display      = 'none';
    hestiaNamingCard.style.display  = 'none';
    hestiaError.style.display       = 'block';
    hestiaError.textContent         = `Error: ${msg}`;
    document.querySelectorAll('.hestia-load-btn').forEach(b => b.disabled = false);
    hestiaList.style.display        = 'flex';
  }
})();
