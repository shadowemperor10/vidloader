import os
import re
import json
import subprocess
import threading
import uuid
from flask import Flask, request, jsonify, send_file, render_template, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# In-memory job tracker
jobs = {}

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def run_download(job_id, url, platform):
    jobs[job_id]["status"] = "downloading"
    jobs[job_id]["progress"] = 0

    output_template = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title)s.%(ext)s")

    yt_dlp_args = [
        "yt-dlp",
        "--no-playlist",
        "-o", output_template,
        "--merge-output-format", "mp4",
        "--newline",
    ]

    if platform == "youtube":
        yt_dlp_args += ["-f", "bestvideo+bestaudio/best"]
    elif platform == "tiktok":
        yt_dlp_args += ["-f", "best"]
    elif platform == "instagram":
        yt_dlp_args += ["-f", "best"]

    yt_dlp_args.append(url)

    try:
        process = subprocess.Popen(
            yt_dlp_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        title = ""
        for line in process.stdout:
            line = line.strip()
            jobs[job_id]["log"] = line

            # Parse progress
            if "[download]" in line and "%" in line:
                match = re.search(r"(\d+\.\d+)%", line)
                if match:
                    jobs[job_id]["progress"] = float(match.group(1))

            # Parse title
            if "Destination:" in line:
                fname = line.split("Destination:")[-1].strip()
                jobs[job_id]["filename"] = os.path.basename(fname)

        process.wait()

        if process.returncode == 0:
            # Find the output file
            files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(job_id)]
            if files:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["progress"] = 100
                jobs[job_id]["filename"] = files[0]
            else:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = "File not found after download."
        else:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = "yt-dlp failed. Check the URL and try again."

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


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
        platform = "youtube"  # default, yt-dlp handles many sites

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "filename": None,
        "error": None,
        "log": "",
        "platform": platform
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
        return jsonify({"error": "File missing"}), 404

    return send_file(filepath, as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)
