"""One process per job: model memory is returned to Windows on completion."""
import gc
import os
import queue
import subprocess
import sys
import time
import threading
import traceback
import wave
from pathlib import Path

from settings import MODELS, MODEL_SPECS, ffmpeg_executable, offline_environment, prepare_cuda
from storage import read_json, write_json, write_text
from outputs import publish_subtitles
from processes import stop_tree
from subtitles import build_cues, render_srt
from translation import group_key, sentence_groups, split_translation

offline_environment()
prepare_cuda()


class Cancelled(Exception):
    pass


class Worker:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.request = read_json(self.folder / "request.json")
        self.state = read_json(self.folder / "status.json", {})
        self.device = "cpu"

    def check_cancel(self):
        if (self.folder / "cancel.flag").exists():
            raise Cancelled()

    def status(self, stage, progress, message, **extra):
        self.state.update(status="running", stage=stage, progress=round(progress, 1),
                          message=message, updated=time.time(), **extra)
        write_json(self.folder / "status.json", self.state)

    def extract(self):
        audio = self.folder / "audio.wav"
        if audio.is_file():
            with wave.open(str(audio)) as wav:
                return audio, wav.getnframes() / wav.getframerate()
        import av
        source = self.request["path"]
        with av.open(source) as container:
            if not container.streams.audio:
                raise ValueError("این فایل مسیر صوتی ندارد.")
            duration = (container.duration or 0) / av.time_base
            track = self.request.get("audio_track", 0)
            if isinstance(track, bool) or not isinstance(track, int) or not 0 <= track < len(container.streams.audio):
                raise ValueError("مسیر صوتی انتخاب‌شده در فایل وجود ندارد.")
        self.status("extracting", 2, "در حال استخراج صدا و حفظ سکوت‌ها…", device="—")
        temporary = self.folder / "audio.partial.wav"
        # FFmpeg normalizes container start time. async=1 inserts silence for audio
        # timestamp gaps (including delayed audio) instead of shifting subtitles.
        cmd = [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-nostdin",
               "-y", "-i", source, "-map", f"0:a:{track}", "-vn", "-af",
               "aresample=16000:async=1:first_pts=0", "-ac", "1", "-ar", "16000",
               "-c:a", "pcm_s16le", "-progress", "pipe:1", str(temporary)]
        with (self.folder / "ffmpeg.log").open("w", encoding="utf-8") as errors:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors,
                                       text=True, encoding="utf-8", errors="replace",
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                       start_new_session=os.name != "nt")
            progress_lines = queue.Queue()

            def read_progress():
                try:
                    for line in process.stdout:
                        progress_lines.put(line)
                finally:
                    progress_lines.put(None)

            reader = threading.Thread(target=read_progress, daemon=True)
            reader.start()
            try:
                while True:
                    self.check_cancel()
                    try:
                        line = progress_lines.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        # EOF may precede process exit; keep cancellation responsive.
                        while process.poll() is None:
                            self.check_cancel()
                            time.sleep(0.1)
                        break
                    if line.startswith("out_time_us=") and duration > 0:
                        try:
                            ratio = min(1, int(line.split("=", 1)[1]) / 1000000 / duration)
                            self.status("extracting", 2 + ratio * 10, "در حال استخراج صدا…")
                        except ValueError:
                            pass
                if process.wait() != 0:
                    raise ValueError("استخراج صدا ناموفق بود؛ فایل را در پخش‌کننده بررسی کنید. جزئیات در ffmpeg.log است.")
            finally:
                if process.poll() is None:
                    stop_tree(process)
                reader.join(timeout=2)
                process.stdout.close()
        self.check_cancel()
        temporary.replace(audio)
        with wave.open(str(audio)) as wav:
            duration = wav.getnframes() / wav.getframerate()
        if duration <= 0:
            raise ValueError("صدای قابل پردازشی در فایل پیدا نشد.")
        return audio, duration

    def transcribe(self, audio, duration):
        existing = read_json(self.folder / "source.json")
        if existing is not None:
            write_text(self.folder / "english.srt", render_srt(existing, "english"), "utf-8-sig")
            return existing
        from faster_whisper import WhisperModel
        self.status("transcribing", 13, "بارگذاری مدل تشخیص گفتار انگلیسی…", device=self.device)
        model = WhisperModel(str(MODELS / "whisper-small.en"), device=self.device,
                             compute_type="int8_float16" if self.device == "cuda" else "int8",
                             cpu_threads=min(4, os.cpu_count() or 2), num_workers=1,
                             local_files_only=True)
        self.check_cancel()
        generator, _ = model.transcribe(str(audio), language="en", task="transcribe",
                                        word_timestamps=True, vad_filter=True,
                                        vad_parameters={"min_silence_duration_ms": 500},
                                        beam_size=5, condition_on_previous_text=False)
        segments = []
        for segment in generator:
            self.check_cancel()
            segments.append({"start": segment.start, "end": segment.end, "text": segment.text,
                             "words": [{"start": w.start, "end": w.end, "word": w.word}
                                       for w in segment.words or []]})
            self.status("transcribing", 15 + 43 * min(segment.end / duration, 1),
                        f"تبدیل گفتار به متن؛ {int(segment.end)} از {int(duration)} ثانیه")
        del generator, model
        gc.collect()
        cues = build_cues(segments, duration)
        if not cues:
            raise ValueError("گفتار انگلیسی قابل تشخیصی پیدا نشد؛ صدا و مسیر صوتی اول فیلم را بررسی کنید.")
        write_json(self.folder / "source.json", cues)
        write_text(self.folder / "english.srt", render_srt(cues, "english"), "utf-8-sig")
        return cues

    def translate(self, cues):
        import ctranslate2
        import sentencepiece
        self.status("translating", 60, "بارگذاری مدل ترجمه فارسی…", device=self.device)
        model_path = MODELS / "nllb-600m-int8"
        tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(model_path / "sentencepiece.bpe.model"))
        translator = ctranslate2.Translator(str(model_path), device=self.device,
                                           compute_type="int8_float16" if self.device == "cuda" else "int8",
                                           inter_threads=1, intra_threads=min(4, os.cpu_count() or 2))
        # This converted checkpoint uses the original NLLB tokenizer convention:
        # sentence pieces + EOS + source language, target language as decoder prefix.
        contextual = self.request.get("context", False)  # Preserve existing jobs/checkpoints.
        units = sentence_groups(cues, contextual)
        checkpoint_dir = self.folder / "translations"
        if contextual:
            checkpoint_dir.mkdir(exist_ok=True)
        translated = read_json(self.folder / "translated.json", {}) if not contextual else {}
        memo = {}

        def translate_texts(texts):
            sources = [tokenizer.encode(text, out_type=str) + ["</s>", "eng_Latn"] for text in texts]
            if any(len(tokens) > 500 for tokens in sources):
                raise ValueError("یک بخش گفتار بیش از حد طولانی است؛ تقسیم‌بندی آن نیاز به بررسی دارد.")
            results = translator.translate_batch(sources, target_prefix=[["pes_Arab"] for _ in sources],
                                                 beam_size=4, max_batch_size=4, max_input_length=512,
                                                 max_decoding_length=256, repetition_penalty=1.1)
            output = []
            if len(results) != len(texts):
                raise ValueError("تعداد نتایج ترجمه با ورودی مطابقت ندارد.")
            for result in results:
                tokens = [t for t in result.hypotheses[0] if t not in ("pes_Arab", "</s>", "<s>", "<pad>")]
                if len(result.hypotheses[0]) >= 256:
                    raise ValueError("ترجمه یک بخش به سقف طول رسید؛ برای جلوگیری از حذف متن، پردازش متوقف شد.")
                text = tokenizer.decode(tokens).strip().replace("ي", "ی").replace("ك", "ک")
                if not text:
                    raise ValueError("مدل برای یک بخش ترجمه خالی تولید کرد. دوباره تلاش کنید.")
                output.append(text)
            return output

        completed = 0
        for offset in range(0, len(units), 4):
            self.check_cancel()
            batch = units[offset:offset + 4]
            missing = []
            for group in batch:
                if contextual:
                    key = group_key(group, MODEL_SPECS["nllb-600m-int8"]["revision"])
                    saved = read_json(checkpoint_dir / (key + ".json"))
                    if (isinstance(saved, list) and len(saved) == len(group)
                            and all(isinstance(s, str) and s.strip() for s in saved)):
                        translated.update({str(c["id"]): text for c, text in zip(group, saved)})
                if any(str(c["id"]) not in translated for c in group):
                    missing.append(group)
            if missing:
                texts = [" ".join(c["english"] for c in group) for group in missing]
                fresh = list(dict.fromkeys(text for text in texts if text not in memo))
                if fresh:
                    memo.update(zip(fresh, translate_texts(fresh)))
                for group, source in zip(missing, texts):
                    parts = split_translation(memo[source], group)
                    if parts is None:
                        parts = translate_texts([c["english"] for c in group])
                    for cue, text in zip(group, parts):
                        translated[str(cue["id"])] = text
                    if contextual:
                        key = group_key(group, MODEL_SPECS["nllb-600m-int8"]["revision"])
                        write_json(checkpoint_dir / (key + ".json"), parts)
                if not contextual:
                    write_json(self.folder / "translated.json", translated)
            for group in batch:
                for cue in group:
                    cue["persian"] = translated[str(cue["id"])]
                completed += len(group)
            self.status("translating", 60 + 36 * completed / len(cues),
                        f"ترجمه فارسی؛ {completed} از {len(cues)} بخش")
        del translator
        gc.collect()
        return cues

    def run(self):
        self.check_cancel()
        import ctranslate2
        requested = "cpu" if os.environ.get("FARSISUB_FORCE_CPU") else self.request["device"]
        self.device = "cuda" if requested != "cpu" and ctranslate2.get_cuda_device_count() else "cpu"
        if requested == "cuda" and self.device != "cuda":
            raise ValueError("کارت گرافیک در دسترس نیست. حالت خودکار یا CPU را انتخاب کنید.")
        audio, duration = self.extract()
        cues = self.translate(self.transcribe(audio, duration))
        self.check_cancel()
        self.status("saving", 98, "ساخت زیرنویس و فایل پیش‌نمایش…")
        publish_subtitles(self.folder, cues)
        if any(c.get("timing_warning") for c in cues):
            previous_warning = self.state.get("warning", "")
            self.state["warning"] = (previous_warning + " بخش‌های دارای هم‌پوشانی برای حفظ متن ادغام شدند؛ زمان‌بندی آن‌ها را بازبینی کنید.").strip()
        self.status("done", 100, "زیرنویس فارسی آماده است.", count=len(cues), duration=duration)
        self.state["status"] = "done"
        write_json(self.folder / "status.json", self.state)


if __name__ == "__main__":
    worker = Worker(sys.argv[1])
    try:
        worker.run()
    except Cancelled:
        worker.state.update(status="cancelled", message="پردازش متوقف شد؛ بخش‌های ذخیره‌شده برای ادامه باقی مانده‌اند.")
        write_json(worker.folder / "status.json", worker.state)
    except Exception as exc:
        traceback.print_exc()
        worker.state.update(status="failed", message=str(exc), updated=time.time())
        write_json(worker.folder / "status.json", worker.state)
        sys.exit(1)
