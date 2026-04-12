import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests

logger = logging.getLogger("bloggers_factory")


# ---------------------------------------------------------------------------
# Video duration (ffprobe)
# ---------------------------------------------------------------------------

def get_video_duration(video_path: Path) -> float:
    """Return video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


# ---------------------------------------------------------------------------
# Frame extraction (ffmpeg)
# ---------------------------------------------------------------------------

def extract_frames(
    video_path: Path,
    num_frames: int = 3,
    output_dir: Path | None = None,
) -> list[Path]:
    """Extract evenly-spaced frames from a video (beginning, middle, end).

    Returns a list of PNG file paths.
    """
    if output_dir is None:
        output_dir = video_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = get_video_duration(video_path)
    if duration <= 0:
        raise RuntimeError(f"Invalid video duration: {duration}")

    margin = min(0.5, duration * 0.05)
    if num_frames == 1:
        timestamps = [duration / 2]
    elif num_frames == 2:
        timestamps = [margin, duration - margin]
    else:
        step = (duration - 2 * margin) / (num_frames - 1)
        timestamps = [margin + i * step for i in range(num_frames)]

    frames: list[Path] = []
    for i, ts in enumerate(timestamps):
        dest = output_dir / f"frame_{i}.png"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{ts:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "2",
            str(dest),
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        if dest.exists():
            frames.append(dest)
            logger.info("Extracted frame %d at %.2fs -> %s", i, ts, dest.name)
        else:
            logger.warning("Frame extraction failed at %.2fs", ts)

    return frames


# ---------------------------------------------------------------------------
# Reel download / resolution
# ---------------------------------------------------------------------------

def _extract_instagram_shortcode(url: str) -> str | None:
    """Extract shortcode from an Instagram reel URL."""
    m = re.search(r"instagram\.com/(?:reel|reels|p)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def _download_with_ytdlp(url: str, dest: Path) -> bool:
    """Download a video using yt-dlp (fallback)."""
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        logger.warning("yt-dlp not found on PATH, skipping fallback download")
        return False

    cmd = [yt_dlp, "-o", str(dest), "--no-warnings", "-q", url]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return dest.exists()
    except Exception as e:
        logger.warning("yt-dlp download failed: %s", e)
        return False


def _download_with_requests(url: str, dest: Path, timeout: int = 60) -> bool:
    """Download a direct video URL with requests (3 retries)."""
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception as e:
            logger.warning("Video download failed (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(3)
    return False


def download_reel(source: str, dest_dir: Path) -> Path:
    """Resolve a reel source to a local mp4 file.

    Supports:
      - local file path  (e.g. "/path/to/reel.mp4")
      - direct video URL (e.g. "https://...video.mp4")
      - Instagram reel URL (e.g. "https://www.instagram.com/reel/ABC123/")
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    local = Path(source)
    if local.exists():
        logger.info("Using local reel file: %s", local)
        return local

    shortcode = _extract_instagram_shortcode(source)

    if shortcode:
        dest = dest_dir / f"{shortcode}.mp4"
        logger.info("Downloading Instagram reel (code=%s) via yt-dlp...", shortcode)
        if _download_with_ytdlp(source, dest):
            logger.info("Downloaded reel -> %s", dest)
            return dest
        raise RuntimeError(f"Could not download Instagram reel: {source}")

    dest = dest_dir / "source_reel.mp4"
    if _download_with_requests(source, dest):
        logger.info("Downloaded video -> %s", dest)
        return dest
    raise RuntimeError(f"Could not download video from URL: {source}")
