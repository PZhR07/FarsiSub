"""Regression tests for lost edits, atomic publication and process cleanup."""
import os
import ctypes
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import Jobs, create_app
from outputs import output_folder, publish_subtitles, read_cues
from processes import stop_tree
from storage import read_json, write_json, write_text
from subtitles import build_cues, validate_cues
from worker import Cancelled, Worker


def sample(text="Original"):
    return [{"id": 1, "start": 0, "end": 2, "english": "Hello.", "persian": text, "readability": []}]


class StorageTests(unittest.TestCase):
    def test_concurrent_writers_publish_whole_files_with_unique_temporaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            barrier = threading.Barrier(2)
            replace = Path.replace
            names = []
            seen = set()
            guard = threading.Lock()

            def synchronized(source, destination):
                with guard:
                    first_attempt = source.name not in seen
                    if first_attempt:
                        seen.add(source.name)
                        names.append(source.name)
                if first_attempt:
                    barrier.wait(timeout=5)
                return replace(source, destination)

            values = [{"value": i, "text": str(i) * 10000} for i in range(2)]
            with patch.object(Path, "replace", synchronized), ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda value: write_json(path, value), values))
            self.assertEqual(len(set(names)), 2)
            self.assertIn(read_json(path), values)
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])

    def test_failed_file_replace_retains_previous_data_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            write_json(path, {"value": "old"})
            with patch.object(Path, "replace", side_effect=PermissionError), patch("storage.time.sleep"):
                with self.assertRaises(PermissionError):
                    write_json(path, {"value": "new"})
            self.assertEqual(read_json(path), {"value": "old"})
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])

    def test_incomplete_generation_never_becomes_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            publish_subtitles(folder, sample())
            previous = output_folder(folder)
            original = write_text

            for broken in ["persian.srt", "persian.vtt", "english.srt"]:
                with self.subTest(broken=broken):
                    def fail(path, text, encoding="utf-8"):
                        if Path(path).name == broken:
                            raise PermissionError("locked export")
                        original(path, text, encoding)

                    with patch("outputs.write_text", fail), self.assertRaises(PermissionError):
                        publish_subtitles(folder, sample("New"))
                    self.assertEqual(output_folder(folder), previous)
                    self.assertEqual(read_cues(folder), sample())

            original_json = write_json

            def fail_commit(path, value):
                if Path(path).name == "subtitles-current.json":
                    raise PermissionError("commit denied")
                original_json(path, value)

            with patch("outputs.write_json", fail_commit), self.assertRaises(PermissionError):
                publish_subtitles(folder, sample("New"))
            self.assertEqual(output_folder(folder), previous)


class EditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app(self.root)
        self.app.testing = True
        self.app.logger.disabled = True
        self.client = self.app.test_client()
        token = re.search(r'name="farsisub-token" content="([^"]+)"', self.client.get("/").text)[1]
        self.folder = self.root / ("a" * 32)
        self.folder.mkdir()
        write_json(self.folder / "request.json", {"path": "movie.mp4"})
        write_json(self.folder / "status.json", {"status": "done"})
        publish_subtitles(self.folder, sample())
        self.url = f"/api/jobs/{self.folder.name}/cues"
        self.headers = {"X-FarsiSub-Token": token,
                        "If-Match": self.client.get(self.url).headers["ETag"]}

    def tearDown(self):
        self.temp.cleanup()

    def test_stale_tab_cannot_overwrite_saved_edit(self):
        first = self.client.post(self.url, json=[{"id": 1, "persian": "First"}], headers=self.headers)
        self.assertEqual(first.status_code, 200)
        stale = self.client.post(self.url, json=[{"id": 1, "persian": "Stale"}], headers=self.headers)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.client.get(self.url).json[0]["persian"], "First")
        second = self.client.post(self.url, json=[{"id": 1, "persian": "Second"}],
                                  headers={**self.headers, "If-Match": first.headers["ETag"]})
        self.assertEqual(second.status_code, 200)

    def test_simultaneous_edits_accept_exactly_one_version(self):
        barrier = threading.Barrier(2)

        def save(text):
            with self.app.test_client() as client:
                barrier.wait(timeout=5)
                return client.post(self.url, json=[{"id": 1, "persian": text}], headers=self.headers).status_code

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(save, ["One", "Two"]))
        self.assertEqual(sorted(results), [200, 409])

    def test_missing_or_wildcard_revision_cannot_bypass_conflict_checks(self):
        for match, expected in [(None, 428), ("*", 409)]:
            headers = dict(self.headers)
            if match is None:
                headers.pop("If-Match")
            else:
                headers["If-Match"] = match
            result = self.client.post(self.url, json=[{"id": 1, "persian": "New"}], headers=headers)
            self.assertEqual(result.status_code, expected)

    def test_failed_save_keeps_editor_and_all_downloads_on_previous_version(self):
        original = write_text

        def fail(path, text, encoding="utf-8"):
            if Path(path).name == "persian.vtt":
                raise PermissionError("locked export")
            original(path, text, encoding)

        with patch("outputs.write_text", fail):
            response = self.client.post(self.url, json=[{"id": 1, "persian": "New"}], headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get(self.url).json, sample())
        for kind in ["srt", "vtt"]:
            download = self.client.get(f"/download/{self.folder.name}/{kind}")
            self.assertIn("Original", download.data.decode("utf-8-sig"))
            self.assertNotIn("New", download.data.decode("utf-8-sig"))
            download.close()


class RecoveryTests(unittest.TestCase):
    def test_completed_worker_publishes_all_exports_and_overlap_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            write_json(folder / "request.json", {"device": "cpu"})
            rows = sample()
            rows[0]["timing_warning"] = "merged_overlap"
            worker = Worker(folder)
            with patch.object(worker, "extract", return_value=(None, 2)), \
                    patch.object(worker, "transcribe", return_value=rows), \
                    patch.object(worker, "translate", return_value=rows):
                worker.run()
            destination = output_folder(folder)
            self.assertEqual(read_cues(folder), rows)
            for filename in ["cues.json", "persian.srt", "persian.vtt", "english.srt"]:
                self.assertTrue((destination / filename).is_file())
            state = read_json(folder / "status.json")
            self.assertEqual(state["status"], "done")
            self.assertEqual(state["progress"], 100)
            self.assertIn("هم‌پوشانی", state["warning"])

    def test_resume_rebuilds_missing_english_export_without_loading_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            write_json(folder / "request.json", {"device": "cpu"})
            write_json(folder / "source.json", sample())
            self.assertEqual(Worker(folder).transcribe(None, 2), sample())
            self.assertIn("Hello.", (folder / "english.srt").read_text(encoding="utf-8-sig"))

    def test_fully_overlapping_or_submillisecond_text_is_preserved(self):
        for start, end in [(1, 1.5), (2.0001, 2.0002)]:
            with self.subTest(start=start):
                cues = build_cues([{"text": "Hello.", "start": 0, "end": 2},
                                   {"text": "Goodbye.", "start": start, "end": end}], 3)
                self.assertEqual(" ".join(c["english"] for c in cues), "Hello. Goodbye.")
                self.assertEqual(cues[0]["timing_warning"], "merged_overlap")
                validate_cues(cues)

    def test_invalid_first_interval_fails_instead_of_discarding_text(self):
        with self.assertRaises(ValueError):
            build_cues([{"text": "Hello.", "start": 0, "end": .0001}], 1)

    def test_shutdown_before_worker_launch_never_spawns_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = Jobs(Path(temporary) / "jobs")
            folder = manager.root / ("a" * 32)
            folder.mkdir()
            write_json(folder / "request.json", {"device": "cpu"})
            write_json(folder / "status.json", {"status": "queued"})
            manager.active = folder.name
            manager.shutdown()
            with patch("app.subprocess.Popen") as spawn:
                manager._run(folder)
            spawn.assert_not_called()
            self.assertEqual(read_json(folder / "status.json")["status"], "cancelled")

    def test_cancel_extraction_without_any_progress_output(self):
        # Exercise a real quiet child/pipe, rather than a mocked iterator.
        from settings import ffmpeg_executable
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "source.wav"
            subprocess.run([ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "sine=duration=1", str(source)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            write_json(folder / "request.json", {"path": str(source), "device": "cpu"})
            real_spawn = subprocess.Popen
            children = []

            def quiet_child(command, **kwargs):
                if command[0] != ffmpeg_executable():
                    return real_spawn(command, **kwargs)
                process = real_spawn([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
                children.append(process)
                return process

            timer = threading.Timer(.3, lambda: (folder / "cancel.flag").touch())
            timer.start()
            try:
                started = time.monotonic()
                with patch("worker.subprocess.Popen", quiet_child), self.assertRaises(Cancelled):
                    Worker(folder).extract()
                self.assertLess(time.monotonic() - started, 8)
                self.assertIsNotNone(children[0].poll())
            finally:
                timer.cancel()
                timer.join()
                for process in children:
                    if process.poll() is None:
                        stop_tree(process)

    @unittest.skipUnless(os.name == "nt", "Windows subprocess tree cleanup")
    def test_forced_stop_ends_real_descendant_process(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "child.pid"
            code = ("import subprocess, sys, time; from pathlib import Path; "
                    "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                    "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)")
            parent = subprocess.Popen([sys.executable, "-c", code, str(marker)],
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            handle = None
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                handle = kernel.OpenProcess(0x00100000, False, int(marker.read_text()))
                self.assertTrue(handle)
                stop_tree(parent)
                self.assertEqual(kernel.WaitForSingleObject(handle, 2000), 0)
                self.assertIsNotNone(parent.poll())
            finally:
                if parent.poll() is None:
                    stop_tree(parent)
                if handle:
                    kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
