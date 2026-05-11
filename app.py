import os
import re
import subprocess
import threading
import uuid
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Size limits per platform (in bytes)
SIZE_LIMITS = {
    "youtube":   1 * 1024 * 1024 * 1024,   # 1 GB
    "tiktok":    500 * 1024 * 1024,          # 500 MB
    "instagram": 500 * 1024 * 1024,          # 500 MB
    "other":     500 * 1024 * 1024,          # 500 MB
}

SIZE_LABELS = {
    "youtube":   "1GB",
    "tiktok":    "500MB",
    "instagram": "500MB",
    "other":     "500MB",
}

# In-memory job tracker
jobs = {}


def run_download(job_id, url, platform):
    jobs[job_id]["status"] = "downloading"
    jobs[job_id]["progress"] = 0

    output_template = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title)s.%(ext)s")
    size_limit = SIZE_LIMITS.get(platform, SIZE_LIMITS["other"])

    yt_dlp_args = [
        "yt-dlp",
        "--no-playlist",
        "-o", output_template,
        "--merge-output-format", "mp4",
        "--newline",
        "--max-filesize", str(size_limit),
    ]

    if platform == "youtube":
        yt_dlp_args += ["-f", "bestvideo+bestaudio/best"]
    elif platform == "tiktok":
        yt_dlp_args += ["-f", "best"]
    elif platform == "instagram":
        yt_dlp_args += ["-f", "best"]
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

            # Parse progress
            if "[download]" in line and "%" in line:
                match = re.search(r"(\d+\.\d+)%", line)
                if match:
                    jobs[job_id]["progress"] = float(match.group(1))

            # Parse filename
            if "Destination:" in line:
                fname = line.split("Destination:")[-1].strip()
                jobs[job_id]["filename"] = os.path.basename(fname)

            # File too large
            if "File is larger than max-filesize" in line or "larger than" in line:
                process.kill()
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = f"❌ File exceeds the {SIZE_LABELS.get(platform, '500MB')} limit for {platform.title()}."
                return

        process.wait()

        if process.returncode == 0:
            files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(job_id)]
            if files:
                # Double-check actual file size
                filepath = os.path.join(DOWNLOAD_DIR, files[0])
                actual_size = os.path.getsize(filepath)
                if actual_size > size_limit:
                    os.remove(filepath)
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["error"] = f"❌ File exceeds the {SIZE_LABELS.get(platform, '500MB')} limit for {platform.title()}."
                    return
                jobs[job_id]["status"] = "done"
                jobs[job_id]["progress"] = 100
                jobs[job_id]["filename"] = files[0]
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
        # Clean up file after 10 minutes
        def cleanup():
            import time
            time.sleep(600)
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(job_id):
                    try:
                        os.remove(os.path.join(DOWNLOAD_DIR, f))
                    except:
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


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Auto-detect platform
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
        "status": "queued",
        "progress": 0,
        "filename": None,
        "error": None,
        "log": "",
        "platform": platform,
        "size_limit": SIZE_LABELS.get(platform, "500MB"),
    }

    t = threading.Thread(target=run_download, args=(job_id, url, platform))
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
