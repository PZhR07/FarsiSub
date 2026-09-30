"""Subtitle boundaries come from source audio, never from translated text length."""
import math
import re
import textwrap


def timestamp(seconds, separator=","):
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Invalid timestamp")
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"


def wrap_text(text, width=44):
    text = " ".join(str(text).split())
    return "\n".join(textwrap.wrap(text, width=width, break_long_words=True,
                                   break_on_hyphens=False))


def annotate_readability(cues):
    for cue in cues:
        text = " ".join(cue.get("persian", "").split())
        seconds = cue["end"] - cue["start"]
        warnings = []
        if len(wrap_text(text).splitlines()) > 2:
            warnings.append("متن بیش از دو خط است؛ برای خوانایی بهتر کوتاه‌ترش کنید.")
        if text and seconds > 0 and len(text) / seconds > 20:
            warnings.append("متن نسبت به زمان نمایش طولانی است؛ سرعت خواندن بالاست.")
        cue["readability"] = warnings
    return cues


def build_cues(segments, duration):
    cues, pending = [], []

    def flush():
        if not pending:
            return
        text = "".join(w["word"] for w in pending).strip()
        start = max(0, pending[0]["start"], cues[-1]["end"] if cues else 0)
        end = min(duration, pending[-1]["end"])
        start, end = round(start, 3), round(end, 3)
        if text and end > start:
            cues.append({"id": len(cues) + 1, "start": start, "end": end,
                         "english": text, "persian": ""})
        elif text and cues:
            # A fully contained/rounded-to-zero interval cannot become a new cue.
            # Keep its words in the preceding cue instead of silently dropping them.
            cues[-1]["english"] += " " + text
            cues[-1]["timing_warning"] = "merged_overlap"
        elif text:
            raise ValueError("زمان بخش گفتار قابل استفاده نیست؛ متن حذف نشد و پردازش متوقف شد.")
        pending.clear()

    for segment in segments:
        words = segment.get("words") or [{"start": segment["start"], "end": segment["end"],
                                          "word": segment["text"]}]
        for word in words:
            if not str(word["word"]).strip():
                continue
            if (not all(isinstance(word.get(key), (int, float)) and math.isfinite(word[key])
                        for key in ("start", "end")) or word["end"] < word["start"]):
                raise ValueError("زمان کلمه معتبر نیست؛ برای جلوگیری از حذف متن پردازش متوقف شد.")
            if pending:
                text_length = sum(len(w["word"]) for w in pending) + len(word["word"])
                if (word["end"] - pending[0]["start"] > 6 or text_length > 78
                        or word["start"] - pending[-1]["end"] > 0.8):
                    flush()
            # Whisper normally includes leading whitespace. A fallback segment may not.
            if pending and not word["word"].startswith(" "):
                word = dict(word, word=" " + word["word"])
            pending.append(word)
            if re.search(r'[.!?]["\x27]?$' , word["word"].strip()):
                flush()
    flush()
    return cues


def validate_cues(cues):
    previous = 0.0
    for cue in cues:
        start, end = cue["start"], cue["end"]
        if not all(isinstance(n, (float, int)) and math.isfinite(n) for n in (start, end)):
            raise ValueError("Invalid subtitle timestamps")
        if start < previous or end <= start:
            raise ValueError("Overlapping or invalid subtitle timestamps")
        previous = end


def render_srt(cues, language="persian"):
    validate_cues(cues)
    return "\n\n".join(f"{i}\n{timestamp(c['start'])} --> {timestamp(c['end'])}\n"
                        f"{wrap_text(c[language])}" for i, c in enumerate(cues, 1)) + "\n"


def render_vtt(cues):
    import html
    validate_cues(cues)
    return "WEBVTT\n\n" + "\n\n".join(
        f"{timestamp(c['start'], '.')} --> {timestamp(c['end'], '.')}\n"
        f"{html.escape(wrap_text(c['persian']))}" for c in cues) + "\n"
