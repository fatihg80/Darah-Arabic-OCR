"""
Mistral Arabic OCR — Flask Web Application (Batch Mode)
معالجة دفعات من ملفات PDF مع عرض النتائج المباشر في المتصفح عبر Server-Sent Events
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path
from queue import Empty, Queue

import markdown as md
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from docconv import extract_markdown_from_pdf

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

UPLOAD_FOLDER = Path("uploads")
ALLOWED_EXTENSIONS = {"pdf"}
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024  # 250 MB total

UPLOAD_FOLDER.mkdir(exist_ok=True)

# In-memory job store  { job_id: { queue, results, status } }
_jobs: dict = {}
_jobs_lock = threading.Lock()

MAX_RETRIES = 3
INITIAL_BACKOFF = 2  # seconds


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _pages_to_html(pages_markdown: list) -> list:
    return [
        md.markdown(page, extensions=["tables", "fenced_code", "nl2br"])
        for page in pages_markdown
    ]


def _emit(queue: Queue, event_type: str, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    queue.put(f"event: {event_type}\ndata: {payload}\n\n")


def _process_job(job_id: str, file_pairs: list) -> None:
    """Background worker — processes each PDF and streams SSE progress events."""
    job = _jobs[job_id]
    queue: Queue = job["queue"]
    results = []
    total = len(file_pairs)

    _emit(queue, "start", {"total": total})

    for idx, (original_name, file_path) in enumerate(file_pairs, start=1):
        _emit(queue, "file_start", {"index": idx, "total": total, "filename": original_name})

        attempts = 0
        backoff = INITIAL_BACKOFF
        success = False
        last_error = ""

        while attempts < MAX_RETRIES and not success:
            attempts += 1
            try:
                pages_markdown = extract_markdown_from_pdf(file_path)
                pages_html = _pages_to_html(pages_markdown)
                result = {
                    "filename": original_name,
                    "page_count": len(pages_html),
                    "pages": pages_html,
                    "pages_markdown": pages_markdown,
                    "status": "success",
                }
                results.append(result)
                success = True
                _emit(queue, "file_done", {**result, "index": idx, "total": total, "attempts": attempts})

            except Exception as exc:
                last_error = str(exc)
                if attempts < MAX_RETRIES:
                    _emit(queue, "file_retry", {
                        "index": idx,
                        "filename": original_name,
                        "attempt": attempts,
                        "max_retries": MAX_RETRIES,
                        "backoff": backoff,
                        "error": last_error,
                    })
                    time.sleep(backoff)
                    backoff *= 2

        if not success:
            result = {"filename": original_name, "status": "error", "error": last_error}
            results.append(result)
            _emit(queue, "file_done", {**result, "index": idx, "total": total})

        try:
            Path(file_path).unlink(missing_ok=True)
        except Exception:
            pass

    succeeded = sum(1 for r in results if r["status"] == "success")
    _emit(queue, "complete", {
        "total": total,
        "succeeded": succeeded,
        "failed": total - succeeded,
    })
    queue.put(None)  # sentinel — closes the SSE stream

    with _jobs_lock:
        job["results"] = results
        job["status"] = "done"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    """Accept multiple PDF uploads, launch background job, return job_id."""
    files = request.files.getlist("pdfs")
    if not files:
        return jsonify({"error": "لم يتم إرسال أي ملفات."}), 400

    file_pairs = []
    for file in files:
        if not file.filename or not _allowed_file(file.filename):
            continue
        safe_name = f"{uuid.uuid4().hex}.pdf"
        upload_path = UPLOAD_FOLDER / safe_name
        file.save(upload_path)
        file_pairs.append((file.filename, str(upload_path)))

    if not file_pairs:
        return jsonify({"error": "لا توجد ملفات PDF صالحة في الطلب."}), 400

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"queue": Queue(), "results": [], "status": "running"}

    threading.Thread(
        target=_process_job, args=(job_id, file_pairs), daemon=True
    ).start()

    return jsonify({"job_id": job_id, "file_count": len(file_pairs)})


@app.route("/stream/<job_id>")
def stream(job_id: str):
    """SSE endpoint — streams processing progress events for a job."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    queue: Queue = job["queue"]

    def generate():
        yield ": connected\n\n"
        while True:
            try:
                msg = queue.get(timeout=120)
                if msg is None:
                    return
                yield msg
            except Empty:
                yield ": keepalive\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
