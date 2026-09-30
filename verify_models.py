"""Upgrade existing downloaded models' integrity markers without network access."""
from model_validation import seal_download
from settings import MODELS, MODEL_SPECS


if __name__ == "__main__":
    for name, spec in MODEL_SPECS.items():
        seal_download(MODELS / name, spec)
        print(f"Verified: {name}", flush=True)
