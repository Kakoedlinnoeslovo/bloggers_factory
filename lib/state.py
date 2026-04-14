import json
import threading
from pathlib import Path

TMP_DIR = Path("tmp")
TMP_DIR.mkdir(exist_ok=True)

STATE_FILE = TMP_DIR / "bulk_generation_state.json"
REF_CACHE_FILE = TMP_DIR / "ref_urls_cache.json"

_state_lock = threading.Lock()
_ref_cache_lock = threading.Lock()


class State:
    """Thread-safe wrapper around bulk_generation_state.json."""

    def __init__(self, path: Path = STATE_FILE):
        self._path = path
        self._data: dict = {}

    def load(self) -> dict:
        with _state_lock:
            if self._path.exists():
                with open(self._path) as f:
                    self._data = json.load(f)
            else:
                self._data = {}
        return self._data

    def save(self):
        with _state_lock:
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)

    def get_model(self, model_name: str) -> dict:
        with _state_lock:
            if model_name not in self._data:
                self._data[model_name] = {
                    "completed_carousels": 0,
                    "completed_post_indices": [],
                    "total_posts_fetched": 0,
                    "posts_cache_file": "",
                    "completed_reels": 0,
                    "completed_reel_indices": [],
                    "reels_cache_file": "",
                }
            return self._data[model_name]

    def update_and_save(self, model_name: str, **kwargs):
        """Update fields on a model's state and persist to disk."""
        with _state_lock:
            ms = self._data.setdefault(model_name, {})
            ms.update(kwargs)
        self.save()

    def reset(self, model_name: str | None = None):
        if model_name:
            self._data.pop(model_name, None)
        else:
            self._data = {}
        self.save()

    @property
    def data(self) -> dict:
        return self._data


class RefCache:
    """Thread-safe wrapper around ref_urls_cache.json."""

    def __init__(self, path: Path = REF_CACHE_FILE):
        self._path = path

    def get(self, model_name: str) -> list[str]:
        with _ref_cache_lock:
            if self._path.exists():
                with open(self._path) as f:
                    cache = json.load(f)
                return cache.get(model_name, [])
        return []

    def set(self, model_name: str, urls: list[str]):
        with _ref_cache_lock:
            if self._path.exists():
                with open(self._path) as f:
                    cache = json.load(f)
            else:
                cache = {}
            cache[model_name] = urls
            with open(self._path, "w") as f:
                json.dump(cache, f, indent=2)
