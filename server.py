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
# Enable CORS for all origins since it's going an APK on any device
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
        # Just remove the oldest items
        current_time = time.time()
        stream_cache = {k: v for k, v in stream_cache.items() if v["expires"] > current_time}


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

    # Normalized query key
    cache_key = query.lower()
    
    # Check Cache
    if cache_key in search_cache:
        cached = search_cache[cache_key]
        if time.time() < cached["expires"]:
            return jsonify(cached["data"])

    # Engine: YouTube Data API v3
    if not YOUTUBE_API_KEY:
        print("Error: YOUTUBE_API_KEY environment variable is missing.")
        return jsonify([])

    try:
        search_url = "https://www.googleapis.com/youtube/v3/search"
        params = {
            "part": "snippet",
            "type": "video",
            "videoCategoryId": "10", # Music category
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
                # Basic parsing, video duration is not natively in /search unless we hit /videos endpoint, 
                # but we will just omit duration or estimate. We'll leave it as None and fetch lazily if needed.
                snippet = item['snippet']
                video_id = item['id']['videoId']
                results.append({
                    "id": video_id,
                    "title": snippet.get('title', ''),
                    "artist": snippet.get('channelTitle', ''),
                    "thumbnail": snippet.get('thumbnails', {}).get('high', {}).get('url', ''),
                    "duration": 0 # Duration is populated upon stream-url loading
                })
            except Exception as e:
                continue
                
        # Update Cache
        search_cache[cache_key] = {
            "expires": time.time() + SEARCH_CACHE_TTL,
            "data": results
        }
        
        return jsonify(results)
    except requests.RequestException as e:
        print(f"YouTube Search Error: {e}")
        return jsonify([]) # Never 500
    except Exception as e:
        print(f"Unknown Search Error: {e}")
        return jsonify([])


@app.route('/stream-url', methods=['GET'])
def stream_url():
    video_id = request.args.get('id', '')
    quality = request.args.get('quality', 'max')
    
    if not video_id:
        return jsonify({"playable": False, "error": "Missing video id"})
        
    cache_key = f"{video_id}_{quality}"
    
    # Clean Cache
    clean_stream_cache()
    
    if cache_key in stream_cache:
        cached = stream_cache[cache_key]
        if time.time() < cached["expires"]:
            return jsonify(cached["data"])
            
    # Retry logic array
    formats_to_try = [
        "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "bestaudio",
        "best"
    ]
    
    for fmt in formats_to_try:
        try:
            ydp_opts = {
                "format": fmt,
                "quiet": True,
                "no_warnings": True,
                "no_check_certificate": True,
                "socket_timeout": 15
            }
            
            with yt_dlp.YoutubeDL(ydp_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                audio_url = info.get('url', None)
                
                if audio_url:
                    response_data = {
                        "playable": True,
                        "url": audio_url,
                        "title": info.get('title', ''),
                        "duration": info.get('duration', 0),
                        "thumbnail": info.get('thumbnail', ''),
                        "bitrate": quality
                    }
                    
                    # Update Cache
                    stream_cache[cache_key] = {
                        "expires": time.time() + STREAM_CACHE_TTL,
                        "data": response_data
                    }
                    
                    return jsonify(response_data)
                    
        except Exception as e:
            print(f"yt-dlp error for format '{fmt}': {e}")
            continue
            
    # All formats failed
    return jsonify({
        "playable": False,
        "error": "Unplayable stream. Audio extraction failed across all formats."
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
