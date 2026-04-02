import os
import json
import time
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_compress import Compress
import yt_dlp
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
# Enable CORS for all origins since it's going to an APK on any device
CORS(app, resources={r"/*": {"origins": "*"}})
# Enable gzip compression natively on all responses
Compress(app)

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")

# In-memory Caches
search_cache = {}    # Key: query, value: {"expires": float, "data": []}
stream_cache = {}    # Key: {id}_{quality}, value: {"expires": float, "data": dict}
# Config
SEARCH_CACHE_TTL = 30 * 60  # 30 minutes
STREAM_CACHE_TTL = 25 * 60  # 25 minutes
MAX_STREAM_CACHE = 150

def clean_stream_cache():
    global stream_cache
    if len(stream_cache) > MAX_STREAM_CACHE:
        current_time = time.time()
        stream_cache = {k: v for k, v in stream_cache.items() if v["expires"] > current_time}


@app.route('/', methods=['GET'])
def home():
    return "Backend is running successfully"


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })


@app.route('/search', methods=['GET'])
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])

    cache_key = query.lower()

    # Check Cache
    if cache_key in search_cache:
        cached = search_cache[cache_key]
        if time.time() < cached["expires"]:
            return jsonify(cached["data"])

    if not YOUTUBE_API_KEY:
        print("Error: YOUTUBE_API_KEY environment variable is missing.")
        return jsonify({"success": False, "error": "Server misconfiguration: missing API key"}), 500

    try:
        search_url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "part": "snippet",
            "type": "video",
            "videoCategoryId": "10",  # Music category
            "q": f"{query} official audio",
            "maxResults": 12,
            "key": YOUTUBE_API_KEY
        }

        response = requests.get(search_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get('items', []):
            try:
                snippet = item['snippet']
                video_id = item['id']['videoId']
                results.append({
                    "id": video_id,
                    "title": snippet.get('title', ''),
                    "artist": snippet.get('channelTitle', ''),
                    "thumbnail": snippet.get('thumbnails', {}).get('high', {}).get('url', ''),
                    "duration": 0
                })
            except Exception:
                continue

        # Update Cache
        search_cache[cache_key] = {
            "expires": time.time() + SEARCH_CACHE_TTL,
            "data": results
        }

        return jsonify(results)

    except requests.RequestException as e:
        print(f"YouTube Search Error: {e}")
        return jsonify({"success": False, "error": "Search request failed"}), 502
    except Exception as e:
        print(f"Unknown Search Error: {e}")
        return jsonify({"success": False, "error": "Unexpected error during search"}), 500


@app.route('/stream-url', methods=['GET'])
def stream_url():
    video_id = request.args.get('id', '')
    quality = request.args.get('quality', 'max')

    if not video_id:
        return jsonify({"playable": False, "error": "Missing video id"}), 400

    cache_key = f"{video_id}_{quality}"

    # Clean and check cache
    clean_stream_cache()

    if cache_key in stream_cache:
        cached = stream_cache[cache_key]
        if time.time() < cached["expires"]:
            return jsonify(cached["data"])

    # Format fallback list — ordered from best to most compatible
    # We extract direct stream URLs (no download), so postprocessors are NOT used
    formats_to_try = [
        "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "bestaudio/best",
        "best",
    ]

    for fmt in formats_to_try:
        try:
            ydl_opts = {
                "format": fmt,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "no_check_certificate": True,
                "socket_timeout": 20,
                # Extract URL only — do NOT download or re-encode
                "skip_download": True,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}",
                    download=False
                )

                # Prefer format-level URL, fall back to top-level
                audio_url = None
                if info.get("formats"):
                    for f in reversed(info["formats"]):
                        if f.get("url"):
                            audio_url = f["url"]
                            break
                if not audio_url:
                    audio_url = info.get("url")

                if audio_url:
                    response_data = {
                        "playable": True,
                        "url": audio_url,
                        "title": info.get("title", ""),
                        "duration": info.get("duration", 0),
                        "thumbnail": info.get("thumbnail", ""),
                        "bitrate": quality
                    }

                    stream_cache[cache_key] = {
                        "expires": time.time() + STREAM_CACHE_TTL,
                        "data": response_data
                    }

                    return jsonify(response_data)

        except yt_dlp.utils.DownloadError as e:
            print(f"yt-dlp DownloadError for format '{fmt}': {e}")
            continue
        except yt_dlp.utils.ExtractorError as e:
            print(f"yt-dlp ExtractorError for format '{fmt}': {e}")
            continue
        except Exception as e:
            print(f"Unexpected yt-dlp error for format '{fmt}': {e}")
            continue

    # All formats failed
    return jsonify({
        "playable": False,
        "error": "Audio extraction failed across all formats. The video may be restricted or unavailable."
    }), 200  # Return 200 so frontend parses the JSON normally


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
