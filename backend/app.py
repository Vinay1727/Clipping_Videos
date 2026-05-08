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
import boto3
from pathlib import Path
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from dotenv import load_dotenv
from gradio_client import Client, handle_file

# Load .env file
load_dotenv()

# ── Config ──────────────────────────────────────
JOBS = {}          # in-memory job store { job_id: { status, clips, error } }
TMP_DIR = Path(tempfile.gettempdir()) / "clipwave"
TMP_DIR.mkdir(exist_ok=True)
COOKIE_PATH = TMP_DIR / "yt_cookies.txt"

app = Flask(__name__)
CORS(app)

# ── Startup: Create cookies.txt from Env ──
cookies_content = os.environ.get("YT_COOKIES_CONTENT", "")
if cookies_content:
    try:
        with open(COOKIE_PATH, "w") as f:
            f.write(cookies_content)
        print(f"[Cookies] Cookies created at {COOKIE_PATH}")
    except Exception as e:
        print(f"[Cookies] Error creating file: {e}")

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

# ── Cloudflare R2 Config ────────────────────────
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "").strip()
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "").strip()
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "clipwave-outputs").strip()
R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY
)

HF_SPACE_URL = os.environ.get("HF_SPACE_URL")

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

# ── HF + R2 Clipper ──
def run_clipper(job_id, video_path):
    try:
        update_job(job_id, status="analyzing", progress=30)

        # A) ML processing - HuggingFace ko bhejo (Using Gradio Client)
        client = Client(HF_SPACE_URL)
        result = client.predict(
            handle_file(str(video_path)),
            fn_index=0
        )
        
        # Gradio client returns the result directly
        ml_results = result 
        print(f"DEBUG: ML Result from Engine: {ml_results}")
        
        # Agar ML engine ne string bheji hai, matlab koi error hua hai
        if isinstance(ml_results, str):
            raise Exception(f"ML Engine Error: {ml_results}")
            
        if not isinstance(ml_results, list):
            raise Exception(f"Expected list from ML engine, but got {type(ml_results).__name__}")

        update_job(job_id, status="cutting_clips", progress=70)
        
        saved_clips = []
        for i, clip_data in enumerate(ml_results):
            # Agar list of strings aa rahi hai (jo ki ho raha hai)
            if isinstance(clip_data, str):
                clip_local_path = clip_data
                clip_label = f"Clip {i+1}"
            # Agar list of dicts aa rahi hai
            else:
                clip_local_path = clip_data.get("path")
                clip_label = clip_data.get("label", f"Part {i+1}")

            if not clip_local_path: continue
            
            clip_name = f"clip_{job_id}_{i+1}.mp4"
            
            # B) File storage - Cloudflare R2 upload
            s3.upload_file(clip_local_path, R2_BUCKET_NAME, f"{job_id}/{clip_name}")
            
            # Generate Public URL (Standard R2 public format points directly to bucket root)
            public_url = f"https://pub-{R2_ACCOUNT_ID}.r2.dev/{job_id}/{clip_name}"
            
            saved_clips.append({
                "name": clip_name,
                "part": f"Part {i+1}",
                "download_url": public_url
            })

        update_job(job_id, status="done", progress=100, clips=saved_clips)

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
            "--cookies", str(COOKIE_PATH),
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

@app.route("/admin/cookies", methods=["POST"])
def upload_cookies():
    """Update yt-dlp cookies.txt"""
    if "cookies" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["cookies"]
    f.save(str(COOKIE_PATH))
    return jsonify({"message": "Cookies updated"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
