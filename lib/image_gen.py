import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import fal_client

from .state import RefCache
from .utils import download_file

logger = logging.getLogger("bloggers_factory")


def ensure_fal_key():
    key = os.getenv("FAL_AI_API_KEY", "")
    if not key:
        raise RuntimeError("FAL_AI_API_KEY not set in .env")
    os.environ["FAL_KEY"] = key


# ---------------------------------------------------------------------------
# Reference image upload
# ---------------------------------------------------------------------------

def get_reference_image_urls(
    model_name: str, ref_dir: str, ref_cache: RefCache, max_images: int = 3,
) -> list[str]:
    """Upload reference face images to fal.ai (cached across runs)."""
    cached = ref_cache.get(model_name)
    if cached:
        logger.info("Using %d cached ref URLs for %s", len(cached), model_name)
        return cached

    ref_path = Path(ref_dir)
    if not ref_path.exists():
        raise RuntimeError(f"Reference image directory not found: {ref_dir}")

    image_files = sorted(
        [f for f in ref_path.iterdir() if f.suffix.lower() in (".jpg", ".jpeg", ".png")],
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    if not image_files:
        raise RuntimeError(f"No image files found in {ref_dir}")

    jpg_files = [f for f in image_files if f.suffix.lower() in (".jpg", ".jpeg")]
    png_files = [f for f in image_files if f.suffix.lower() == ".png"]

    selected: list[Path] = []
    if jpg_files:
        selected.append(jpg_files[0])
    for png in png_files:
        if len(selected) >= max_images:
            break
        selected.append(png)
    while len(selected) < max_images and len(selected) < len(image_files):
        for f in image_files:
            if f not in selected:
                selected.append(f)
                break

    logger.info("Uploading %d ref images for %s...", len(selected), model_name)
    urls = []
    for img_file in selected:
        url = fal_client.upload_file(str(img_file))
        urls.append(url)

    ref_cache.set(model_name, urls)
    logger.info("Cached %d ref URLs for %s", len(urls), model_name)
    return urls


# ---------------------------------------------------------------------------
# Carousel image generation (sequential or parallel)
# ---------------------------------------------------------------------------

def _generate_single_image(
    prompt_idx: int,
    prompt: str,
    ref_image_urls: list[str],
    aspect_ratio: str,
    model_name: str,
) -> tuple[int, dict]:
    for attempt in range(3):
        try:
            result = fal_client.subscribe(
                "fal-ai/nano-banana-2/edit",
                arguments={
                    "prompt": prompt,
                    "image_urls": ref_image_urls,
                    "num_images": 1,
                    "aspect_ratio": aspect_ratio,
                    "output_format": "png",
                    "resolution": "1K",
                    "safety_tolerance": "6",
                },
                with_logs=False,
            )
            images = result.get("images", [])
            if images:
                logger.info("  [%s] Image %d generated", model_name, prompt_idx + 1)
            else:
                logger.warning("  [%s] Image %d: no image returned", model_name, prompt_idx + 1)
            return (prompt_idx, result)

        except Exception as e:
            logger.warning("  [%s] Image %d failed (attempt %d/3): %s",
                           model_name, prompt_idx + 1, attempt + 1, e)
            if attempt < 2:
                time.sleep(10 * (attempt + 1))

    return (prompt_idx, {"images": [], "error": "all attempts failed"})


def generate_carousel_images(
    prompts: list[str],
    ref_image_urls: list[str],
    aspect_ratio: str,
    model_name: str,
    parallel: bool = True,
) -> list[dict]:
    """Generate one image per prompt via fal.ai Nano Banana 2."""
    if parallel:
        results: list[dict | None] = [None] * len(prompts)
        with ThreadPoolExecutor(max_workers=len(prompts), thread_name_prefix=f"img-{model_name}") as ex:
            futures = {
                ex.submit(_generate_single_image, i, p, ref_image_urls, aspect_ratio, model_name): i
                for i, p in enumerate(prompts)
            }
            for future in as_completed(futures):
                idx, result = future.result()
                results[idx] = result
        return results  # type: ignore[return-value]
    else:
        results_seq = []
        for i, prompt in enumerate(prompts):
            _, result = _generate_single_image(i, prompt, ref_image_urls, aspect_ratio, model_name)
            results_seq.append(result)
        return results_seq


# ---------------------------------------------------------------------------
# Download generated images
# ---------------------------------------------------------------------------

def download_images(
    results: list[dict], output_dir: Path, parallel: bool = True,
) -> list[Path]:
    """Download generated image URLs to output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)

    items = []
    for i, result in enumerate(results, 1):
        images = result.get("images", [])
        if images:
            items.append((i, images[0]["url"]))

    if not items:
        return []

    if parallel:
        downloaded: list[Path] = []
        with ThreadPoolExecutor(max_workers=len(items), thread_name_prefix="dl") as ex:
            futures = {}
            for idx, url in items:
                dest = output_dir / f"image_{idx}.png"
                futures[ex.submit(download_file, url, dest)] = dest
            for future in as_completed(futures):
                if future.result():
                    downloaded.append(futures[future])
        downloaded.sort(key=lambda p: p.name)
        return downloaded
    else:
        downloaded_seq: list[Path] = []
        for idx, url in items:
            dest = output_dir / f"image_{idx}.png"
            if download_file(url, dest):
                downloaded_seq.append(dest)
        return downloaded_seq


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def save_metadata(
    output_dir: Path,
    model_name: str,
    blogger: str,
    inspiration_post: dict,
    prompt_result: dict,
    generated_files: list[Path],
    carousel_index: int,
    cycle_number: int,
):
    meta = {
        "model": model_name,
        "blogger_source": blogger,
        "inspiration_post_code": inspiration_post.get("code", ""),
        "inspiration_caption": inspiration_post.get("caption", ""),
        "inspiration_taken_at": inspiration_post.get("taken_at", 0),
        "theme": prompt_result.get("theme", ""),
        "prompts": prompt_result.get("prompts", []),
        "generated_files": [f.name for f in generated_files],
        "generated_at": datetime.now().isoformat(),
        "carousel_index": carousel_index,
        "cycle_number": cycle_number,
    }
    meta_file = output_dir / "metadata.json"
    with open(meta_file, "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
