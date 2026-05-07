"""
🎬 ClipWave Backend — Render.com deployment
Flask API: accepts YouTube/streaming URL or file upload
Downloads → ML clips → returns download links
"""

import os
import uuid
import json
import threading
import subprocess
import tempfile
import time
import shutil
import requests
from pathlib import Path
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Self-Ping Keep-Alive (Render sleep prevent) ──────────────
SELF_PING_INTERVAL = 25   # seconds (Render sleeps after 15 min inactivity)
_keepalive_started = False

def _self_ping_loop():
    """
    Pings own /ping endpoint every 25 seconds.
    Keeps Render free service awake — no external service needed.
    Runs as background daemon thread.
    """
    time.sleep(10)  # wait for server to fully boot first
    own_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not own_url:
        svc = os.environ.get("RENDER_SERVICE_NAME", "")
        if svc:
            own_url = f"https://{svc}.onrender.com"
    if not own_url:
        print("[KeepAlive] RENDER_EXTERNAL_URL not set — add it in Render dashboard > Environment")
        return

    ping_url = own_url + "/ping"
    print(f"[KeepAlive] Started — pinging {ping_url} every {SELF_PING_INTERVAL}s")
    failures = 0
    while True:
        try:
            r = requests.get(ping_url, timeout=10)
            if r.status_code == 200:
                failures = 0
                print(f"[KeepAlive] ✓ {time.strftime('%H:%M:%S')}")
            else:
                failures += 1
        except Exception as e:
            failures += 1
            print(f"[KeepAlive] ✗ {e}")
        time.sleep(60 if failures >= 5 else SELF_PING_INTERVAL)

def start_keepalive():
    global _keepalive_started
    if not _keepalive_started:
        _keepalive_started = True
        threading.Thread(target=_self_ping_loop, daemon=True, name="KeepAlive").start()

start_keepalive()

# ── Config ──────────────────────────────────────
JOBS = {}          # in-memory job store { job_id: { status, clips, error } }
TMP_DIR = Path(tempfile.gettempdir()) / "clipwave"
TMP_DIR.mkdir(exist_ok=True)

CLIP_MIN = 40
CLIP_MAX = 60
CLIP_TARGET = 50
MAX_CLIPS = 5

# ── Helpers ──────────────────────────────────────
def update_job(job_id, **kwargs):
    JOBS[job_id].update(kwargs)

def cleanup_job_files(job_id):
    """Delete files after 30 min to save /tmp space"""
    time.sleep(1800)
    job_dir = TMP_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    JOBS.pop(job_id, None)

# ── ML Clipper (same logic as local clipper.py) ──
def run_clipper(job_id, video_path):
    try:
        import cv2
        import numpy as np
        import librosa
        from scipy.ndimage import uniform_filter1d

        update_job(job_id, status="analyzing", progress=10)

        job_dir = TMP_DIR / job_id
        job_dir.mkdir(exist_ok=True)
        clips_dir = job_dir / "clips"
        clips_dir.mkdir(exist_ok=True)

        # ── Get video info ──
        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 24
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps
        update_job(job_id, duration=round(duration), progress=15)

        # ── Audio extraction ──
        update_job(job_id, status="extracting_audio", progress=20)
        tmp_audio = job_dir / "audio.wav"
        try:
            subprocess.run([
                "ffmpeg", "-y", "-i", str(video_path),
                "-vn", "-ar", "8000", "-ac", "1",
                "-f", "wav", str(tmp_audio), "-loglevel", "error"
            ], check=True, timeout=120)
            # Optimization: Load at 8000Hz (60% less RAM than 22050Hz)
            # Enough for energy detection, significantly reduces memory footprint
            y, sr = librosa.load(str(tmp_audio), sr=8000, mono=True)
            rms = librosa.feature.rms(y=y, frame_length=sr*2, hop_length=sr)[0]
            audio_rms = np.array(rms)
        except Exception:
            audio_rms = np.zeros(int(duration) + 1)

        # ── Visual analysis (Optimized) ──
        update_job(job_id, status="analyzing_scenes", progress=35)
        # Sample every 1 second (fps * 1) instead of 0.3s to save CPU/RAM
        sample_every = max(1, int(fps * 1))
        prev_gray = None
        visual_scores = np.zeros(int(duration) + 1)
        frame_idx = 0
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx % sample_every == 0:
                # Resize to tiny 80x45 for extreme RAM efficiency
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                gray = cv2.resize(gray, (80, 45))
                if prev_gray is not None:
                    diff = cv2.absdiff(gray, prev_gray).mean()
                    sec = int(frame_idx / fps)
                    if sec < len(visual_scores):
                        visual_scores[sec] = max(visual_scores[sec], diff)
                prev_gray = gray
            frame_idx += 1
        cap.release()

        update_job(job_id, progress=55)

        # ── Interest score ──
        n = min(len(visual_scores), len(audio_rms))
        def norm(x):
            mn, mx = x.min(), x.max()
            return (x - mn) / (mx - mn + 1e-8)

        combined = 0.6 * norm(visual_scores[:n]) + 0.4 * norm(audio_rms[:n])
        smoothed = uniform_filter1d(combined.astype(float), size=10)

        # ── Find best segments ──
        update_job(job_id, status="finding_highlights", progress=65)
        used = set()
        segments = []
        for _ in range(MAX_CLIPS * 4):
            best_score, best_start = -1, -1
            for start in range(0, len(smoothed) - CLIP_MIN, 2):
                if any(abs(start - u) < 90 for u in used):
                    continue
                end = min(start + CLIP_TARGET, len(smoothed) - 1)
                if end - start < CLIP_MIN:
                    continue
                score = smoothed[start:end].mean()
                if score > best_score:
                    best_score, best_start = score, start
            if best_start == -1:
                break
            end = min(best_start + CLIP_TARGET, len(smoothed) - 1)
            segments.append({"start": best_start, "end": end,
                             "duration": end - best_start, "score": float(best_score)})
            used.add(best_start)
            if len(segments) >= MAX_CLIPS:
                break

        segments.sort(key=lambda x: x["start"])

        # ── Cut clips ──
        update_job(job_id, status="cutting_clips", progress=75)
        video_stem = Path(video_path).stem[:30]
        saved_clips = []

        for i, seg in enumerate(segments):
            fname = f"{video_stem}_Part{i+1}_t{seg['start']}s.mp4"
            out_path = clips_dir / fname
            try:
                subprocess.run([
                    "ffmpeg", "-y",
                    "-ss", str(seg["start"]),
                    "-i", str(video_path),
                    "-t", str(seg["duration"]),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                    "-c:a", "aac", "-b:a", "96k",
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
                    str(out_path), "-loglevel", "error"
                ], check=True, timeout=300)
                saved_clips.append({
                    "name": fname,
                    "part": f"Part {i+1}",
                    "start": seg["start"],
                    "end": seg["end"],
                    "duration": seg["duration"],
                    "score": round(seg["score"], 3),
                    "download_url": f"/download/{job_id}/{fname}"
                })
            except Exception as e:
                print(f"Clip {i+1} failed: {e}")

        update_job(job_id, status="done", progress=100, clips=saved_clips)

        # Schedule cleanup
        t = threading.Thread(target=cleanup_job_files, args=(job_id,), daemon=True)
        t.start()

    except Exception as e:
        update_job(job_id, status="error", error=str(e), progress=0)

def download_video(job_id, url):
    """Download from YouTube or any streaming URL using yt-dlp"""
    job_dir = TMP_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    out_template = str(job_dir / "video.%(ext)s")

    update_job(job_id, status="downloading", progress=5)
    try:
        subprocess.run([
            "yt-dlp",
            "--format", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
            "--merge-output-format", "mp4",
            "--output", out_template,
            "--no-playlist",
            "--no-check-certificates",
            "--force-ipv4",
            "--add-header", "Accept-Language: en-US,en;q=0.9",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "--extractor-args", "youtube:player-client=ios,android;player-skip=web",
            url
        ], check=True, timeout=600)

        # Find downloaded file
        for f in job_dir.iterdir():
            if f.suffix in (".mp4", ".mkv", ".avi", ".webm"):
                return f
        raise FileNotFoundError("Download completed but no video file found")
    except subprocess.TimeoutExpired:
        raise Exception("Video download timed out — try a shorter video")
    except subprocess.CalledProcessError as e:
        # Capture actual yt-dlp error for logs
        print(f"[yt-dlp Error] Command failed with code {e.returncode}")
        raise Exception(f"Download failed: YouTube is blocking this request. Try a different video or try again later.")

def process_job(job_id, url=None, file_path=None):
    """Full pipeline: download (if URL) → clip → done"""
    try:
        if url:
            video_path = download_video(job_id, url)
        else:
            video_path = file_path
        run_clipper(job_id, video_path)
    except Exception as e:
        update_job(job_id, status="error", error=str(e), progress=0)

# ── Routes ───────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ClipWave API running", "version": "1.0"})

@app.route("/clip/url", methods=["POST"])
def clip_from_url():
    """Accept YouTube / streaming URL"""
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {"status": "queued", "progress": 0, "clips": [], "error": None}

    t = threading.Thread(target=process_job, args=(job_id,), kwargs={"url": url}, daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "message": "Processing started"})

@app.route("/clip/upload", methods=["POST"])
def clip_from_upload():
    """Accept direct file upload"""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    job_id = str(uuid.uuid4())[:8]
    job_dir = TMP_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    ext = Path(f.filename).suffix or ".mp4"
    save_path = job_dir / f"upload{ext}"
    f.save(str(save_path))

    JOBS[job_id] = {"status": "queued", "progress": 0, "clips": [], "error": None}

    t = threading.Thread(target=process_job, args=(job_id,), kwargs={"file_path": save_path}, daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "message": "Processing started"})

@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id):
    """Poll job status"""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

@app.route("/download/<job_id>/<filename>", methods=["GET"])
def download_clip(job_id, filename):
    """Stream clip file to user"""
    clip_path = TMP_DIR / job_id / "clips" / filename
    if not clip_path.exists():
        return jsonify({"error": "File not found or expired"}), 404
    return send_file(str(clip_path), as_attachment=True, download_name=filename)


@app.route("/ping", methods=["GET"])
def ping():
    """Keep-alive endpoint — self-pinged every 25s to prevent Render sleep"""
    return jsonify({"status": "awake", "time": time.time()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
