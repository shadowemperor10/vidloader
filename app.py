import os
import re
import json
import subprocess
import threading
import uuid
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Auto-update yt-dlp on every startup
try:
    subprocess.run(["pip", "install", "--upgrade", "yt-dlp"], capture_output=True, timeout=120)
except Exception:
    pass


def sanitize_filename(name):
    name = re.sub(r'[\\/*?:"<>|#&%=+@!$^{}\[\]()\',;~`]', "_", name)
    name = re.sub(r'_+', '_', name)
    return name.strip('_')


SIZE_LIMITS = {
    "youtube":   None,
    "tiktok":    500 * 1024 * 1024,
    "instagram": 500 * 1024 * 1024,
    "other":     500 * 1024 * 1024,
}
SIZE_LABELS = {
    "youtube":   "Unlimited",
    "tiktok":    "500MB",
    "instagram": "500MB",
    "other":     "500MB",
}

jobs = {}


def detect_platform(url):
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    elif "tiktok.com" in url:
        return "tiktok"
    elif "instagram.com" in url:
        return "instagram"
    return "other"


def youtube_info_args():
    # mweb lists DASH formats AND bypasses the bot/cookie check
    return [
        "yt-dlp", "--no-playlist",
        "--extractor-args", "youtube:player_client=mweb,tv_embedded,web",
        "--user-agent", "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
        "--no-check-certificates", "--no-warnings",
    ]


def youtube_download_args():
    # web_creator supports DASH (separate video+audio streams) and bypasses bot check
    return [
        "yt-dlp", "--no-playlist",
        "--extractor-args", "youtube:player_client=web_creator,mweb,tv_embedded",
        "--user-agent", "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
        "--no-check-certificates", "--no-warnings",
    ]


def tiktok_args():
    # Use the mobile API hostname — less blocked on cloud IPs
    return [
        "yt-dlp", "--no-playlist",
        "--extractor-args", "tiktok:api_hostname=api22-normal-c-useast2a.tiktokv.com",
        "--user-agent", "com.zhiliaoapp.musically/2022600030 (Linux; U; Android 7.1.2; en_US; Pixel 2; Build/N2G48H; Cronet/58.0.2991.0)",
        "--add-header", "Referer:https://www.tiktok.com/",
        "--no-check-certificates", "--no-warnings",
    ]


def generic_args():
    return [
        "yt-dlp", "--no-playlist",
        "--user-agent", "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
        "--add-header", "Accept-Language:en-US,en;q=0.9",
        "--no-check-certificates", "--no-warnings",
    ]


def info_args_for(platform):
    if platform == "youtube":
        return youtube_info_args()
    elif platform == "tiktok":
        return tiktok_args()
    return generic_args()


def download_args_for(platform):
    if platform == "youtube":
        return youtube_download_args()
    elif platform == "tiktok":
        return tiktok_args()
    return generic_args()


HEIGHT_META = {
    2160: ("✨ 4K (2160p)", "Ultra HD"),
    1440: ("🔷 2K (1440p)", "Quad HD"),
    1080: ("🎬 1080p",      "Full HD"),
    720:  ("📹 720p",       "HD"),
    480:  ("📺 480p",       "SD"),
    360:  ("📱 360p",       "Low"),
    240:  ("🔻 240p",       "Very Low"),
    144:  ("⬇️ 144p",       "Minimum"),
}


def run_download(job_id, url, platform, quality="best"):
    jobs[job_id]["status"] = "downloading"
    jobs[job_id]["progress"] = 0

    output_template = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title)s.%(ext)s")
    size_limit = SIZE_LIMITS.get(platform, SIZE_LIMITS["other"])

    yt_dlp_args = download_args_for(platform) + [
        "-o", output_template,
        "--merge-output-format", "mp4",
        "--newline",
    ]

    if size_limit is not None:
        yt_dlp_args += ["--max-filesize", str(size_limit)]

    if platform == "youtube":
        if quality == "best":
            yt_dlp_args += ["-f", "bestvideo+bestaudio/best"]
        elif quality == "audio":
            yt_dlp_args.remove("--merge-output-format")
            yt_dlp_args.remove("mp4")
            yt_dlp_args += ["-f", "bestaudio", "-x", "--audio-format", "mp3"]
        else:
            h = quality.replace("p", "")
            yt_dlp_args += [
                "-f",
                f"bestvideo[height<={h}]+bestaudio"
                f"/bestvideo[height<={h}]+bestaudio[ext=m4a]"
                f"/best[height<={h}]"
                f"/bestvideo+bestaudio/best"
            ]
    else:
        yt_dlp_args += ["-f", "best"]

    yt_dlp_args.append(url)

    try:
        process = subprocess.Popen(yt_dlp_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        for line in process.stdout:
            line = line.strip()
            jobs[job_id]["log"] = line
            if "[download]" in line and "%" in line:
                m = re.search(r"(\d+\.\d+)%", line)
                if m:
                    jobs[job_id]["progress"] = float(m.group(1))
            if "Destination:" in line:
                jobs[job_id]["filename"] = os.path.basename(line.split("Destination:")[-1].strip())
            if "File is larger than max-filesize" in line or "larger than" in line:
                process.kill()
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = f"❌ File exceeds the {SIZE_LABELS.get(platform, '500MB')} limit for {platform.title()}."
                return

        process.wait()

        if process.returncode == 0:
            files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(job_id)]
            if files:
                old_path = os.path.join(DOWNLOAD_DIR, files[0])
                clean_name = sanitize_filename(files[0])
                new_path = os.path.join(DOWNLOAD_DIR, clean_name)
                if old_path != new_path:
                    os.rename(old_path, new_path)
                if size_limit and os.path.getsize(new_path) > size_limit:
                    os.remove(new_path)
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["error"] = f"❌ File exceeds the {SIZE_LABELS.get(platform, '500MB')} limit."
                    return
                jobs[job_id]["status"] = "done"
                jobs[job_id]["progress"] = 100
                jobs[job_id]["filename"] = clean_name
            else:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = "File not found after download."
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "Download failed. Check the URL or the video may be unavailable."

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
    finally:
        def cleanup():
            import time; time.sleep(600)
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(job_id):
                    try: os.remove(os.path.join(DOWNLOAD_DIR, f))
                    except: pass
        threading.Thread(target=cleanup, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    return send_file(os.path.join(os.path.dirname(__file__), "sw.js"), mimetype="application/javascript")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    platform = detect_platform(url)

    try:
        cmd = info_args_for(platform) + ["--dump-json", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout or "").strip()
            for line in reversed(err_msg.splitlines()):
                if "ERROR" in line or "error" in line.lower():
                    err_msg = line.strip()
                    break
            return jsonify({"error": f"Could not fetch video info. {err_msg[:220] or 'Check the URL.'}"}), 400

        info = json.loads(result.stdout)
        title     = info.get("title", "Unknown")
        thumbnail = info.get("thumbnail", "")
        duration  = info.get("duration_string", "")
        uploader  = info.get("uploader", "")

        platform = detect_platform(info.get("webpage_url", url))

        if platform == "youtube":
            seen_heights = set()
            for f in info.get("formats", []):
                h = f.get("height")
                vcodec = f.get("vcodec", "none")
                if h and vcodec and vcodec != "none":
                    seen_heights.add(h)

            qualities = [{"label": "🏆 Best Quality", "value": "best", "type": "video", "sub": "Highest available"}]
            for h in sorted(seen_heights, reverse=True):
                label, sub = HEIGHT_META.get(h, (f"🎬 {h}p", "Video"))
                qualities.append({"label": label, "value": f"{h}p", "type": "video", "sub": sub})
            qualities.append({"label": "🎵 Audio Only (MP3)", "value": "audio", "type": "audio", "sub": "MP3 · No video"})
        else:
            qualities = [{"label": "🏆 Best Quality", "value": "best", "type": "video", "sub": "Highest available"}]

        return jsonify({"title": title, "thumbnail": thumbnail, "duration": duration,
                        "uploader": uploader, "platform": platform, "qualities": qualities})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info. Try again."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    data    = request.json
    url     = data.get("url", "").strip()
    quality = data.get("quality", "best")
    if not url:
        return jsonify({"error": "URL is required"}), 400

    platform = detect_platform(url)
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "queued", "progress": 0, "filename": None,
        "error": None, "log": "", "platform": platform,
        "quality": quality, "size_limit": SIZE_LABELS.get(platform, "500MB"),
    }
    threading.Thread(target=run_download, args=(job_id, url, platform, quality), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def get_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/file/<job_id>")
def get_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    filepath = os.path.join(DOWNLOAD_DIR, job["filename"])
    if not os.path.exists(filepath):
        return jsonify({"error": "File missing on server"}), 404
    return send_file(filepath, as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)
