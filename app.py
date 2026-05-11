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

# ── Auto-update yt-dlp on every startup so it never goes stale ──
try:
    subprocess.run(
        ["pip", "install", "--upgrade", "yt-dlp"],
        capture_output=True, timeout=120
    )
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


def yt_dlp_base_args():
    """Common yt-dlp args that help bypass bot-detection on cloud IPs."""
    return [
        "yt-dlp",
        "--no-playlist",
        # tv_embedded bypasses the "Sign in to confirm" bot check
        "--extractor-args", "youtube:player_client=tv_embedded,ios,web",
        # Smart TV user-agent matches tv_embedded client
        "--user-agent",
        "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/538.1 "
        "(KHTML, like Gecko) Version/6.0 TV Safari/538.1",
        "--no-check-certificates",
        "--no-warnings",
        # Small delay to avoid rate-limiting
        "--sleep-requests", "1",
    ]


def run_download(job_id, url, platform, quality="best"):
    jobs[job_id]["status"] = "downloading"
    jobs[job_id]["progress"] = 0

    output_template = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title)s.%(ext)s")
    size_limit = SIZE_LIMITS.get(platform, SIZE_LIMITS["other"])

    yt_dlp_args = yt_dlp_base_args() + [
        "-o", output_template,
        "--merge-output-format", "mp4",
        "--newline",
    ]

    if size_limit is not None:
        yt_dlp_args += ["--max-filesize", str(size_limit)]

    # Quality selection
    if platform == "youtube":
        if quality == "best":
            yt_dlp_args += ["-f", "bestvideo+bestaudio/best"]
        elif quality == "audio":
            # Remove merge-output-format for audio-only
            yt_dlp_args.remove("--merge-output-format")
            yt_dlp_args.remove("mp4")
            yt_dlp_args += ["-f", "bestaudio", "-x", "--audio-format", "mp3"]
        else:
            # e.g. "1080p" → exact height match, then fallback to best below it
            h = quality.replace("p", "")
            yt_dlp_args += [
                "-f",
                f"bestvideo[height={h}]+bestaudio"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/bestvideo+bestaudio/best"
            ]
    else:
        yt_dlp_args += ["-f", "best"]

    yt_dlp_args.append(url)

    try:
        process = subprocess.Popen(
            yt_dlp_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        for line in process.stdout:
            line = line.strip()
            jobs[job_id]["log"] = line

            if "[download]" in line and "%" in line:
                match = re.search(r"(\d+\.\d+)%", line)
                if match:
                    jobs[job_id]["progress"] = float(match.group(1))

            if "Destination:" in line:
                fname = line.split("Destination:")[-1].strip()
                jobs[job_id]["filename"] = os.path.basename(fname)

            if "File is larger than max-filesize" in line or "larger than" in line:
                process.kill()
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = (
                    f"❌ File exceeds the {SIZE_LABELS.get(platform, '500MB')} "
                    f"limit for {platform.title()}."
                )
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
                actual_size = os.path.getsize(new_path)
                if size_limit is not None and actual_size > size_limit:
                    os.remove(new_path)
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["error"] = (
                        f"❌ File exceeds the {SIZE_LABELS.get(platform, '500MB')} "
                        f"limit for {platform.title()}."
                    )
                    return
                jobs[job_id]["status"] = "done"
                jobs[job_id]["progress"] = 100
                jobs[job_id]["filename"] = clean_name
            else:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = "File not found after download. The video may be too large or unavailable."
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "Download failed. Check the URL or the video may exceed size limit."

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
    finally:
        def cleanup():
            import time
            time.sleep(600)
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(job_id):
                    try:
                        os.remove(os.path.join(DOWNLOAD_DIR, f))
                    except Exception:
                        pass
        threading.Thread(target=cleanup, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    return send_file(
        os.path.join(os.path.dirname(__file__), "sw.js"),
        mimetype="application/javascript"
    )


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        cmd = yt_dlp_base_args() + ["--dump-json", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            # Surface the real yt-dlp error so it's easier to debug
            err_msg = (result.stderr or result.stdout or "").strip()
            # Find the most useful line (last ERROR: line)
            for line in reversed(err_msg.splitlines()):
                if "ERROR" in line or "error" in line.lower():
                    err_msg = line.strip()
                    break
            return jsonify({
                "error": f"Could not fetch video info. {err_msg[:200] or 'Check the URL.'}"
            }), 400

        info = json.loads(result.stdout)
        title     = info.get("title", "Unknown")
        thumbnail = info.get("thumbnail", "")
        duration  = info.get("duration_string", "")
        uploader  = info.get("uploader", "")

        webpage_url = info.get("webpage_url", url)
        if "youtube.com" in webpage_url or "youtu.be" in webpage_url:
            platform = "youtube"
        elif "tiktok.com" in webpage_url:
            platform = "tiktok"
        elif "instagram.com" in webpage_url:
            platform = "instagram"
        else:
            platform = "other"

        if platform == "youtube":
            # Scan real formats — only show resolutions that actually exist
            formats = info.get("formats", [])
            seen_heights = set()
            for f in formats:
                h = f.get("height")
                vcodec = f.get("vcodec", "none")
                if h and vcodec and vcodec != "none":
                    seen_heights.add(h)

            # Map height → label/sub
            HEIGHT_META = {
                2160: ("✨ 4K (2160p)",  "Ultra HD"),
                1440: ("🔷 2K (1440p)",  "Quad HD"),
                1080: ("🎬 1080p",        "Full HD"),
                720:  ("📹 720p",         "HD"),
                480:  ("📺 480p",         "SD"),
                360:  ("📱 360p",         "Low"),
                240:  ("🔻 240p",         "Very Low"),
                144:  ("⬇️ 144p",         "Minimum"),
            }

            qualities = [{"label": "🏆 Best Quality", "value": "best", "type": "video", "sub": "Highest available"}]

            for h in sorted(seen_heights, reverse=True):
                label, sub = HEIGHT_META.get(h, (f"🎬 {h}p", "Video"))
                qualities.append({"label": label, "value": f"{h}p", "type": "video", "sub": sub})

            qualities.append({"label": "🎵 Audio Only (MP3)", "value": "audio", "type": "audio", "sub": "MP3 · No video"})
        else:
            qualities = [{"label": "🏆 Best Quality", "value": "best", "type": "video", "sub": "Highest available"}]

        return jsonify({
            "title": title,
            "thumbnail": thumbnail,
            "duration": duration,
            "uploader": uploader,
            "platform": platform,
            "qualities": qualities,
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info. Try again."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url     = data.get("url", "").strip()
    quality = data.get("quality", "best")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    if "youtube.com" in url or "youtu.be" in url:
        platform = "youtube"
    elif "tiktok.com" in url:
        platform = "tiktok"
    elif "instagram.com" in url:
        platform = "instagram"
    else:
        platform = "other"

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status":     "queued",
        "progress":   0,
        "filename":   None,
        "error":      None,
        "log":        "",
        "platform":   platform,
        "quality":    quality,
        "size_limit": SIZE_LABELS.get(platform, "500MB"),
    }

    t = threading.Thread(target=run_download, args=(job_id, url, platform, quality))
    t.daemon = True
    t.start()

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
