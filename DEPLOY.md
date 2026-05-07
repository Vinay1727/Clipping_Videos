# 🎬 ClipWave — Deployment Guide

**Frontend** → Vercel (free, always on)
**Backend** → Render (free, sleeps after 15 min inactivity)

---

## 📁 Folder Structure

```
clipweb/
├── backend/
│   ├── app.py              ← Flask API (Render pe deploy)
│   ├── requirements.txt
│   ├── Dockerfile          ← IMPORTANT: ffmpeg included
│   └── render.yaml
└── frontend/
    ├── index.html          ← Web UI (Vercel pe deploy)
    └── vercel.json
```

---

## 🚀 STEP 1 — Backend on Render

### 1.1 GitHub pe push karo
```bash
cd clipweb/backend
git init
git add .
git commit -m "ClipWave backend"
git branch -M main
git remote add origin https://github.com/YOURNAME/clipwave-backend.git
git push -u origin main
```

### 1.2 Render pe deploy
1. Jao: **https://render.com** → Sign up (free)
2. Click: **"New +"** → **"Web Service"**
3. Connect your GitHub repo: `clipwave-backend`
4. Settings:
   ```
   Name:           clipwave-api
   Runtime:        Docker          ← IMPORTANT (ffmpeg ke liye)
   Branch:         main
   Instance Type:  Free
   ```
5. Click **"Create Web Service"**
6. Wait ~5 min for build
7. Tumhara URL milega: `https://clipwave-api.onrender.com`

---

## 🌐 STEP 2 — Frontend on Vercel

### 2.1 index.html mein API URL update karo
`frontend/index.html` line 295:
```javascript
const API = "https://clipwave-api.onrender.com";  // ← apna Render URL
```

### 2.2 Vercel pe deploy
**Option A — Drag & Drop (easiest):**
1. Jao: **https://vercel.com** → Sign up
2. Dashboard pe **"Add New → Project"**
3. `frontend/` folder ko drag & drop karo
4. Deploy! URL milega: `https://clipwave.vercel.app`

**Option B — CLI:**
```bash
npm i -g vercel
cd clipweb/frontend
vercel
```

---

## ✅ STEP 3 — Test karo

1. Vercel URL open karo
2. Koi YouTube URL paste karo
3. "CLIP IT" click karo
4. 2–10 min wait karo (video length pe depend karta hai)
5. Clips download karo 🎉

---

## ⚠️ Known Limits (Free Tier)

| Issue | Workaround |
|---|---|
| Cold start 30–60s | Normal — first request slow hogi |
| Files delete after 30 min | Download karo jaldi |
| 720p max download | Instagram ke liye kaafi hai |
| 100 GB bandwidth/month | ~50–100 videos process kar sakte ho |
| 512MB RAM | Ek video at a time process hogi |

---

## 🔧 Troubleshooting

**"Failed to fetch" error** → Backend so gaya — ek baar manually visit karo `your-api.onrender.com`

**Download stuck** → File 30 min ke baad delete ho gayi — dobara process karo

**"ffmpeg not found"** → Make sure Runtime = Docker in Render settings

---

## 💡 Pro Tips

- **Render cold start fix**: UptimeRobot se free ping lagao har 14 min — service kabhi nahi soyegi
  - https://uptimerobot.com → Add Monitor → HTTP → your Render URL → 14 min interval
- **Bandwidth bachao**: Short clips (under 30 min movies) process karo
- **Speed**: 720p download kaafi fast hai vs 1080p

---

*ClipWave — Free forever for Instagram creators* 🎬
