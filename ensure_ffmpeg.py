"""Install bundled FFmpeg only if the computer does not already have FFmpeg."""
import subprocess
import sys
from settings import ffmpeg_executable

try:
    print("FFmpeg:", ffmpeg_executable())
except RuntimeError:
    subprocess.run([sys.executable, "-m", "pip", "install", "imageio-ffmpeg==0.6.0"], check=True)
    print("FFmpeg:", ffmpeg_executable())
