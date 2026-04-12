import base64
import logging
import sys
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

import requests

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("bloggers_factory")


def setup_logging(verbose: bool = False, parallel: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    tag = "parallel" if parallel else "bulk"
    fmt = "%(asctime)s [%(levelname)s] "
    if parallel:
        fmt += "[%(threadName)s] "
    fmt += "%(message)s"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / f"{tag}_{datetime.now():%Y-%m-%d_%H%M%S}.log"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)

    for noisy in ("httpx", "httpcore", "openai", "fal_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def retry(max_attempts=3, delay=2, backoff=2):
    """Decorator that retries a function on exception with exponential backoff."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception:
                    if attempt == max_attempts:
                        raise
                    wait = delay * backoff ** (attempt - 1)
                    logger.warning("%s failed (attempt %d/%d), retrying in %ds...",
                                   fn.__name__, attempt, max_attempts, wait)
                    time.sleep(wait)
        return wrapper
    return decorator


def download_image_as_base64(url: str) -> str | None:
    """Download an image URL and return as a base64 data URI."""
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            if ";" in content_type:
                content_type = content_type.split(";")[0].strip()
            b64 = base64.b64encode(resp.content).decode("utf-8")
            return f"data:{content_type};base64,{b64}"
        except Exception as e:
            logger.warning("Image download failed (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(3)
    return None


def download_file(url: str, dest: Path, timeout: int = 60) -> bool:
    """Download a URL to a local file path. Returns True on success."""
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            return True
        except Exception as e:
            logger.warning("Download failed (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(3)
    return False
