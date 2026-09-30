"""The only entry point that downloads models; run once while online."""
import os
from pathlib import Path

os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ.pop("HF_HUB_OFFLINE", None)
os.environ.pop("TRANSFORMERS_OFFLINE", None)

from huggingface_hub import snapshot_download
from settings import MODELS, MODEL_SPECS
from model_validation import seal_download

# Bound metadata connections as well as redirected file downloads. Older Hub
# versions use requests; current versions use httpx.
try:
    import httpx
    from huggingface_hub import set_client_factory
    set_client_factory(lambda: httpx.Client(timeout=httpx.Timeout(45, connect=20), follow_redirects=True))
except ImportError:
    pass


def main():
    for name, spec in MODEL_SPECS.items():
        destination = MODELS / name
        print(f"Downloading / verifying {name}...", flush=True)
        snapshot_download(repo_id=spec["repo"], revision=spec["revision"],
                          local_dir=str(destination),
                          allow_patterns=spec["files"] + ["README.md"], max_workers=2)
        try:
            seal_download(destination, spec)
        except (OSError, ValueError):
            # Hub may reuse a locally cached file even if it was damaged later.
            print(f"Checksum mismatch for {name}; downloading a fresh copy...", flush=True)
            snapshot_download(repo_id=spec["repo"], revision=spec["revision"],
                              local_dir=str(destination), allow_patterns=spec["files"],
                              max_workers=2, force_download=True)
            seal_download(destination, spec)
    print("Models are ready. You can disconnect the internet and run start.bat.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Download failed: {exc}\nRun setup.bat again to resume.", flush=True)
        raise SystemExit(1)
