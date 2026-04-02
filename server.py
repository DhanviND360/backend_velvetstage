import os
import time
import requests
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from flask_compress import Compress
import yt_dlp
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
Compress(app)

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

# In-memory Caches
search_cache = {}
stream_cache = {}
SEARCH_CACHE_TTL = 30 * 60   # 30 minutes
STREAM_CACHE_TTL = 20 * 60   # 20 minutes (YouTube signed URLs expire ~6hrs but we refresh early)
MAX_STREAM_CACHE = 100

# ---------------------------------------------------------------------------
# Spoofed headers — makes yt-dlp requests look like a real Android/Chrome user
# ---------------------------------------------------------------------------
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/112.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Mode": "navigate",
}

# ---------------------------------------------------------------------------
# yt-dlp attempt strategies — ordered from most bypass-friendly to fallback
# Each entry: (player_client list, format string)
# The android/ios clients bypass YouTube's web-bot detection entirely
# ---------------------------------------------------------------------------
EXTRACTION_STRATEGIES = [
    # Strategy 1: Android client — best bypass, no bot detection
    {
        "extractor_args": {"youtube": {"player_client": ["android"]}},
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
    },
    # Strategy 2: iOS client — second best bypass
    {
        "extractor_args": {"youtube": {"player_client": ["ios"]}},
        "format": "bestaudio/best",
    },
    # Strategy 3: Android + web combined
    {
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "format": "bestaudio/best",
    },
    # Strategy 4: TV embed client — often bypasses age restrictions too
    {
        "extractor_args": {"youtube": {"player_client": ["tv_embedded"]}},
        "format": "best",
    },
    # Strategy 5: Bare fallback — let yt-dlp choose anything
    {
        "extractor_args": {},
        "format": "best",
    },
]


def clean_stream_cache():
    global stream_cache
    if len(stream_cache) > MAX_STREAM_CACHE:
        now = time.time()
        stream_cache = {k: v for k, v in stream_cache.items() if v["expires"] > now}


def build_ydl_opts(strategy: dict) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "no_check_certificate": True,
        "socket_timeout": 30,
        "skip_download": True,
        "http_headers": BROWSER_HEADERS,
        "format": strategy["format"],
    }
    if strategy.get("extractor_args"):
        opts["extractor_args"] = strategy["extractor_args"]
    return opts


def extract_best_url(info: dict) -> str | None:
    """Pull the best audio URL from yt-dlp info dict."""
    # Try formats list first (pick last good one = highest quality)
    formats = info.get("formats") or []
    for fmt in reversed(formats):
        url = fmt.get("url", "")
        if url and not url.startswith("manifest"):
            return url
    # Fallback to top-level url
    return info.get("url")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    return "Backend is running successfully"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })


@app.route("/search", methods=["GET"])
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])

    cache_key = query.lower()
    if cache_key in search_cache:
        cached = search_cache[cache_key]
        if time.time() < cached["expires"]:
            return jsonify(cached["data"])

    if not YOUTUBE_API_KEY:
        print("Error: YOUTUBE_API_KEY missing.")
        return jsonify({"success": False, "error": "Server misconfiguration: missing API key"}), 500

    try:
        params = {
            "part": "snippet",
            "type": "video",
            "videoCategoryId": "10",
            "q": f"{query} official audio",
            "maxResults": 12,
            "key": YOUTUBE_API_KEY,
        }
        resp = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=params,
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("items", []):
            try:
                snippet = item["snippet"]
                results.append({
                    "id": item["id"]["videoId"],
                    "title": snippet.get("title", ""),
                    "artist": snippet.get("channelTitle", ""),
                    "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                    "duration": 0,
                })
            except Exception:
                continue

        search_cache[cache_key] = {"expires": time.time() + SEARCH_CACHE_TTL, "data": results}
        return jsonify(results)

    except requests.RequestException as e:
        print(f"YouTube Search Error: {e}")
        return jsonify({"success": False, "error": "Search request failed"}), 502
    except Exception as e:
        print(f"Unknown Search Error: {e}")
        return jsonify({"success": False, "error": "Unexpected error during search"}), 500


@app.route("/stream-url", methods=["GET"])
def stream_url():
    video_id = request.args.get("id", "").strip()
    quality = request.args.get("quality", "max")

    if not video_id:
        return jsonify({"playable": False, "error": "Missing video id"}), 400

    cache_key = f"{video_id}_{quality}"
    clean_stream_cache()

    if cache_key in stream_cache:
        cached = stream_cache[cache_key]
        if time.time() < cached["expires"]:
            return jsonify(cached["data"])

    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    last_error = "Unknown error"

    for i, strategy in enumerate(EXTRACTION_STRATEGIES):
        try:
            ydl_opts = build_ydl_opts(strategy)
            print(f"[stream-url] Trying strategy {i+1}/{len(EXTRACTION_STRATEGIES)}: "
                  f"client={strategy.get('extractor_args', {}).get('youtube', {}).get('player_client', ['default'])}, "
                  f"format={strategy['format']}")

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(yt_url, download=False)
                audio_url = extract_best_url(info)

                if audio_url:
                    print(f"[stream-url] Success with strategy {i+1}")
                    response_data = {
                        "playable": True,
                        "url": audio_url,
                        "title": info.get("title", ""),
                        "duration": info.get("duration", 0),
                        "thumbnail": info.get("thumbnail", ""),
                        "bitrate": quality,
                    }
                    stream_cache[cache_key] = {
                        "expires": time.time() + STREAM_CACHE_TTL,
                        "data": response_data,
                    }
                    return jsonify(response_data)
                else:
                    last_error = "yt-dlp returned no URL"

        except yt_dlp.utils.DownloadError as e:
            last_error = str(e)
            print(f"[stream-url] Strategy {i+1} DownloadError: {e}")
            continue
        except yt_dlp.utils.ExtractorError as e:
            last_error = str(e)
            print(f"[stream-url] Strategy {i+1} ExtractorError: {e}")
            continue
        except Exception as e:
            last_error = str(e)
            print(f"[stream-url] Strategy {i+1} unexpected error: {e}")
            continue

    print(f"[stream-url] All strategies failed for {video_id}. Last error: {last_error}")
    return jsonify({
        "playable": False,
        "error": "Could not extract audio stream. Try a different track.",
        "detail": last_error,
    }), 200  # 200 so frontend always parses the JSON body


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
