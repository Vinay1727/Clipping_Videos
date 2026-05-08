// ── Config ──
const API = "http://127.0.0.1:5000";

let currentJobId = null;
let pollInterval = null;
let selectedFile = null;

// ── Tab switching ──
window.switchTab = function(tab) {
  document.querySelectorAll('.tab').forEach((t, i) => t.classList.toggle('active', (i === 0 && tab === 'url') || (i === 1 && tab === 'upload')));
  document.getElementById('panel-url').classList.toggle('active', tab === 'url');
  document.getElementById('panel-upload').classList.toggle('active', tab === 'upload');
}

// ── File selected ──
window.fileSelected = function(input) {
  selectedFile = input.files[0];
  if (selectedFile) {
    document.getElementById('upload-filename').textContent = '✓ ' + selectedFile.name;
    document.getElementById('btn-upload').disabled = false;
  }
}

// Drag & drop
const zone = document.getElementById('upload-zone');
if (zone) {
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    const f = e.dataTransfer.files[0];
    if (f) {
      document.getElementById('file-input').files = e.dataTransfer.files;
      window.fileSelected({ files: [f] });
    }
  });
}

// ── Submit URL ──
window.submitUrl = async function() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) return alert('Please paste a URL first!');
  document.getElementById('btn-url').disabled = true;
  showError(false);
  showProgress(true);

  try {
    const res = await fetch(`${API}/clip/url`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    currentJobId = data.job_id;
    startPolling();
  } catch (e) {
    showError(true, e.message);
    showProgress(false);
    document.getElementById('btn-url').disabled = false;
  }
}

// ── Submit Upload ──
window.submitUpload = async function() {
  if (!selectedFile) return;
  document.getElementById('btn-upload').disabled = true;
  showError(false);
  showProgress(true);

  const formData = new FormData();
  formData.append('file', selectedFile);

  try {
    const res = await fetch(`${API}/clip/upload`, { method: 'POST', body: formData });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    currentJobId = data.job_id;
    startPolling();
  } catch (e) {
    showError(true, e.message);
    showProgress(false);
    document.getElementById('btn-upload').disabled = false;
  }
}

// ── Polling ──
function startPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(pollStatus, 2500);
}

async function pollStatus() {
  try {
    const res = await fetch(`${API}/status/${currentJobId}`);
    const job = await res.json();
    updateProgress(job);

    if (job.status === 'done') {
      clearInterval(pollInterval);
      showResults(job.clips);
    } else if (job.status === 'error') {
      clearInterval(pollInterval);
      showProgress(false);
      showError(true, job.error || 'Processing failed');
      document.getElementById('btn-url').disabled = false;
      document.getElementById('btn-upload').disabled = false;
    }
  } catch (e) {
    console.error('Poll error:', e);
  }
}

const STATUS_MAP = {
  queued: { label: 'QUEUED', steps: [] },
  downloading: { label: 'DOWNLOADING', steps: ['downloading'] },
  analyzing: { label: 'ANALYZING', steps: ['downloading', 'analyzing'] },
  extracting_audio: { label: 'READING AUDIO', steps: ['downloading', 'analyzing'] },
  analyzing_scenes: { label: 'READING SCENES', steps: ['downloading', 'analyzing'] },
  finding_highlights: { label: 'FINDING HIGHLIGHTS', steps: ['downloading', 'analyzing', 'finding'] },
  cutting_clips: { label: 'CUTTING CLIPS', steps: ['downloading', 'analyzing', 'finding', 'cutting'] },
  done: { label: 'DONE!', steps: ['downloading', 'analyzing', 'finding', 'cutting', 'done'] }
};

function updateProgress(job) {
  const info = STATUS_MAP[job.status] || { label: job.status.toUpperCase(), steps: [] };
  document.getElementById('status-text').textContent = info.label;
  document.getElementById('pct-text').textContent = (job.progress || 0) + '%';
  document.getElementById('progress-fill').style.width = (job.progress || 0) + '%';

  ['downloading', 'analyzing', 'finding', 'cutting', 'done'].forEach(s => {
    const el = document.getElementById('step-' + s);
    const isDone = info.steps.includes(s) && job.status !== s.replace('-', '_');
    const isActive = job.status.includes(s.substring(0, 6));
    el.className = 'step-pill' + (isDone ? ' done' : isActive ? ' active' : '');
  });
}

// ── Show results ──
function showResults(clips) {
  showProgress(false);
  document.getElementById('results-section').classList.add('visible');
  const grid = document.getElementById('clips-grid');
  grid.innerHTML = '';

  clips.forEach((clip, i) => {
    const scoreWidth = Math.round(clip.score * 100);
    const card = document.createElement('div');
    card.className = 'clip-card';
    card.style.animationDelay = (i * 0.1) + 's';
    card.innerHTML = `
        <div class="clip-part">${clip.part.toUpperCase()}</div>
        <div class="clip-meta">
          <div class="clip-name">${clip.name}</div>
          <div class="clip-info">
            <span>⏱ ${clip.duration}s</span>
            <span>📍 ${formatTime(clip.start)} → ${formatTime(clip.end)}</span>
            <div class="score-bar">
              <span>Score</span>
              <div class="score-track"><div class="score-fill" style="width:${scoreWidth}%"></div></div>
              <span>${scoreWidth}%</span>
            </div>
          </div>
        </div>
        <a class="btn-download" href="${clip.download_url}" target="_blank" download="${clip.name}">DOWNLOAD</a>
      `;
    grid.appendChild(card);
  });
}

function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

// ── UI helpers ──
function showProgress(show) {
  document.getElementById('progress-section').classList.toggle('visible', show);
}

function showError(show, msg = '') {
  const box = document.getElementById('error-box');
  box.classList.toggle('visible', show);
  if (msg) document.getElementById('error-text').textContent = '⚠ ' + msg;
}

window.resetAll = function() {
  currentJobId = null;
  if (pollInterval) clearInterval(pollInterval);
  document.getElementById('url-input').value = '';
  document.getElementById('upload-filename').textContent = '';
  document.getElementById('btn-url').disabled = false;
  document.getElementById('btn-upload').disabled = true;
  selectedFile = null;
  showProgress(false);
  showError(false);
  document.getElementById('results-section').classList.remove('visible');
  document.getElementById('progress-fill').style.width = '0%';
}
