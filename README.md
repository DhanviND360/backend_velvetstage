# VelvetStage Backend

Flask + yt-dlp backend that powers the VelvetStage music streaming app.  
Provides `/search` and `/stream-url` endpoints used by the React Native frontend.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/search?q=<query>` | Search YouTube for music tracks |
| GET | `/stream-url?id=<videoId>&quality=<max\|low>` | Get a direct audio stream URL |

## Local Development

```bash
# 1. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set your environment variable
cp .env.example .env
# Edit .env and add your YOUTUBE_API_KEY

# 4. Run the server
python server.py
# Server starts on http://localhost:5000
```

## Deploy to Railway

1. Push this folder to a **new GitHub repository** (backend only).
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub Repo**.
3. Select your backend repo.
4. In Railway's dashboard, go to **Variables** and add:
   ```
   YOUTUBE_API_KEY = <your key here>
   ```
5. Railway will automatically detect `nixpacks.toml` (installs ffmpeg) and `Procfile` (`gunicorn server:app`).
6. Once deployed, copy the public URL (e.g. `https://your-backend.up.railway.app`) and update `CONFIG.SERVER_URL` in the frontend app.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `YOUTUBE_API_KEY` | ✅ Yes | Google/YouTube Data API v3 key |
| `PORT` | Auto-set by Railway | Port the server listens on |

> **Never commit your `.env` file.** Set secrets through Railway's Variables panel.

## Stack

- **Python 3** + **Flask**
- **yt-dlp** — audio stream extraction
- **gunicorn** — production WSGI server
- **ffmpeg** — installed via `nixpacks.toml` for audio processing
