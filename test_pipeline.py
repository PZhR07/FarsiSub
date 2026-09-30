"""Exercise translation checkpoints and GPU failure recovery without downloading models."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import Jobs
from storage import read_json, write_json
from worker import Worker


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        write_json(self.folder / "request.json", {"device": "auto"})
        write_json(self.folder / "status.json", {"id": "test", "status": "queued"})

    def tearDown(self):
        self.temp.cleanup()

    def test_translation_keeps_timestamps_and_resumes_checkpoint(self):
        cues = [{"id": i, "start": i * 3.123, "end": i * 3.123 + 2,
                 "english": f"English {i}", "persian": ""} for i in range(1, 7)]
        write_json(self.folder / "translated.json", {"1": "ترجمه قبلی"})
        requests = []

        class Tokenizer:
            def __init__(self, **kwargs):
                pass

            def encode(self, text, out_type):
                return [text]

            def decode(self, tokens):
                return "ترجمه فارسی"

        class Translator:
            def __init__(self, *args, **kwargs):
                pass

            def translate_batch(self, sources, **kwargs):
                requests.extend(sources)
                assert kwargs["target_prefix"] == [["pes_Arab"] for _ in sources]
                return [SimpleNamespace(hypotheses=[["pes_Arab", "piece", "</s>"]]) for _ in sources]

        times = [(c["start"], c["end"]) for c in cues]
        with patch("sentencepiece.SentencePieceProcessor", Tokenizer), patch("ctranslate2.Translator", Translator):
            output = Worker(self.folder).translate(cues)
        self.assertEqual(times, [(c["start"], c["end"]) for c in output])
        self.assertEqual(len(requests), 5)
        self.assertTrue(all(tokens[-2:] == ["</s>", "eng_Latn"] for tokens in requests))
        self.assertEqual(output[0]["persian"], "ترجمه قبلی")
        self.assertEqual(len(read_json(self.folder / "translated.json")), 6)

    def test_gpu_crash_retries_once_on_cpu(self):
        manager = Jobs(self.folder / "jobs")
        manager.active = "test"
        environments = []

        class Process:
            def __init__(self, *args, **kwargs):
                environments.append(kwargs["env"])

            def wait(self):
                if len(environments) == 1:
                    write_json(folder / "status.json", {"status": "running", "device": "cuda"})
                    return -1
                state = read_json(folder / "status.json")
                state.update(status="done", device="cpu")
                write_json(folder / "status.json", state)
                return 0

        folder = self.folder
        with patch("app.subprocess.Popen", Process):
            manager._run(folder)
        self.assertEqual(len(environments), 2)
        self.assertEqual(environments[1]["FARSISUB_FORCE_CPU"], "1")
        self.assertEqual(read_json(folder / "status.json")["status"], "done")
        self.assertIsNone(manager.active)

    def test_explicit_gpu_mode_does_not_silently_retry(self):
        write_json(self.folder / "request.json", {"device": "cuda"})
        write_json(self.folder / "status.json", {"status": "running", "device": "cuda"})
        manager = Jobs(self.folder / "jobs")
        with patch("app.subprocess.Popen") as process:
            process.return_value.wait.return_value = -1
            manager._run(self.folder)
        self.assertEqual(process.call_count, 1)
        self.assertEqual(read_json(self.folder / "status.json")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
