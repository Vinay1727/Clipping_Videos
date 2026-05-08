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
# Fixed: gradio-client ImportError by updating requirements.txt

JOBS = {}          # in-memory job store { job_id: { status, clips, error } }
TMP_DIR = Path(tempfile.gettempdir()) / "clipwave"
TMP_DIR.mkdir(exist_ok=True)
COOKIE_PATH = TMP_DIR / "yt_cookies.txt"

app = Flask(__name__)
CORS(app)

# ── Startup: Create cookies.txt from Env ──
cookies_content = os.environ.get("YT_COOKIES_CONTENT", "").strip()
if cookies_content:
    try:
        with open(COOKIE_PATH, "w") as f:
            f.write(cookies_content)
        print(f"[Cookies] Cookies file created at {COOKIE_PATH}")
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
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip("/")
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



def get_stream_url_via_piped(youtube_url):
    """Piped API se direct stream URL lo — no bot detection"""
    # Extract video ID
    if "v=" in youtube_url:
        video_id = youtube_url.split("v=")[-1].split("&")[0]
    elif "youtu.be/" in youtube_url:
        video_id = youtube_url.split("/")[-1].split("?")[0]
    else:
        video_id = youtube_url.split("/")[-1]
    
    piped_instances = [
        "https://pipedapi.kavin.rocks",
        "https://pipedapi.adminforge.de", 
        "https://piped-api.garudalinux.org"
    ]
    
    for instance in piped_instances:
        try:
            r = requests.get(f"{instance}/streams/{video_id}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                streams = data.get("videoStreams", [])
                
                # 720p ya usse kam prefer karo
                for s in streams:
                    if s.get("quality") in ["720p", "480p", "360p"]:
                        return s["url"], data.get("title", "video")
                
                if streams:
                    return streams[0]["url"], data.get("title", "video")
        except Exception as e:
            print(f"[Piped] {instance} failed: {e}")
            continue
    
    raise Exception("YouTube video access nahi ho pa raha — sabhi Piped instances fail")

def get_video_stream(job_id, url):
    """
    Video download NAHI karta — sirf stream URL return karta hai
    ffmpeg directly stream se clip kaatega
    """
    update_job(job_id, status="getting_stream", progress=5)
    
    if "youtube.com" in url or "youtu.be" in url:
        stream_url, title = get_stream_url_via_piped(url)
        return stream_url, title
    else:
        # Direct URL (Drive, direct mp4 link) — as-is return karo
        return url, "video"

from botocore.config import Config

def upload_to_r2(local_path, filename):
    """Clip ko R2 mein upload karo aur public URL return karo"""
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto"
    )
    
    bucket = os.environ["R2_BUCKET_NAME"]
    s3.upload_file(
        local_path, 
        bucket, 
        filename,
        ExtraArgs={"ContentType": "video/mp4"}
    )
    
    public_url = os.environ["R2_PUBLIC_URL"]
    return f"{public_url}/{filename}"

# ── HF + R2 Clipper ──
def run_clipper(job_id, video_input, is_stream=True):
    try:
        update_job(job_id, status="analyzing", progress=30)

        # A) ML processing - HuggingFace ko bhejo
        client = Client(HF_SPACE_URL)
        
        # Agar stream hai toh direct string, agar upload hai toh handle_file
        hf_input = video_input if is_stream else handle_file(str(video_input))
        
        result = client.predict(
            hf_input,
            api_name="/predict"
        )
        
        ml_results = result 
        if isinstance(ml_results, str):
            raise Exception(f"ML Engine Error: {ml_results}")
            
        update_job(job_id, status="cutting_clips", progress=70)
        
        saved_clips = []
        for i, clip_data in enumerate(ml_results):
            clip_local_path = clip_data if isinstance(clip_data, str) else clip_data.get("path")
            if not clip_local_path: continue
            
            clip_filename = f"{job_id}/clip_{i+1}.mp4"
            
            # B) File storage - Cloudflare R2 upload (Using new function)
            public_url = upload_to_r2(clip_local_path, clip_filename)
            
            saved_clips.append({
                "name": f"clip_{i+1}.mp4",
                "part": f"Part {i+1}",
                "download_url": public_url
            })

        update_job(job_id, status="done", progress=100, clips=saved_clips)

    except Exception as e:
        update_job(job_id, status="error", error=str(e), progress=0)

def process_job(job_id, url=None, file_path=None):
    """Full pipeline: get stream (if URL) → clip → done"""
    try:
        if url:
            # Stream URL lo — download mat karo
            stream_url, title = get_video_stream(job_id, url)
            run_clipper(job_id, stream_url, is_stream=True)
        else:
            # User uploaded file — R2 se direct path
            run_clipper(job_id, file_path, is_stream=False)
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
