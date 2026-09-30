"""Paths and pinned model revisions. No network access in the application."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
JOBS = Path(os.environ.get("FARSISUB_JOBS", str(ROOT / "data" / "jobs")))
MODEL_SPECS = {
    "whisper-small.en": {
        "repo": "Systran/faster-whisper-small.en",
        "revision": "d1d751a5f8271d482d14ca55d9e2deeebbae577f",
        "files": ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"],
    },
    "nllb-600m-int8": {
        "repo": "JustFrederik/nllb-200-distilled-600M-ct2-int8",
        "revision": "302d78f00e6fdb50a1064059df7c392b735e9d05",
        "files": ["config.json", "model.bin", "sentencepiece.bpe.model", "shared_vocabulary.txt"],
    },
}


def model_status():
    from model_validation import model_ready
    return {name: model_ready(MODELS / name, spec) for name, spec in MODEL_SPECS.items()}


def offline_environment():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"


def ffmpeg_executable():
    import shutil
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError("FFmpeg پیدا نشد؛ setup.bat را اجرا کنید.") from exc


_dll_handles = []


def prepare_cuda():
    """Discover optional NVIDIA pip DLLs without changing the system PATH."""
    if os.name != "nt":
        return
    import site
    dirs = [ROOT / "cuda"]
    for base in site.getsitepackages():
        dirs += [Path(base) / "nvidia" / part / "bin" for part in ("cublas", "cudnn", "cuda_runtime")]
    for path in dirs:
        if path.is_dir():
            os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")
            _dll_handles.append(os.add_dll_directory(str(path)))
