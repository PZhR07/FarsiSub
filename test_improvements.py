import hashlib
import os
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from app import create_app
from media import audio_streams
from model_validation import digest_file, model_ready, seal_download
from outputs import cleanup_subtitles, output_folder, publish_subtitles, read_cues
from settings import ffmpeg_executable
from storage import read_json, write_json
from subtitles import annotate_readability, build_cues, wrap_text
from translation import sentence_groups, split_translation
from worker import Worker


class SubtitleTests(unittest.TestCase):
    def test_zero_duration_words_are_retained_in_order(self):
        words = [{"start":0,"end":1,"word":" Hello"},
                 {"start":1,"end":1,"word":" world"},
                 {"start":1.2,"end":1.8,"word":" again."}]
        cues = build_cues([{"words":words}],2)
        self.assertEqual(" ".join(c["english"] for c in cues),"Hello world again.")

    def test_invalid_word_time_fails_instead_of_discarding_text(self):
        with self.assertRaises(ValueError):
            build_cues([{"words":[{"start":1,"end":0,"word":"Hello"}]}],2)

    def test_wrap_bounds_every_line_without_removing_characters(self):
        for text in ["واژه " * 60, "x" * 200]:
            wrapped = wrap_text(text)
            self.assertTrue(all(len(line) <= 44 for line in wrapped.splitlines()))
            self.assertEqual("".join(wrapped.split()), "".join(text.split()))

    def test_readability_warnings_do_not_move_timestamps(self):
        rows = [{"start":1,"end":2,"persian":"واژه "*60}]
        annotate_readability(rows)
        self.assertEqual(len(rows[0]["readability"]),2)
        self.assertEqual((rows[0]["start"],rows[0]["end"]),(1,2))


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.spec = {"repo":"test/repo","revision":"a"*40,"files":["model.bin","config.json"]}
        cache = self.folder / ".cache" / "huggingface" / "download"
        cache.mkdir(parents=True)
        for name, content in [("model.bin",b"model contents"),("config.json",b'{"ok":true}')]:
            (self.folder / name).write_bytes(content)
            etag = (hashlib.sha256(content).hexdigest() if name.endswith(".bin")
                    else hashlib.sha1(f"blob {len(content)}\0".encode()+content).hexdigest())
            (cache / (name+".metadata")).write_text(self.spec["revision"]+"\n"+etag+"\n0\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_sealed_files_are_verified_and_cache_invalidates_on_corruption(self):
        seal_download(self.folder,self.spec)
        self.assertTrue(model_ready(self.folder,self.spec))
        self.assertTrue(model_ready(self.folder,self.spec))
        (self.folder / "model.bin").write_bytes(b"broken model!!")
        self.assertFalse(model_ready(self.folder,self.spec))

    def test_missing_invalid_and_wrong_revision_markers_are_not_ready(self):
        for marker in [None,"not JSON",{}, {**self.spec,"revision":"b"*40,"schema":2}]:
            if marker is not None:
                (self.folder / "ready.json").write_text(marker if isinstance(marker,str) else json.dumps(marker))
            self.assertFalse(model_ready(self.folder,self.spec))

    def test_sealing_rejects_truncated_file_and_unpinned_metadata(self):
        (self.folder / "model.bin").write_bytes(b"x")
        with self.assertRaises(ValueError):
            seal_download(self.folder,self.spec)
        self.assertFalse((self.folder / "ready.json").exists())

    def test_installer_forces_redownload_when_cached_file_is_damaged(self):
        with patch.dict(os.environ):
            import download_models
        calls = []
        def download(**kwargs):
            calls.append(kwargs)
            destination = Path(kwargs["local_dir"])
            cache = destination / ".cache" / "huggingface" / "download"
            cache.mkdir(parents=True,exist_ok=True)
            for name in self.spec["files"]:
                content = b"verified contents"
                (destination/name).write_bytes(content if kwargs.get("force_download") else b"")
                (cache/(name+".metadata")).write_text(self.spec["revision"]+"\n"+hashlib.sha256(content).hexdigest()+"\n0\n")
        with patch.object(download_models,"MODELS",self.folder), \
                patch.object(download_models,"MODEL_SPECS",{"test":self.spec}), \
                patch.object(download_models,"snapshot_download",side_effect=download),patch("builtins.print"):
            download_models.main()
        self.assertEqual(len(calls),2)
        self.assertTrue(calls[1]["force_download"])
        self.assertTrue(model_ready(self.folder/"test",self.spec))


class RetentionTests(unittest.TestCase):
    def test_cleanup_bounds_versions_and_retains_current_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(root)
            client = app.test_client()
            folder = root / ("a"*32); folder.mkdir()
            write_json(folder/"request.json",{"path":"movie.mp4"})
            write_json(folder/"status.json",{"status":"done"})
            def rows(text):
                return [{"id":1,"start":0,"end":2,"english":"Hello.","persian":text}]
            publish_subtitles(folder,rows("Initial"))
            download = client.get(f"/download/{folder.name}/srt", buffered=False)
            incomplete = folder/"subtitles"/("b"*32); incomplete.mkdir()
            (incomplete/"cues.json").write_text("[]")
            for i in range(8):
                publish_subtitles(folder,rows(f"Version {i}"))
            self.assertEqual(len(list((folder/"subtitles").iterdir())),3)
            self.assertEqual(read_cues(folder)[0]["persian"],"Version 7")
            self.assertIn("Initial",download.data.decode("utf-8-sig"))
            download.close()

    def test_pruning_failure_does_not_report_a_committed_save_as_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            rows = [{"id":1,"start":0,"end":2,"english":"Hello.","persian":"New"}]
            with patch("outputs.cleanup_subtitles",side_effect=PermissionError):
                publish_subtitles(folder,rows)
            self.assertEqual(read_cues(folder)[0]["persian"],"New")

    def test_failed_first_publication_removes_its_uncommitted_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            rows = [{"id":1,"start":0,"end":2,"english":"Hello.","persian":"New"}]
            with patch("outputs.write_text",side_effect=PermissionError),self.assertRaises(PermissionError):
                publish_subtitles(folder,rows)
            self.assertFalse((folder/"subtitles-current.json").exists())
            self.assertEqual(list((folder/"subtitles").iterdir()),[])


class AudioTests(unittest.TestCase):
    def test_real_second_audio_stream_is_described_and_extracted(self):
        import numpy as np
        import wave
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder/"multiaudio.mkv"
            subprocess.run([ffmpeg_executable(),"-hide_banner","-loglevel","error","-y",
                            "-f","lavfi","-i","sine=frequency=440:duration=1",
                            "-f","lavfi","-i","sine=frequency=880:duration=1",
                            "-map","0:a","-map","1:a","-c:a","pcm_s16le",
                            "-metadata:s:a:0","language=fra","-metadata:s:a:1","language=eng",str(source)],
                           check=True, capture_output=True, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            streams = audio_streams(source)
            self.assertEqual([s["language"] for s in streams],["fra","eng"])
            app = create_app(folder/"jobs");client=app.test_client()
            token = re.search(r'name="farsisub-token" content="([^"]+)"',client.get("/").text)[1]
            response=client.post("/api/media-info",json={"path":str(source)},headers={"X-FarsiSub-Token":token})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json["audio"][1]["track"],1)
            write_json(folder/"request.json",{"path":str(source),"device":"cpu","audio_track":1})
            audio,_ = Worker(folder).extract()
            with wave.open(str(audio)) as wav:
                samples=np.frombuffer(wav.readframes(wav.getnframes()),dtype=np.int16)
                sample_rate=wav.getframerate()
            peak=np.argmax(abs(np.fft.rfft(samples)))*sample_rate/len(samples)
            self.assertAlmostEqual(peak,880,delta=3)


class ContextTests(unittest.TestCase):
    def test_grouping_respects_sentences_and_silence(self):
        rows=[{"id":1,"start":0,"end":2,"english":"When I arrived"},
              {"id":2,"start":2,"end":4,"english":"she had left."},
              {"id":3,"start":8,"end":10,"english":"Hello."}]
        self.assertEqual([len(g) for g in sentence_groups(rows)],[2,1])
        parts=split_translation("وقتی رسیدم او رفته بود",rows[:2])
        self.assertTrue(all(parts))
        self.assertEqual(" ".join(parts),"وقتی رسیدم او رفته بود")
        self.assertIsNone(split_translation("سلام",rows[:2]))

    def test_context_translation_uses_sentence_and_resumes_per_sentence_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            write_json(folder/"request.json",{"device":"cpu","context":True})
            rows=[{"id":1,"start":0,"end":2,"english":"When I arrived","persian":""},
                  {"id":2,"start":2,"end":4,"english":"she had left.","persian":""}]
            requests=[]
            class Tokenizer:
                def __init__(self,**kwargs): pass
                def encode(self,text,out_type): return [text]
                def decode(self,tokens): return "وقتی رسیدم او رفته بود"
            class Translator:
                def __init__(self,*args,**kwargs): pass
                def translate_batch(self,sources,**kwargs):
                    requests.extend(sources)
                    return [SimpleNamespace(hypotheses=[["pes_Arab","piece"]]) for _ in sources]
            with patch("sentencepiece.SentencePieceProcessor",Tokenizer),patch("ctranslate2.Translator",Translator):
                Worker(folder).translate(rows)
                self.assertEqual(requests[0][0],"When I arrived she had left.")
                self.assertEqual(len(requests),1)
                Worker(folder).translate(rows)
                self.assertEqual(len(requests),1)
            self.assertEqual([(r["start"],r["end"]) for r in rows],[(0,2),(2,4)])
            self.assertEqual(" ".join(r["persian"] for r in rows),"وقتی رسیدم او رفته بود")


if __name__ == "__main__":
    unittest.main()
