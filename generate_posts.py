import argparse
import base64
import json
import logging
import os
import random
import sys
from datetime import datetime
from pathlib import Path

import fal_client
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(override=True)

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("glam")


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def load_config() -> dict:
    with open("config.json") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Stage A: Fetch blogger posts via RapidAPI
# ---------------------------------------------------------------------------

def fetch_blogger_posts(username: str) -> list[dict]:
    """Fetch recent posts for a blogger and return image-only posts sorted by likes."""
    api_key = os.getenv("RAPID_API_KEY")
    if not api_key:
        raise RuntimeError("RAPID_API_KEY not set in .env")

    logger.info("Fetching posts for @%s via RapidAPI...", username)

    resp = requests.post(
        "https://instagram120.p.rapidapi.com/api/instagram/posts",
        json={"username": username, "maxId": ""},
        headers={
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": "instagram120.p.rapidapi.com",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    edges = data.get("result", {}).get("edges", [])
    image_posts = []

    for edge in edges:
        node = edge.get("node", {})
        media_type = node.get("media_type")

        # media_type 1 = single photo, 8 = carousel
        if media_type not in (1, 8):
            continue

        caption_obj = node.get("caption") or {}
        caption_text = caption_obj.get("text", "")
        like_count = node.get("like_count", 0)

        # Get the best image URL
        candidates = (
            node.get("image_versions2", {}).get("candidates", [])
        )
        if media_type == 8:
            carousel_items = node.get("carousel_media", []) or []
            first_item = carousel_items[0] if carousel_items else {}
            candidates = (
                first_item.get("image_versions2", {}).get("candidates", [])
                or candidates
            )

        best_image_url = ""
        if candidates:
            best = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
            best_image_url = best.get("url", "")

        if not best_image_url:
            continue

        image_posts.append({
            "caption": caption_text,
            "image_url": best_image_url,
            "like_count": like_count,
            "media_type": media_type,
            "code": node.get("code", ""),
        })

    image_posts.sort(key=lambda p: p["like_count"], reverse=True)
    logger.info("Found %d image posts for @%s", len(image_posts), username)
    return image_posts


def pick_inspiration_post(posts: list[dict]) -> dict:
    """Pick a random post from the top 5 most liked image posts."""
    top = posts[:5] if len(posts) >= 5 else posts
    if not top:
        raise RuntimeError("No image posts found to use as inspiration")
    chosen = random.choice(top)
    logger.info(
        "Picked inspiration post (code=%s, likes=%d): %s",
        chosen["code"],
        chosen["like_count"],
        chosen["caption"][:80],
    )
    return chosen


# ---------------------------------------------------------------------------
# Stage B: Generate prompts via OpenAI GPT-4o
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert Instagram content strategist and AI image prompt engineer.

You will receive a real Instagram blogger's post (caption + image). Your job is to:
1. Analyze the scene, setting, pose, outfit, lighting, mood, and aesthetic from the image and caption.
2. Generate exactly {carousel_size} distinct image generation prompts for Nano Banana 2 (a text-to-image model that takes reference face images).
3. Each prompt should describe a photorealistic scene that recreates a similar vibe/aesthetic but with enough variety for an Instagram carousel post.
4. Prompts should describe the woman's pose, outfit, setting, lighting, and mood in detail.
5. Do NOT mention any names or usernames. Describe the person generically (e.g., "a young woman", "the woman").
6. Vary angles, poses, and backgrounds across the 5 prompts while keeping a cohesive theme/aesthetic.
7. Each prompt should be 1-3 sentences, vivid and specific.
8. Use Instagram-worthy aesthetics: golden hour lighting, editorial fashion, lifestyle vibes.

Return ONLY valid JSON with this structure:
{{
  "theme": "brief theme description",
  "prompts": ["prompt 1", "prompt 2", "prompt 3", "prompt 4", "prompt 5"]
}}"""


def _download_image_as_base64(url: str) -> str:
    """Download an image from URL and return as a base64 data URI."""
    logger.info("Downloading blogger image for GPT-4o analysis...")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "image/jpeg")
    if ";" in content_type:
        content_type = content_type.split(";")[0].strip()
    b64 = base64.b64encode(resp.content).decode("utf-8")
    return f"data:{content_type};base64,{b64}"


def generate_prompts(
    caption: str, image_url: str, carousel_size: int = 5
) -> dict:
    """Use OpenAI GPT-4o to analyze a blogger post and generate scene prompts."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in .env")

    client = OpenAI(api_key=api_key)
    logger.info("Generating %d prompts via GPT-4o...", carousel_size)

    system = SYSTEM_PROMPT.format(carousel_size=carousel_size)

    # Instagram CDN URLs are temporary/restricted; download and send as base64
    image_data_uri = _download_image_as_base64(image_url)

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Instagram caption:\n{caption}\n\nAnalyze this post and generate {carousel_size} image prompts.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": image_data_uri, "detail": "high"},
                    },
                ],
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.9,
        max_tokens=1500,
    )

    raw = response.choices[0].message.content
    result = json.loads(raw)

    prompts = result.get("prompts", [])
    theme = result.get("theme", "")
    logger.info("Theme: %s", theme)
    for i, p in enumerate(prompts, 1):
        logger.info("  Prompt %d: %s", i, p[:100])

    return result


# ---------------------------------------------------------------------------
# Stage C: Upload AI model reference images to fal.ai
# ---------------------------------------------------------------------------

CACHE_FILE = Path("ref_urls_cache.json")


def load_ref_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_ref_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def get_reference_image_urls(model_name: str, ref_dir: str, max_images: int = 3) -> list[str]:
    """Upload reference images to fal.ai and return their URLs. Uses cache."""
    cache = load_ref_cache()
    cached = cache.get(model_name, [])
    if cached:
        logger.info("Using %d cached reference URLs for %s", len(cached), model_name)
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

    # Prefer the JPG file (usually highest quality) + a couple of PNGs
    jpg_files = [f for f in image_files if f.suffix.lower() in (".jpg", ".jpeg")]
    png_files = [f for f in image_files if f.suffix.lower() == ".png"]

    selected = []
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

    logger.info("Uploading %d reference images for %s...", len(selected), model_name)
    urls = []
    for img_file in selected:
        logger.info("  Uploading %s...", img_file.name)
        url = fal_client.upload_file(str(img_file))
        urls.append(url)
        logger.info("  -> %s", url)

    cache[model_name] = urls
    save_ref_cache(cache)
    logger.info("Cached %d reference URLs for %s", len(urls), model_name)
    return urls


# ---------------------------------------------------------------------------
# Stage D: Generate images via Nano Banana 2
# ---------------------------------------------------------------------------

def generate_carousel_images(
    prompts: list[str],
    ref_image_urls: list[str],
    aspect_ratio: str = "4:5",
) -> list[dict]:
    """Generate one image per prompt using Nano Banana 2. Returns list of result dicts."""

    results = []
    for i, prompt in enumerate(prompts, 1):
        logger.info("Generating image %d/%d...", i, len(prompts))
        logger.info("  Prompt: %s", prompt[:120])

        def on_queue_update(update):
            if isinstance(update, fal_client.InProgress):
                for log_entry in update.logs:
                    logger.debug("  fal.ai: %s", log_entry.get("message", ""))

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
            with_logs=True,
            on_queue_update=on_queue_update,
        )

        images = result.get("images", [])
        if images:
            logger.info("  Generated: %s", images[0].get("url", "")[:80])
        else:
            logger.warning("  No image returned for prompt %d", i)

        results.append(result)

    return results


def download_images(
    results: list[dict], output_dir: Path
) -> list[Path]:
    """Download generated images to the output directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    for i, result in enumerate(results, 1):
        images = result.get("images", [])
        if not images:
            continue

        url = images[0]["url"]
        filename = output_dir / f"image_{i}.png"

        logger.info("Downloading image %d -> %s", i, filename)
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        filename.write_bytes(resp.content)
        downloaded.append(filename)

    logger.info("Downloaded %d images to %s", len(downloaded), output_dir)
    return downloaded


def save_metadata(
    output_dir: Path,
    model_name: str,
    blogger: str,
    inspiration_post: dict,
    prompt_result: dict,
    generated_files: list[Path],
):
    """Save generation metadata alongside the images."""
    meta = {
        "model": model_name,
        "blogger_source": blogger,
        "inspiration_post_code": inspiration_post.get("code", ""),
        "inspiration_caption": inspiration_post.get("caption", ""),
        "theme": prompt_result.get("theme", ""),
        "prompts": prompt_result.get("prompts", []),
        "generated_files": [str(f.name) for f in generated_files],
        "generated_at": datetime.now().isoformat(),
    }
    meta_file = output_dir / "metadata.json"
    with open(meta_file, "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    logger.info("Saved metadata to %s", meta_file)


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------

def _ensure_fal_key():
    """Set FAL_KEY env var from FAL_AI_API_KEY for fal_client."""
    key = os.getenv("FAL_AI_API_KEY", "")
    if not key:
        raise RuntimeError("FAL_AI_API_KEY not set in .env")
    os.environ["FAL_KEY"] = key


def run_pipeline(model_name: str, config: dict):
    """Run the full pipeline for a single AI model."""
    _ensure_fal_key()

    models = config["models"]
    if model_name not in models:
        raise RuntimeError(
            f"Model '{model_name}' not found in config. Available: {list(models.keys())}"
        )

    model_cfg = models[model_name]
    bloggers = model_cfg["bloggers"]
    ref_dir = model_cfg["ref_images_dir"]
    carousel_size = config.get("carousel_size", 5)
    aspect_ratio = config.get("aspect_ratio", "4:5")
    output_base = Path(config.get("output_dir", "output"))

    logger.info("=" * 60)
    logger.info("Running pipeline for model: %s", model_name)
    logger.info("Assigned bloggers: %s", bloggers)
    logger.info("=" * 60)

    # Pick a random blogger
    blogger = random.choice(bloggers)
    logger.info("Selected blogger: @%s", blogger)

    # Stage A: Fetch posts
    posts = fetch_blogger_posts(blogger)
    if not posts:
        logger.error("No image posts found for @%s, skipping.", blogger)
        return

    inspiration = pick_inspiration_post(posts)

    # Stage B: Generate prompts
    prompt_result = generate_prompts(
        caption=inspiration["caption"],
        image_url=inspiration["image_url"],
        carousel_size=carousel_size,
    )
    prompts = prompt_result.get("prompts", [])
    if not prompts:
        logger.error("No prompts generated, skipping.")
        return

    # Stage C: Upload reference images
    ref_urls = get_reference_image_urls(model_name, ref_dir)

    # Stage D: Generate images
    results = generate_carousel_images(prompts, ref_urls, aspect_ratio)

    # Download and save
    today = datetime.now().strftime("%Y-%m-%d") + "_" + str(random.randint(1, 1000000))
    output_dir = output_base / model_name / today
    generated_files = download_images(results, output_dir)
    save_metadata(output_dir, model_name, blogger, inspiration, prompt_result, generated_files)

    logger.info("Pipeline complete for %s: %d images generated", model_name, len(generated_files))
    return generated_files


def main():
    parser = argparse.ArgumentParser(description="Glam Bloggers Factory - AI Image Carousel Generator")
    parser.add_argument("--model", type=str, help="AI model name (e.g., Andrea)")
    parser.add_argument("--cron", action="store_true", help="Run for all models (cron mode)")
    parser.add_argument("--test", action="store_true", help="Test mode (same as normal, just a label)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)
    config = load_config()

    if args.cron:
        logger.info("CRON MODE: Running pipeline for all models")
        for model_name in config["models"]:
            try:
                run_pipeline(model_name, config)
            except Exception:
                logger.exception("Failed for model %s", model_name)
    elif args.model:
        label = " (TEST)" if args.test else ""
        logger.info("Running pipeline for model: %s%s", args.model, label)
        run_pipeline(args.model, config)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
