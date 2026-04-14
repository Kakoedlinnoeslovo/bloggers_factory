import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

import requests

logger = logging.getLogger("bloggers_factory")

POSTS_CACHE_DIR = Path("posts_cache")

_API_URL = "https://instagram120.p.rapidapi.com/api/instagram/reels"


def _api_request(payload: str, api_key: str, label: str) -> requests.Response:
    """POST to the RapidAPI endpoint with retry on 5xx / transient errors."""
    for attempt in range(5):
        try:
            resp = requests.post(
                _API_URL,
                data=payload,
                headers={
                    "x-rapidapi-key": api_key,
                    "x-rapidapi-host": "instagram120.p.rapidapi.com",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            if resp.status_code >= 500:
                logger.warning("%s API returned %d (attempt %d/5): %s",
                               label, resp.status_code, attempt + 1, resp.text[:300])
                if attempt < 4:
                    time.sleep(5 * (attempt + 1))
                    continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError:
            raise
        except Exception as e:
            logger.warning("%s API request failed (attempt %d/5): %s", label, attempt + 1, e)
            if attempt < 4:
                time.sleep(5 * (attempt + 1))
            else:
                raise
    raise RuntimeError(f"{label} API request failed after 5 attempts")


def _extract_next_cursor(result: dict) -> tuple[str, bool]:
    """Extract pagination cursor and has-next flag from an API result dict."""
    page_info = result.get("page_info", {})
    next_max_id = (
        result.get("next_max_id", "")
        or result.get("end_cursor", "")
        or page_info.get("end_cursor", "")
    )
    has_next = (
        result.get("has_next_page", False)
        or result.get("more_available", False)
        or page_info.get("has_next_page", False)
    )

    if not next_max_id and not has_next:
        paging = result.get("paging_info", {})
        next_max_id = paging.get("max_id", "") or paging.get("end_cursor", "")
        has_next = paging.get("more_available", False)

    return next_max_id, has_next


def _fetch_paginated(
    username: str,
    max_pages: int,
    label: str,
    get_edges: Callable[[dict], list],
    parse_edge: Callable[[dict, set], dict | None],
) -> list[dict]:
    """Generic paginated fetcher for posts or reels.

    *get_edges* extracts the edge list from the API result dict.
    *parse_edge* converts one edge into a record dict, or None to skip.
    """
    api_key = os.getenv("RAPID_API_KEY")
    if not api_key:
        raise RuntimeError("RAPID_API_KEY not set in .env")

    items: list[dict] = []
    max_id = ""
    seen_codes: set[str] = set()

    for page in range(max_pages):
        logger.info("Fetching %s page %d for @%s...", label, page + 1, username)
        payload = json.dumps({"username": username, "maxId": max_id})
        resp = _api_request(payload, api_key, label)

        data = resp.json()
        result = data.get("result", {})
        edges = get_edges(result)
        if not edges:
            break

        new_count = 0
        for edge in edges:
            record = parse_edge(edge, seen_codes)
            if record:
                items.append(record)
                new_count += 1

        logger.info("  Page %d: %d new %s (total: %d)", page + 1, new_count, label, len(items))

        next_max_id, has_next = _extract_next_cursor(result)
        if not next_max_id or not has_next:
            break
        max_id = next_max_id
        time.sleep(2)

    return items


# ---------------------------------------------------------------------------
# Edge parsers
# ---------------------------------------------------------------------------

def _parse_post_edge(edge: dict, seen_codes: set[str]) -> dict | None:
    node = edge.get("node", {})
    media_type = node.get("media_type")
    code = node.get("code", "")

    if code in seen_codes or media_type not in (1, 8):
        return None
    seen_codes.add(code)

    caption_text = (node.get("caption") or {}).get("text", "")

    candidates = node.get("image_versions2", {}).get("candidates", [])
    if media_type == 8:
        carousel_items = node.get("carousel_media", []) or []
        if carousel_items:
            candidates = (
                carousel_items[0].get("image_versions2", {}).get("candidates", [])
                or candidates
            )

    if not candidates:
        return None
    best = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
    best_url = best.get("url", "")
    if not best_url:
        return None

    return {
        "caption": caption_text,
        "image_url": best_url,
        "like_count": node.get("like_count", 0),
        "media_type": media_type,
        "code": code,
        "taken_at": node.get("taken_at", 0),
    }


def _parse_reel_edge(edge: dict, seen_codes: set[str]) -> dict | None:
    node = edge.get("node", edge) if isinstance(edge, dict) else edge
    media = node.get("media", node)
    code = media.get("code", "")
    if not code or code in seen_codes:
        return None
    seen_codes.add(code)

    video_versions = media.get("video_versions", [])
    if video_versions:
        best_video = max(video_versions, key=lambda v: v.get("width", 0) * v.get("height", 0))
        video_url = best_video.get("url", "")
    else:
        video_url = f"https://www.instagram.com/reel/{code}/"

    caption_text = (media.get("caption") or {}).get("text", "")

    thumb_candidates = media.get("image_versions2", {}).get("candidates", [])
    thumbnail_url = ""
    if thumb_candidates:
        best_thumb = max(thumb_candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
        thumbnail_url = best_thumb.get("url", "")

    return {
        "code": code,
        "video_url": video_url,
        "thumbnail_url": thumbnail_url,
        "caption": caption_text,
        "like_count": media.get("like_count", 0),
        "taken_at": media.get("taken_at", 0),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_all_blogger_posts(username: str, max_pages: int = 20) -> list[dict]:
    """Paginate through RapidAPI Instagram posts and return image/carousel posts sorted chronologically."""
    posts = _fetch_paginated(
        username, max_pages, "posts",
        get_edges=lambda r: r.get("edges", []),
        parse_edge=_parse_post_edge,
    )
    posts.sort(key=lambda p: p.get("taken_at", 0))
    logger.info("Total posts for @%s: %d (chronological)", username, len(posts))
    return posts


def fetch_blogger_reels(username: str, max_pages: int = 3) -> list[dict]:
    """Fetch reels for a blogger via RapidAPI, sorted by recency."""
    reels = _fetch_paginated(
        username, max_pages, "reels",
        get_edges=lambda r: r.get("edges", r.get("items", [])),
        parse_edge=_parse_reel_edge,
    )
    reels.sort(key=lambda r: r.get("taken_at", 0), reverse=True)
    logger.info("Total reels for @%s: %d", username, len(reels))
    return reels


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def cache_posts(model_name: str, posts: list[dict]) -> str:
    """Save fetched posts to disk cache. Returns the cache file path."""
    POSTS_CACHE_DIR.mkdir(exist_ok=True)
    cache_file = POSTS_CACHE_DIR / f"{model_name}_posts.json"
    with open(cache_file, "w") as f:
        json.dump(posts, f, indent=2, ensure_ascii=False)
    logger.info("Cached %d posts to %s", len(posts), cache_file)
    return str(cache_file)


def load_cached_posts(cache_file: str) -> list[dict] | None:
    """Load posts from a cache file, or None if missing."""
    path = Path(cache_file)
    if path.exists():
        with open(path) as f:
            posts = json.load(f)
        logger.info("Loaded %d cached posts from %s", len(posts), cache_file)
        return posts
    return None


def cache_reels(model_name: str, reels: list[dict]) -> str:
    """Save fetched reels to disk cache. Returns the cache file path."""
    POSTS_CACHE_DIR.mkdir(exist_ok=True)
    cache_file = POSTS_CACHE_DIR / f"{model_name}_reels.json"
    with open(cache_file, "w") as f:
        json.dump(reels, f, indent=2, ensure_ascii=False)
    logger.info("Cached %d reels to %s", len(reels), cache_file)
    return str(cache_file)


def load_cached_reels(cache_file: str) -> list[dict] | None:
    """Load reels from a cache file, or None if missing."""
    path = Path(cache_file)
    if path.exists():
        with open(path) as f:
            reels = json.load(f)
        logger.info("Loaded %d cached reels from %s", len(reels), cache_file)
        return reels
    return None
