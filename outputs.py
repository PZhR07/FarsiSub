"""Publish subtitle files as one immutable generation, with one commit point."""
import hashlib
import json
import re
import uuid
import shutil
from pathlib import Path

from storage import read_json, write_json, write_text
from subtitles import annotate_readability, render_srt, render_vtt


def output_folder(folder):
    folder = Path(folder)
    manifest = folder / "subtitles-current.json"
    if not manifest.exists():
        return folder  # Existing jobs retain their original file layout.
    data = read_json(manifest)
    generation = data.get("generation", "") if isinstance(data, dict) else ""
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ValueError("نسخهٔ زیرنویس معتبر نیست؛ پردازش را دوباره اجرا کنید.")
    result = folder / "subtitles" / generation
    if not result.is_dir():
        raise ValueError("فایل‌های نسخهٔ زیرنویس پیدا نشدند.")
    return result


def read_cues(folder):
    rows = read_json(output_folder(folder) / "cues.json")
    if not isinstance(rows, list):
        raise ValueError("فایل زیرنویس معتبر نیست.")
    return annotate_readability(rows)


def cleanup_subtitles(folder, keep=3):
    """Called under the job's edit lock; downloads materialize bytes under it too."""
    folder = Path(folder)
    current = output_folder(folder)
    versions = folder / "subtitles"
    if (not versions.is_dir() or current == folder or versions.is_symlink()
            or versions.is_junction() or versions.resolve() != folder.resolve() / "subtitles"):
        return
    candidates = [p for p in versions.iterdir() if p.is_dir()
                  and re.fullmatch(r"[0-9a-f]{32}", p.name)
                  and not p.is_symlink() and not p.is_junction()]
    candidates.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    retained = {current.resolve()}
    for candidate in candidates:
        if all((candidate / name).is_file() for name in ("cues.json", "persian.srt", "persian.vtt", "english.srt")):
            if len(retained) < keep:
                retained.add(candidate.resolve())
    for candidate in candidates:
        target = candidate.resolve()
        # Verify the absolute target before any recursive deletion on Windows.
        if target not in retained and target.parent == versions.resolve():
            try:
                shutil.rmtree(target)
            except OSError:
                pass  # Antivirus/reader locks will be retried on the next cleanup.


def revision(rows):
    data = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def publish_subtitles(folder, rows):
    """Readers see the previous generation until every new export is ready."""
    # Render before creating a generation: invalid timestamps never get published.
    annotate_readability(rows)
    exports = {"persian.srt": render_srt(rows), "persian.vtt": render_vtt(rows),
               "english.srt": render_srt(rows, "english")}
    generation = uuid.uuid4().hex
    destination = Path(folder) / "subtitles" / generation
    destination.mkdir(parents=True)
    try:
        write_json(destination / "cues.json", rows)
        for name, text in exports.items():
            write_text(destination / name, text, "utf-8" if name.endswith(".vtt") else "utf-8-sig")
        write_json(Path(folder) / "subtitles-current.json", {"generation": generation})
    except BaseException:
        manifest = read_json(Path(folder) / "subtitles-current.json", {})
        # Only remove this builder's uncommitted directory, after checking its target.
        versions = Path(folder) / "subtitles"
        target = destination.resolve()
        if (isinstance(manifest, dict) and manifest.get("generation") != generation
                and not versions.is_symlink() and not versions.is_junction()
                and not destination.is_symlink() and not destination.is_junction()
                and versions.resolve() == Path(folder).resolve() / "subtitles"
                and target.parent == versions.resolve()):
            try:
                shutil.rmtree(target)
            except OSError:
                pass
        raise
    try:
        cleanup_subtitles(folder)
    except OSError:
        pass  # A committed save stays successful even if cleanup needs another attempt.
