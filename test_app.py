import os
import re
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app import create_app, validate_path
from storage import read_json, write_json
from subtitles import build_cues, render_srt, render_vtt, timestamp, validate_cues, wrap_text
from worker import Cancelled, Worker


class TimingTests(unittest.TestCase):
    def test_millisecond_carry_and_long_duration(self):
        self.assertEqual(timestamp(59.9996), "00:01:00,000")
        self.assertEqual(timestamp(360000.125), "100:00:00,125")

    def test_silence_and_exact_word_boundaries(self):
        cues = build_cues([{"words": [
            {"start": 2.4, "end": 2.9, "word": " Hello"},
            {"start": 3.0, "end": 3.4, "word": " world."},
            {"start": 10.0, "end": 10.5, "word": " Goodbye."},
        ]}], 15)
        self.assertEqual([(c["start"], c["end"]) for c in cues], [(2.4, 3.4), (10, 10.5)])
        cues[0]["persian"] = "سلام دنیا؛ این یک ترجمه بسیار طولانی‌تر از متن اصلی است."
        cues[1]["persian"] = "خداحافظ."
        srt = render_srt(cues)
        self.assertIn("00:00:02,400 --> 00:00:03,400", srt)
        self.assertIn("00:00:10,000 --> 00:00:10,500", srt)

    def test_word_grouping_never_drops_words(self):
        words = [{"start": i * .5, "end": i * .5 + .3, "word": f" word{i}"} for i in range(100)]
        cues = build_cues([{"words": words}], 50)
        text = " ".join(c["english"] for c in cues)
        self.assertEqual(text.split(), [w["word"].strip() for w in words])
        validate_cues(cues)
        self.assertTrue(all(c["end"] - c["start"] <= 6.001 for c in cues))

    def test_negative_and_overlap_are_normalized(self):
        cues = build_cues([{"text": "Hi.", "start": -1, "end": 1},
                           {"text": "Bye.", "start": .9, "end": 2}], 1.8)
        self.assertEqual([(c["start"], c["end"]) for c in cues], [(0, 1), (1, 1.8)])
        validate_cues(cues)

    def test_empty_audio_does_not_invent_cues(self):
        self.assertEqual(build_cues([], 10), [])

    def test_invalid_timestamp_rejected(self):
        for value in [-1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                timestamp(value)
        with self.assertRaises(ValueError):
            validate_cues([{"start": 2, "end": 1}])

    def test_wrap_never_truncates_and_vtt_escapes_tags(self):
        text = "واژه " * 80
        self.assertEqual(wrap_text(text).split(), text.split())
        self.assertIn("&lt;b&gt;", render_vtt([{"start": 0, "end": 1, "persian": "<b>سلام"}]))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app(self.root / "jobs")
        self.app.testing = True
        self.client = self.app.test_client()
        html = self.client.get("/").get_data(as_text=True)
        self.token = re.search(r'name="farsisub-token" content="([^"]+)"', html)[1]
        self.headers = {"X-FarsiSub-Token": self.token}
        self.media = self.root / "فیلم با فاصله.mp4"
        self.media.write_bytes(b"test media")

    def tearDown(self):
        self.temp.cleanup()

    def fixture_job(self):
        folder = self.root / "jobs" / ("a" * 32)
        folder.mkdir()
        info = self.media.stat()
        write_json(folder / "request.json", {"path": str(self.media), "device": "cpu",
                                             "size": info.st_size, "mtime": info.st_mtime_ns})
        write_json(folder / "status.json", {"id": folder.name, "status": "done"})
        write_json(folder / "cues.json", [{"id": 1, "start": 2.345, "end": 4.567,
                                          "english": "Hello", "persian": "سلام"}])
        self.headers["If-Match"] = self.client.get(f"/api/jobs/{folder.name}/cues").headers["ETag"]
        return folder

    def test_loopback_host_and_csrf_guard(self):
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/jobs", json={}).status_code, 403)
        response = self.client.post("/api/jobs", json={}, headers={**self.headers, "Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_quoted_unicode_path_and_network_paths(self):
        self.assertEqual(validate_path('"' + str(self.media) + '"'), self.media.resolve())
        for raw in ("https://example.com/test.mp4", "\\\\server\\movie.mp4", "//server/a.mp4", "relative.mp4"):
            with self.assertRaises(ValueError):
                validate_path(raw)

    def test_missing_models_and_invalid_payloads(self):
        with patch("app.model_status", return_value={"speech": False}):
            response = self.client.post("/api/jobs", json={"path": str(self.media)}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("setup.bat", response.json["error"])
        self.assertEqual(self.client.post("/api/jobs", json=[], headers=self.headers).status_code, 400)

    def test_text_edit_cannot_change_timing(self):
        folder = self.fixture_job()
        response = self.client.post(f"/api/jobs/{folder.name}/cues", json=[{
            "id": 1, "start": 999, "end": 1000, "persian": "سلام دنیا"}], headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json[0]["start"], 2.345)
        subtitle = self.client.get(f"/download/{folder.name}/srt")
        self.assertTrue(subtitle.data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("00:00:02,345 --> 00:00:04,567", subtitle.data.decode("utf-8-sig"))
        subtitle.close()

    def test_empty_edit_rejected_and_original_retained(self):
        folder = self.fixture_job()
        response = self.client.post(f"/api/jobs/{folder.name}/cues", json=[{"id": 1, "persian": ""}], headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(read_json(folder / "cues.json")[0]["persian"], "سلام")

    def test_resume_rejects_changed_original(self):
        folder = self.fixture_job()
        write_json(folder / "status.json", {"status": "failed"})
        self.media.write_bytes(b"changed")
        response = self.client.post(f"/api/jobs/{folder.name}/retry", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("تغییر", response.json["error"])

    def test_job_id_cannot_escape_directory(self):
        self.assertEqual(self.client.get("/api/jobs/not-a-job").status_code, 404)
        self.assertEqual(self.client.get("/download/" + "a" * 32 + "/secret").status_code, 404)

    def test_browser_supports_range_requests(self):
        folder = self.fixture_job()
        response = self.client.get(f"/media/{folder.name}", headers={"Range": "bytes=0-3"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"test")
        response.close()


class AudioTests(unittest.TestCase):
    def test_delayed_audio_keeps_initial_silence(self):
        from settings import ffmpeg_executable
        import numpy as np
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "delayed.mp4"
            command = [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y",
                       "-f", "lavfi", "-i", "color=black:s=64x64:r=10:d=4",
                       "-itsoffset", "2", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                       "-c:v", "libx264", "-c:a", "aac", str(source)]
            subprocess.run(command, check=True, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            write_json(folder / "request.json", {"path": str(source), "device": "cpu"})
            worker = Worker(folder)
            audio, duration = worker.extract()
            with wave.open(str(audio)) as wav:
                samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
            self.assertGreater(duration, 2.9)
            self.assertLess(abs(samples[:int(1.8 * 16000)]).max(), 10)
            self.assertGreater(abs(samples[int(2.1 * 16000):int(2.8 * 16000)]).max(), 100)

    def test_cancel_before_model_load(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_json(folder / "request.json", {"device": "cpu"})
            (folder / "cancel.flag").touch()
            with self.assertRaises(Cancelled):
                Worker(folder).run()


if __name__ == "__main__":
    unittest.main()
