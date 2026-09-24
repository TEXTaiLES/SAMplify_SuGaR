(() => {
  'use strict';

  const loadingCard  = document.getElementById('loadingCard');
  const errorCard    = document.getElementById('errorCard');
  const errorMsg     = document.getElementById('errorMsg');
  const scansCard    = document.getElementById('scansCard');
  const scansList    = document.getElementById('scansList');
  const scanCount    = document.getElementById('scanCount');
  const scanPlural   = document.getElementById('scanPlural');
  const downloadCard = document.getElementById('downloadCard');
  const dlSub        = document.getElementById('dlSub');
  const dlFill       = document.getElementById('dlFill');

  function fmtDate(iso) {
    if (!iso) return '';
    try { return new Date(iso).toLocaleString(); } catch { return iso; }
  }

  function showError(msg) {
    loadingCard.hidden = true;
    scansCard.hidden   = true;
    errorMsg.textContent = msg;
    errorCard.hidden = false;
  }

  function renderScans(scans) {
    loadingCard.hidden = true;
    scanCount.textContent = scans.length;
    scanPlural.textContent = scans.length === 1 ? '' : 's';
    scansList.innerHTML = '';

    for (const s of scans) {
      const li = document.createElement('li');
      li.className = 'hestia-item';
      li.innerHTML = `
        <div class="hestia-icon">📦</div>
        <div class="hestia-meta">
          <div class="hestia-scan-id">${s.scan_id}</div>
          <div class="hestia-scan-sub">${s.image_count} image${s.image_count === 1 ? '' : 's'} · ${fmtDate(s.timestamp)}</div>
        </div>
        <button class="hestia-load-btn" data-scan="${s.scan_id}">Load</button>`;
      li.querySelector('.hestia-load-btn').addEventListener('click', () => startLoad(s.scan_id, s.image_count));
      scansList.appendChild(li);
    }
    scansCard.hidden = false;
  }

  async function fetchScans() {
    try {
      const r = await fetch('/hestia/scans', { cache: 'no-store' });
      const d = await r.json();
      if (!d.ok) { showError(d.error || 'Failed to load scans'); return; }
      renderScans(d.scans || []);
    } catch (e) {
      showError(`Network error: ${e.message}`);
    }
  }

  function startLoad(scanId, imageCount) {
    // Disable all load buttons
    document.querySelectorAll('.hestia-load-btn').forEach(b => b.disabled = true);
    scansCard.hidden   = true;
    downloadCard.hidden = false;
    dlSub.textContent  = 'Starting download…';
    dlFill.style.width = '5%';

    fetch('/hestia/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scan_id: scanId }),
    })
    .then(r => r.json())
    .then(d => {
      if (!d.ok) { showError(d.error || 'Load failed'); return; }
      pollProgress(scanId, imageCount, d.dataset);
    })
    .catch(e => showError(`Request failed: ${e.message}`));
  }

  function pollProgress(scanId, imageCount, datasetName) {
    let attempts = 0;
    const timer = setInterval(async () => {
      attempts++;
      try {
        const r = await fetch(`/hestia/load/status?dataset=${encodeURIComponent(datasetName)}`, { cache: 'no-store' });
        const d = await r.json();
        if (!d.ok) { clearInterval(timer); showError(d.error || 'Status error'); return; }

        const pct = imageCount > 0 ? Math.min(95, Math.round((d.downloaded / imageCount) * 100)) : 50;
        dlFill.style.width = pct + '%';
        dlSub.textContent = `Downloaded ${d.downloaded}${imageCount ? ' / ' + imageCount : ''} images…`;

        if (d.status === 'done') {
          clearInterval(timer);
          dlFill.style.width = '100%';
          dlSub.textContent  = `Done! ${d.downloaded} images saved as dataset "${datasetName}".`;
          // Redirect to picker after short pause
          setTimeout(() => { window.location.href = '/welcome'; }, 1200);
        } else if (d.status === 'error') {
          clearInterval(timer);
          showError(`Download error: ${d.error}`);
        }
      } catch (e) {
        if (attempts > 60) { clearInterval(timer); showError('Polling timeout'); }
      }
    }, 2000);
  }

  fetchScans();
})();
