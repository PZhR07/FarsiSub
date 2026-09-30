"""Verify pinned local downloads against Hub metadata, then cache file checks."""
import hashlib
import json
import re
import threading
from pathlib import Path

from storage import read_json, write_json

_cache = {}
_lock = threading.Lock()


def digest_file(path, algorithm="sha256", git_blob=False):
    path = Path(path)
    digest = hashlib.new(algorithm)
    if git_blob:
        digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seal_download(folder, spec):
    """Use the downloader's pinned commit/ETag; never bless arbitrary local bytes."""
    folder = Path(folder)
    files = {}
    for name in spec["files"]:
        path = folder / name
        metadata = folder / ".cache" / "huggingface" / "download" / (name + ".metadata")
        lines = metadata.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2 or lines[0] != spec["revision"]:
            raise ValueError(f"Pinned download metadata is missing or different: {name}")
        etag = lines[1].strip('"')
        if re.fullmatch(r"[0-9a-f]{64}", etag):
            sha256 = digest_file(path)
            valid = sha256 == etag
        elif re.fullmatch(r"[0-9a-f]{40}", etag):
            valid = digest_file(path, "sha1", git_blob=True) == etag
            sha256 = digest_file(path)
        else:
            raise ValueError(f"Unsupported download checksum: {name}")
        if not valid or path.stat().st_size == 0:
            raise ValueError(f"Downloaded model file is incomplete or corrupt: {name}")
        files[name] = {"size": path.stat().st_size, "sha256": sha256}
    write_json(folder / "ready.json", {**spec, "schema": 2, "checksums": files})


def model_ready(folder, spec):
    folder = Path(folder)
    with _lock:
        try:
            marker = folder / "ready.json"
            paths = [marker] + [folder / name for name in spec["files"]]
            signature = tuple((p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in paths)
            key = (str(folder.resolve()), spec["revision"])
            if _cache.get(key, (None, None))[0] == signature:
                return _cache[key][1]
            manifest = read_json(marker)
            valid = isinstance(manifest, dict) and manifest.get("schema") == 2
            valid = valid and all(manifest.get(k) == spec[k] for k in ("repo", "revision", "files"))
            checksums = manifest.get("checksums", {}) if valid else {}
            valid = valid and isinstance(checksums, dict)
            if valid:
                for name in spec["files"]:
                    entry = checksums.get(name)
                    if (not isinstance(entry, dict) or entry.get("size", 0) <= 0
                            or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))
                            or (folder / name).stat().st_size != entry["size"]
                            or digest_file(folder / name) != entry["sha256"]):
                        valid = False
                        break
            _cache[key] = (signature, bool(valid))
            return bool(valid)
        except (OSError, ValueError, TypeError):
            return False
