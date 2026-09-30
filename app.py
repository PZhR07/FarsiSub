"""Loopback-only web UI. Run with python app.py, never flask --host=0.0.0.0."""
import argparse
import atexit
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, render_template, request, send_file
from outputs import cleanup_subtitles, output_folder, publish_subtitles, read_cues, revision
from media import audio_streams

from settings import JOBS, ROOT, model_status, offline_environment
from storage import read_json, write_json

offline_environment()
TERMINAL = {"done", "failed", "cancelled"}
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".wmv", ".ts",
                    ".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}


def instance_lock():
    JOBS.mkdir(parents=True, exist_ok=True)
    handle = (JOBS / "app.lock").open("a+b")
    if handle.tell() == 0:
        handle.write(b" ")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        print("FarsiSub is already running. Close its previous window first.")
        raise SystemExit(1)
    return handle


class Jobs:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.active = None
        self.process = None
        self.edit_locks = {}
        self.stopping = False
        self.thread = None
        for folder in self.root.iterdir():
            if folder.is_dir() and (folder / "subtitles-current.json").is_file():
                try:
                    cleanup_subtitles(folder)
                except (OSError, ValueError):
                    pass
            state = read_json(folder / "status.json", {}) if folder.is_dir() else {}
            if state and state.get("status") not in TERMINAL:
                state.update(status="failed", message="اجرای قبلی ناتمام مانده است؛ ادامه را بزنید.")
                write_json(folder / "status.json", state)

    def folder(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            abort(404)
        folder = self.root / job_id
        if not (folder / "request.json").is_file():
            abort(404)
        return folder

    def start(self, folder):
        with self.lock:
            if self.stopping:
                raise ValueError("برنامه در حال بسته‌شدن است.")
            if self.active:
                raise ValueError("یک فیلم در حال پردازش است. صبر کنید یا آن را متوقف کنید.")
            (folder / "cancel.flag").unlink(missing_ok=True)
            state = read_json(folder / "status.json", {})
            state.update(status="queued", stage="queued", progress=0,
                         message="آماده‌سازی پردازش…", updated=time.time())
            write_json(folder / "status.json", state)
            self.active = folder.name
            self.thread = threading.Thread(target=self._run, args=(folder,), daemon=True)
            self.thread.start()

    def edit_lock(self, job_id):
        with self.lock:
            return self.edit_locks.setdefault(job_id, threading.Lock())

    def _run(self, folder):
        try:
            with (folder / "worker.log").open("a", encoding="utf-8") as log:
                for attempt in range(2):
                    env = os.environ.copy()
                    env["PYTHONIOENCODING"] = "utf-8"
                    if attempt:
                        env["FARSISUB_FORCE_CPU"] = "1"
                    with self.lock:
                        if self.stopping or (folder / "cancel.flag").exists():
                            state = read_json(folder / "status.json", {})
                            state.update(status="cancelled", message="پردازش متوقف شد.")
                            write_json(folder / "status.json", state)
                            break
                        process = subprocess.Popen([sys.executable, str(ROOT / "worker.py"), str(folder)],
                                                   cwd=str(ROOT), stdout=log, stderr=log, env=env,
                                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                                   start_new_session=os.name != "nt")
                        self.process = process
                    code = process.wait()
                    state = read_json(folder / "status.json", {})
                    cancelled = (folder / "cancel.flag").exists()
                    options = read_json(folder / "request.json")
                    if code and not cancelled and attempt == 0 and options["device"] == "auto" and state.get("device") == "cuda":
                        state.update(status="running", message="اجرای GPU موفق نشد؛ ادامه با CPU…",
                                     warning="GPU در دسترس نبود یا خطا داد؛ پردازش با CPU ادامه یافت.")
                        write_json(folder / "status.json", state)
                        continue
                    if cancelled and state.get("status") != "done":
                        state.update(status="cancelled", message="پردازش متوقف شد.")
                    elif code and state.get("status") != "failed":
                        state.update(status="failed", message="پردازش بسته شد. حالت CPU را امتحان کنید؛ جزئیات در worker.log است.")
                    write_json(folder / "status.json", state)
                    break
        except Exception as exc:
            state = read_json(folder / "status.json", {})
            state.update(status="failed", message=str(exc))
            write_json(folder / "status.json", state)
        finally:
            with self.lock:
                self.active = None
                self.process = None

    def cancel(self, folder):
        (folder / "cancel.flag").touch()

    def shutdown(self):
        from processes import stop_tree
        with self.lock:
            self.stopping = True
            if self.active:
                self.cancel(self.root / self.active)
            process, thread = self.process, self.thread
        if process and process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stop_tree(process)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=10)


def validate_path(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("مسیر کامل فایل را وارد کنید.")
    raw = value.strip().strip('"')
    if raw.startswith(("\\\\", "//")) or "://" in raw:
        raise ValueError("فقط مسیر فایل روی همین کامپیوتر پذیرفته می‌شود.")
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("فایل پیدا نشد. مسیر کامل فایل را بررسی کنید.")
    if path.suffix.lower() not in MEDIA_EXTENSIONS:
        raise ValueError("پسوند این فایل پشتیبانی نمی‌شود.")
    path = path.resolve()
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("مسیر شبکه پذیرفته نمی‌شود.")
    return path


def create_app(job_root=JOBS):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
    manager = Jobs(job_root)
    app.extensions["jobs"] = manager
    token = secrets.token_urlsafe(32)

    @app.before_request
    def local_only():
        if request.host.split(":")[0].lower() not in ("localhost", "127.0.0.1"):
            abort(403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                abort(403)
            if not secrets.compare_digest(request.headers.get("X-FarsiSub-Token", ""), token):
                abort(403)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'; object-src 'none'"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.get("/")
    def index():
        return render_template("index.html", token=token)

    @app.get("/api/system")
    def system():
        return jsonify(models=model_status(), active=manager.active, offline=True)

    @app.get("/api/jobs")
    def history():
        states = [read_json(p / "status.json", {}) for p in manager.root.iterdir() if p.is_dir()]
        return jsonify(sorted([s for s in states if s], key=lambda s: s.get("created", 0), reverse=True)[:50])

    @app.post("/api/media-info")
    def media_info():
        body = request.get_json()
        if not isinstance(body, dict):
            raise ValueError("درخواست نامعتبر است.")
        return jsonify(audio=audio_streams(validate_path(body.get("path"))))

    @app.post("/api/jobs")
    def start():
        body = request.get_json()
        if not isinstance(body, dict):
            raise ValueError("درخواست نامعتبر است.")
        path = validate_path(body.get("path"))
        device = body.get("device", "auto")
        if device not in ("auto", "cpu", "cuda"):
            raise ValueError("نوع پردازش نامعتبر است.")
        if not all(model_status().values()):
            raise ValueError("مدل‌ها آماده نیستند؛ ابتدا setup.bat را با اینترنت اجرا کنید.")
        if manager.active:
            return jsonify(error="یک فیلم در حال پردازش است."), 409
        track = body.get("audio_track", 0)
        if isinstance(track, bool) or not isinstance(track, int) or track < 0:
            raise ValueError("مسیر صوتی نامعتبر است.")
        if track >= len(audio_streams(path)):
            raise ValueError("مسیر صوتی انتخاب‌شده در فایل وجود ندارد.")
        context = body.get("context", True)
        if not isinstance(context, bool):
            raise ValueError("تنظیم ترجمه نامعتبر است.")
        job_id = uuid.uuid4().hex
        folder = manager.root / job_id
        folder.mkdir()
        info = path.stat()
        write_json(folder / "request.json", {"path": str(path), "device": device,
                                             "audio_track": track, "context": context,
                                             "size": info.st_size, "mtime": info.st_mtime_ns})
        write_json(folder / "status.json", {"id": job_id, "name": path.name, "created": time.time()})
        manager.start(folder)
        return jsonify(id=job_id), 202

    @app.get("/api/jobs/<job_id>")
    def status(job_id):
        return jsonify(read_json(manager.folder(job_id) / "status.json"))

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel(job_id):
        folder = manager.folder(job_id)
        if manager.active != job_id:
            raise ValueError("این پردازش فعال نیست.")
        manager.cancel(folder)
        return jsonify(ok=True)

    @app.post("/api/jobs/<job_id>/retry")
    def retry(job_id):
        folder = manager.folder(job_id)
        state = read_json(folder / "status.json")
        if state["status"] not in ("failed", "cancelled"):
            raise ValueError("این پردازش نیاز به ادامه ندارد.")
        options = read_json(folder / "request.json")
        path = validate_path(options["path"])
        info = path.stat()
        if info.st_size != options["size"] or info.st_mtime_ns != options["mtime"]:
            raise ValueError("فایل اصلی تغییر کرده؛ یک پردازش جدید ایجاد کنید.")
        if not all(model_status().values()):
            raise ValueError("ابتدا setup.bat را اجرا کنید.")
        manager.start(folder)
        return jsonify(id=job_id), 202

    @app.route("/api/jobs/<job_id>/cues", methods=["GET", "POST"])
    def cues(job_id):
        folder = manager.folder(job_id)
        if read_json(folder / "status.json").get("status") != "done":
            raise ValueError("زیرنویس هنوز آماده نیست.")
        with manager.edit_lock(job_id):
            rows = read_cues(folder)
            if request.method == "POST":
                if not request.headers.get("If-Match"):
                    return jsonify(error="صفحه را تازه کنید؛ نسخهٔ ویرایش مشخص نیست."), 428
                if request.if_match.star_tag or not request.if_match.contains(revision(rows)):
                    return jsonify(error="زیرنویس در تب دیگری تغییر کرده است. تغییرات شما حفظ شدند؛ نسخهٔ جدید را بررسی کنید."), 409
                changes = request.get_json()
                if not isinstance(changes, list) or len(changes) != len(rows):
                    raise ValueError("تعداد بخش‌ها تغییر کرده است؛ صفحه را تازه کنید.")
                # Only text is editable: source timestamps cannot be overwritten by a client.
                for row, change in zip(rows, changes):
                    if not isinstance(change, dict) or change.get("id") != row["id"]:
                        raise ValueError("ترتیب بخش‌ها نامعتبر است.")
                    text = change.get("persian")
                    if not isinstance(text, str) or not text.strip() or len(text) > 3000:
                        raise ValueError("متن هر بخش باید بین ۱ تا ۳۰۰۰ نویسه باشد.")
                    row["persian"] = " ".join(text.split())
                try:
                    publish_subtitles(folder, rows)
                except OSError:
                    app.logger.exception("Could not publish subtitles for %s", job_id)
                    return jsonify(error="ذخیره انجام نشد؛ فضای دیسک و دسترسی فایل‌ها را بررسی و دوباره تلاش کنید."), 503
            response = jsonify(rows)
            response.set_etag(revision(rows))
            return response

    @app.get("/download/<job_id>/<kind>")
    def download(job_id, kind):
        folder = manager.folder(job_id)
        files = {"srt": "persian.srt", "english": "english.srt", "vtt": "persian.vtt", "audio": "audio.wav"}
        if kind not in files:
            abort(404)
        stem = Path(read_json(folder / "request.json")["path"]).stem
        suffix = {"srt": ".fa.srt", "english": ".en.srt", "vtt": ".fa.vtt", "audio": ".wav"}[kind]
        if kind == "audio":
            path = folder / files[kind]
            if not path.is_file():
                abort(404)
            source = path
        else:
            # Readers keep their own bytes; pruning cannot invalidate an in-flight download.
            with manager.edit_lock(job_id):
                path = output_folder(folder) / files[kind]
                if not path.is_file():
                    abort(404)
                source = BytesIO(path.read_bytes())
        return send_file(source, as_attachment=kind != "vtt", download_name=stem + suffix,
                         mimetype="text/vtt; charset=utf-8" if kind == "vtt" else None, max_age=0)

    @app.get("/media/<job_id>")
    def media(job_id):
        folder = manager.folder(job_id)
        path = validate_path(read_json(folder / "request.json")["path"])
        return send_file(path, conditional=True)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    lock_handle = instance_lock()
    atexit.register(lock_handle.close)
    app = create_app()
    atexit.register(app.extensions["jobs"].shutdown)
    from waitress import create_server
    try:
        server = create_server(app, host="127.0.0.1", port=args.port, threads=6)
    except OSError:
        print("Port is occupied. Close the previous app or use: python app.py --port 8766")
        raise SystemExit(1)
    url = f"http://127.0.0.1:{args.port}"
    print(f"FarsiSub: {url}\nKeep this window open. Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        app.extensions["jobs"].shutdown()
        server.close()
