#!/usr/bin/env python3
"""
Bloggers Factory - AI Instagram Carousel & Reel-to-Video Generator.

Unified CLI that replaces generate_posts.py, generate_bulk_posts.py,
and generate_bulk_parallel.py.

Usage:
    python generate.py --model Andrea                          # single carousel
    python generate.py --bulk --model Andrea --min-carousels 60   # bulk sequential
    python generate.py --bulk --parallel --workers 4              # bulk parallel
    python generate.py --status                                   # show progress
    python generate.py --reset --model Andrea                     # reset one model
    python generate.py --reset                                    # reset all
    python generate.py --reel --model Andrea                      # single reel-to-video
    python generate.py --reel --bulk --min-reels 10               # bulk reels, all models
    python generate.py --reel --bulk --parallel --workers 4       # bulk reels, parallel
"""

import argparse
import json
import logging
import random
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

from lib.utils import setup_logging, download_file
from lib.state import State, RefCache
from lib.instagram import (
    fetch_all_blogger_posts,
    fetch_blogger_reels,
    cache_posts,
    load_cached_posts,
    cache_reels,
    load_cached_reels,
)
from lib.prompts import generate_prompts
from lib.image_gen import (
    ensure_fal_key,
    get_reference_image_urls,
    generate_carousel_images,
    _generate_single_image,
    download_images,
    save_metadata,
)
from lib.video_utils import download_reel, extract_frames
from lib.reel_gen import (
    generate_scene_prompt,
    analyze_motion_with_vision,
    generate_kling_video,
    save_reel_metadata,
)
from lib.nanobanana_ugc_prompt import ugc_style_modifier

logger = logging.getLogger("bloggers_factory")

DEFAULT_TARGET = 60


def load_config(path: str = "config.json") -> dict:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Single-carousel mode (replaces generate_posts.py)
# ---------------------------------------------------------------------------

def run_single(model_name: str, config: dict):
    """Generate one carousel for a model (random inspiration post)."""
    ensure_fal_key()
    ref_cache = RefCache()

    model_cfg = config["models"][model_name]
    blogger = random.choice(model_cfg["bloggers"])
    ref_dir = model_cfg["ref_images_dir"]
    carousel_size = config.get("carousel_size", 5)
    aspect_ratio = config.get("aspect_ratio", "4:5")
    output_base = Path(model_cfg.get("output_dir", config.get("output_dir", "output")))

    logger.info("Single mode | Model: %s | Blogger: @%s", model_name, blogger)

    posts = fetch_all_blogger_posts(blogger, max_pages=1)
    if not posts:
        logger.error("No posts found for @%s", blogger)
        return

    top = posts[:5] if len(posts) >= 5 else posts
    inspiration = random.choice(top)
    logger.info("Picked inspiration post (code=%s, likes=%d)",
                inspiration["code"], inspiration["like_count"])

    prompt_result = generate_prompts(
        caption=inspiration["caption"],
        image_url=inspiration["image_url"],
        carousel_size=carousel_size,
    )
    if not prompt_result or not prompt_result.get("prompts"):
        logger.error("No prompts generated, aborting.")
        return

    ref_urls = get_reference_image_urls(model_name, ref_dir, ref_cache)
    results = generate_carousel_images(
        prompt_result["prompts"], ref_urls, aspect_ratio, model_name, parallel=True,
    )

    tag = datetime.now().strftime("%Y-%m-%d") + "_" + str(random.randint(1, 1_000_000))
    output_dir = output_base / model_name / tag
    generated = download_images(results, output_dir, parallel=True)

    if generated:
        save_metadata(output_dir, model_name, blogger, inspiration, prompt_result, generated, 1, 1)
    logger.info("Done: %d images -> %s", len(generated), output_dir)


# ---------------------------------------------------------------------------
# Bulk mode (replaces generate_bulk_posts.py / generate_bulk_parallel.py)
# ---------------------------------------------------------------------------

def generate_for_model(
    model_name: str,
    config: dict,
    state: State,
    ref_cache: RefCache,
    target: int,
    parallel: bool = True,
):
    """Generate up to `target` carousels for one model, resumable."""
    model_cfg = config["models"][model_name]
    blogger = model_cfg["bloggers"][0]
    ref_dir = model_cfg["ref_images_dir"]
    carousel_size = config.get("carousel_size", 5)
    aspect_ratio = config.get("aspect_ratio", "4:5")
    output_base = Path(model_cfg.get("output_dir", config.get("output_dir", "output")))

    ms = state.get_model(model_name)

    logger.info("=" * 60)
    logger.info("[%s] Blogger: @%s | Done: %d/%d", model_name, blogger, ms["completed_carousels"], target)
    logger.info("=" * 60)

    if ms["completed_carousels"] >= target:
        logger.info("[%s] Already at target, skipping.", model_name)
        return

    posts = None
    if ms.get("posts_cache_file"):
        posts = load_cached_posts(ms["posts_cache_file"])

    if not posts:
        posts = fetch_all_blogger_posts(blogger)
        if not posts:
            logger.error("[%s] No posts for @%s, skipping.", model_name, blogger)
            return
        cache_file = cache_posts(model_name, posts)
        state.update_and_save(model_name, posts_cache_file=cache_file, total_posts_fetched=len(posts))
        ms = state.get_model(model_name)

    ref_urls = get_reference_image_urls(model_name, ref_dir, ref_cache)

    completed_indices = set(ms.get("completed_post_indices", []))
    carousel_count = ms["completed_carousels"]
    total_posts = len(posts)

    logger.info("[%s] %d posts | Starting carousel #%d", model_name, total_posts, carousel_count + 1)

    cycle = 0
    while carousel_count < target:
        cycle += 1
        logger.info("[%s] CYCLE %d (%d more needed)", model_name, cycle, target - carousel_count)

        for post_idx, post in enumerate(posts):
            if carousel_count >= target:
                break

            composite_key = f"{cycle}_{post_idx}"
            if composite_key in completed_indices:
                continue

            carousel_num = carousel_count + 1
            logger.info("[%s] Carousel %d/%d | Cycle %d | Post %d/%d",
                        model_name, carousel_num, target, cycle, post_idx + 1, total_posts)

            prompt_result = generate_prompts(
                caption=post.get("caption", ""),
                image_url=post.get("image_url", ""),
                carousel_size=carousel_size,
            )

            if not prompt_result or not prompt_result.get("prompts"):
                logger.warning("[%s] No prompts for post %d, skipping", model_name, post_idx)
                completed_indices.add(composite_key)
                state.update_and_save(model_name, completed_post_indices=list(completed_indices))
                continue

            logger.info("[%s] Theme: %s | Generating %d images...",
                        model_name, prompt_result.get("theme", ""), len(prompt_result["prompts"]))

            results = generate_carousel_images(
                prompt_result["prompts"], ref_urls, aspect_ratio, model_name, parallel=parallel,
            )

            date_tag = datetime.now().strftime("%Y-%m-%d")
            dir_name = f"{date_tag}_carousel_{carousel_num:03d}"
            output_dir = output_base / model_name / dir_name
            generated_files = download_images(results, output_dir, parallel=parallel)

            if generated_files:
                save_metadata(
                    output_dir, model_name, blogger, post,
                    prompt_result, generated_files, carousel_num, cycle,
                )
                carousel_count += 1
                logger.info("[%s] SAVED carousel %d -> %s (%d images)",
                            model_name, carousel_num, output_dir, len(generated_files))
            else:
                logger.warning("[%s] No images for carousel %d", model_name, carousel_num)

            completed_indices.add(composite_key)
            state.update_and_save(
                model_name,
                completed_carousels=carousel_count,
                completed_post_indices=list(completed_indices),
            )

    logger.info("[%s] COMPLETED - %d carousels", model_name, carousel_count)


# ---------------------------------------------------------------------------
# Reel-to-video mode
# ---------------------------------------------------------------------------

def _load_master_prompt(path: str = "reel_master_prompt.json") -> str:
    with open(path) as f:
        data = json.load(f)
    return data["master_prompt"]


def run_reel(
    model_name: str,
    config: dict,
    args: argparse.Namespace,
    reel_data: dict | None = None,
    shared_ref_cache: RefCache | None = None,
) -> bool:
    """Generate a Kling video inspired by a blogger's reel.

    Returns True on success, False on failure.
    When *reel_data* is supplied (keys: video_url, code, like_count) the reel
    is used directly instead of fetching from Instagram or --reel-source.
    """
    ensure_fal_key()
    ref_cache = shared_ref_cache or RefCache()

    model_cfg = config["models"][model_name]
    blogger = random.choice(model_cfg["bloggers"])
    ref_dir = model_cfg["ref_images_dir"]
    output_base = Path(model_cfg.get("output_dir", config.get("output_dir", "output")))
    aspect_ratio = "9:16"
    duration = args.duration
    vision_model = args.vision_model

    logger.info("=" * 60)
    logger.info("REEL-TO-VIDEO | Model: %s | Blogger: @%s", model_name, blogger)
    logger.info("=" * 60)

    # --- 1. Resolve reel source ---
    reel_source: str | None = None
    reel_code = ""

    if reel_data:
        reel_source = reel_data["video_url"]
        reel_code = reel_data.get("code", "")
        logger.info("Using pre-fetched reel (code=%s)", reel_code)
    else:
        reel_source = getattr(args, "reel_source", None)

    tag = datetime.now().strftime("%Y-%m-%d") + "_" + str(random.randint(1, 1_000_000))
    output_dir = output_base / model_name / f"reel_{tag}"
    intermediate_dir = output_dir / "intermediate_data"
    intermediate_dir.mkdir(parents=True, exist_ok=True)

    def _cleanup_on_failure():
        if output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
            logger.info("Cleaned up failed reel dir: %s", output_dir)

    if reel_source:
        logger.info("Using reel source: %s", reel_source)
    else:
        logger.info("Fetching reels for @%s...", blogger)
        reels = fetch_blogger_reels(blogger, max_pages=1)
        if not reels:
            logger.error("No reels found for @%s", blogger)
            _cleanup_on_failure()
            return False
        reel = random.choice(reels[:5] if len(reels) >= 5 else reels)
        reel_source = reel["video_url"]
        reel_code = reel["code"]
        logger.info("Picked reel (code=%s, likes=%d)", reel_code, reel.get("like_count", 0))

    # --- 2. Download reel ---
    video_path = download_reel(reel_source, intermediate_dir)
    logger.info("Reel downloaded: %s", video_path)

    # --- 3. Extract frames ---
    frames = extract_frames(video_path, num_frames=3, output_dir=intermediate_dir)
    if not frames:
        logger.error("No frames extracted, aborting.")
        _cleanup_on_failure()
        return False
    logger.info("Extracted %d frames", len(frames))

    # --- 4. Generate scene prompt from first frame ---
    scene_prompt = generate_scene_prompt(frames[0])
    if not scene_prompt:
        logger.error("Failed to generate scene prompt, aborting.")
        _cleanup_on_failure()
        return False
    logger.info("Scene prompt: %s", scene_prompt[:120])

    # --- 5. Generate Nano Banana image (identity-preserving scene recreation) ---
    ref_urls = get_reference_image_urls(model_name, ref_dir, ref_cache)
    ugc_prompt = ugc_style_modifier(scene_prompt)
    logger.info("Generating Nano Banana image (UGC scene recreation + identity)...")
    _, nb_result = _generate_single_image(0, ugc_prompt, ref_urls, aspect_ratio, model_name)
    nb_images = nb_result.get("images", [])
    if not nb_images:
        logger.error("Nano Banana returned no image, aborting.")
        _cleanup_on_failure()
        return False

    nb_image_url = nb_images[0]["url"]
    nb_dest = intermediate_dir / "generated_image.png"
    download_file(nb_image_url, nb_dest)
    logger.info("Nano Banana image saved: %s", nb_dest)

    # --- 6. Vision-based motion analysis ---
    master_prompt = _load_master_prompt()
    logger.info("Running vision analysis with %s...", vision_model)
    vision_result = analyze_motion_with_vision(frames, master_prompt, vision_model)
    if not vision_result:
        logger.error("Vision analysis failed, aborting.")
        _cleanup_on_failure()
        return False
    logger.info("Kling prompt: %s", vision_result.get("kling_prompt", "")[:120])

    # --- 7. Generate Kling video ---
    kling_prompt = vision_result.get("kling_prompt", "")
    negative_prompt = vision_result.get("negative_prompt", "")
    if not kling_prompt:
        logger.error("Vision analysis produced no kling_prompt, aborting.")
        _cleanup_on_failure()
        return False

    kling_result = generate_kling_video(
        image_url=nb_image_url,
        prompt=kling_prompt,
        negative_prompt=negative_prompt,
        duration=duration,
    )
    if not kling_result:
        logger.error("Kling video generation failed, aborting.")
        _cleanup_on_failure()
        return False

    # --- 8. Download output video ---
    video_url = kling_result.get("video", {}).get("url", "")
    if not video_url:
        logger.error("Kling returned no video URL.")
        _cleanup_on_failure()
        return False

    video_filename = f"{reel_code}.mp4" if reel_code else "output_video.mp4"
    video_dest = output_dir / video_filename
    if not download_file(video_url, video_dest, timeout=120):
        logger.error("Failed to download output video.")
        _cleanup_on_failure()
        return False
    logger.info("Output video saved: %s", video_dest)

    # --- 9. Save metadata ---
    generated_files = [video_dest.name, nb_dest.name] + [f.name for f in frames]
    save_reel_metadata(
        output_dir=intermediate_dir,
        model_name=model_name,
        blogger=blogger,
        reel_source=reel_source,
        reel_code=reel_code,
        scene_prompt=scene_prompt,
        vision_result=vision_result,
        generated_files=generated_files,
        duration=duration,
        vision_model=vision_model,
    )

    logger.info("=" * 60)
    logger.info("REEL-TO-VIDEO COMPLETE -> %s", output_dir)
    logger.info("=" * 60)
    return True


# ---------------------------------------------------------------------------
# Bulk reel mode
# ---------------------------------------------------------------------------

def generate_reels_for_model(
    model_name: str,
    config: dict,
    state: State,
    ref_cache: RefCache,
    target: int,
    args: argparse.Namespace,
):
    """Generate up to *target* reels for one model, resumable."""
    model_cfg = config["models"][model_name]
    blogger = model_cfg["bloggers"][0]

    ms = state.get_model(model_name)

    logger.info("=" * 60)
    logger.info("[%s] BULK REELS | Blogger: @%s | Done: %d/%d",
                model_name, blogger, ms.get("completed_reels", 0), target)
    logger.info("=" * 60)

    if ms.get("completed_reels", 0) >= target:
        logger.info("[%s] Already at reel target, skipping.", model_name)
        return

    reels = None
    if ms.get("reels_cache_file"):
        reels = load_cached_reels(ms["reels_cache_file"])

    if not reels:
        reels = fetch_blogger_reels(blogger)
        if not reels:
            logger.error("[%s] No reels for @%s, skipping.", model_name, blogger)
            return
        cache_file = cache_reels(model_name, reels)
        state.update_and_save(model_name, reels_cache_file=cache_file)
        ms = state.get_model(model_name)

    completed_indices = set(ms.get("completed_reel_indices", []))
    reel_count = ms.get("completed_reels", 0)
    total_reels = len(reels)

    logger.info("[%s] %d source reels | Starting reel #%d", model_name, total_reels, reel_count + 1)

    max_consecutive_failures = total_reels * 2
    consecutive_failures = 0

    cycle = 0
    while reel_count < target:
        cycle += 1
        logger.info("[%s] CYCLE %d (%d more needed)", model_name, cycle, target - reel_count)

        cycle_had_success = False
        for reel_idx, reel in enumerate(reels):
            if reel_count >= target:
                break

            composite_key = f"{cycle}_{reel_idx}"
            if composite_key in completed_indices:
                continue

            reel_num = reel_count + 1
            logger.info("[%s] Reel %d/%d | Cycle %d | Source reel %d/%d (code=%s)",
                        model_name, reel_num, target, cycle, reel_idx + 1, total_reels,
                        reel.get("code", ""))

            try:
                success = run_reel(
                    model_name, config, args,
                    reel_data=reel,
                    shared_ref_cache=ref_cache,
                )
            except Exception:
                logger.exception("[%s] run_reel failed for reel %d", model_name, reel_num)
                success = False

            if success:
                reel_count += 1
                consecutive_failures = 0
                cycle_had_success = True
                logger.info("[%s] COMPLETED reel %d/%d", model_name, reel_count, target)
            else:
                consecutive_failures += 1
                logger.warning("[%s] Reel %d failed (%d consecutive failures)",
                               model_name, reel_num, consecutive_failures)

            completed_indices.add(composite_key)
            state.update_and_save(
                model_name,
                completed_reels=reel_count,
                completed_reel_indices=list(completed_indices),
            )

            if consecutive_failures >= max_consecutive_failures:
                logger.error("[%s] Too many consecutive failures (%d), aborting.",
                             model_name, consecutive_failures)
                break

        if consecutive_failures >= max_consecutive_failures:
            break
        if not cycle_had_success:
            logger.error("[%s] Entire cycle %d had no successes, aborting to avoid infinite loop.",
                         model_name, cycle)
            break

    logger.info("[%s] BULK REELS DONE - %d/%d reels completed", model_name, reel_count, target)


# ---------------------------------------------------------------------------
# Progress display
# ---------------------------------------------------------------------------

def print_progress(state: State, config: dict, target: int):
    logger.info("")
    logger.info("=" * 70)
    logger.info("PROGRESS SUMMARY")
    logger.info("=" * 70)
    logger.info("%-15s %-20s %10s %10s %8s", "Model", "Blogger", "Done", "Target", "Status")
    logger.info("-" * 70)

    total_done = total_target = 0
    for model_name, model_cfg in config["models"].items():
        ms = state.data.get(model_name, {})
        done = ms.get("completed_carousels", 0)
        blogger = model_cfg["bloggers"][0]
        status = "DONE" if done >= target else f"{done}/{target}"
        logger.info("%-15s %-20s %10d %10d %8s", model_name, blogger, done, target, status)
        total_done += done
        total_target += target

    logger.info("-" * 70)
    logger.info("%-15s %-20s %10d %10d %8s", "TOTAL", "", total_done, total_target,
                "DONE" if total_done >= total_target else f"{total_done}/{total_target}")
    logger.info("=" * 70)


def print_reel_progress(state: State, config: dict, target: int):
    logger.info("")
    logger.info("=" * 70)
    logger.info("REEL PROGRESS SUMMARY")
    logger.info("=" * 70)
    logger.info("%-15s %-20s %10s %10s %8s", "Model", "Blogger", "Done", "Target", "Status")
    logger.info("-" * 70)

    total_done = total_target = 0
    for model_name, model_cfg in config["models"].items():
        ms = state.data.get(model_name, {})
        done = ms.get("completed_reels", 0)
        blogger = model_cfg["bloggers"][0]
        status = "DONE" if done >= target else f"{done}/{target}"
        logger.info("%-15s %-20s %10d %10d %8s", model_name, blogger, done, target, status)
        total_done += done
        total_target += target

    logger.info("-" * 70)
    logger.info("%-15s %-20s %10d %10d %8s", "TOTAL", "", total_done, total_target,
                "DONE" if total_done >= total_target else f"{total_done}/{total_target}")
    logger.info("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bloggers Factory - AI Carousel & Reel-to-Video Generator")
    parser.add_argument("--model", type=str, help="Run for a single model")
    parser.add_argument("--bulk", action="store_true", help="Bulk mode (multiple carousels with resume)")
    parser.add_argument("--parallel", action="store_true", help="Run models in parallel (bulk mode)")
    parser.add_argument("--workers", type=int, default=4, help="Parallel model workers (default: 4)")
    parser.add_argument("--min-carousels", type=int, default=DEFAULT_TARGET,
                        help=f"Target carousels per model (default: {DEFAULT_TARGET})")
    parser.add_argument("--min-reels", type=int, default=10,
                        help="Target reels per model in bulk reel mode (default: 10)")
    parser.add_argument("--config", type=str, default="config.json", help="Config file path")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--status", action="store_true", help="Show progress and exit")
    parser.add_argument("--reset", action="store_true", help="Reset state (use --model for single)")
    parser.add_argument("--cron", action="store_true", help="Run single carousel for all models")
    parser.add_argument("--reel", action="store_true", help="Reel-to-video mode")
    parser.add_argument("--reel-source", type=str, default=None,
                        help="Direct reel path/URL (omit to fetch from blogger)")
    parser.add_argument("--duration", type=int, default=5,
                        help="Kling video duration in seconds (default: 5)")
    parser.add_argument("--vision-model", type=str, default="google/gemini-2.5-flash",
                        help="Vision model for motion analysis (default: google/gemini-2.5-flash)")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose, parallel=args.parallel)
    config = load_config(args.config)
    state = State()
    state.load()

    target = args.min_carousels

    if args.status:
        print_progress(state, config, target)
        return

    if args.reset:
        if args.model:
            state.reset(args.model)
            logger.info("Reset state for %s.", args.model)
        else:
            state.reset()
            logger.info("Reset all state.")
        return

    if args.model and args.model not in config["models"]:
        logger.error("Model '%s' not in config. Available: %s", args.model, list(config["models"].keys()))
        return

    ensure_fal_key()

    # --- Reel-to-video mode ---
    if args.reel:
        if args.bulk:
            reel_target = args.min_reels
            models_to_run = [args.model] if args.model else list(config["models"].keys())
            ref_cache = RefCache()

            logger.info("Bulk Reel Generator | Models: %s | Target: %d/model | Parallel: %s",
                        models_to_run, reel_target, args.parallel)
            print_reel_progress(state, config, reel_target)

            start_time = time.time()

            if args.parallel:
                num_workers = min(args.workers, len(models_to_run))
                with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix="reel") as executor:
                    futures = {
                        executor.submit(
                            generate_reels_for_model, name, config, state, ref_cache, reel_target, args
                        ): name
                        for name in models_to_run if name in config["models"]
                    }
                    for future in as_completed(futures):
                        name = futures[future]
                        try:
                            future.result()
                            logger.info("[%s] Reel worker finished", name)
                        except Exception:
                            logger.exception("[%s] Reel worker FAILED", name)
            else:
                for name in models_to_run:
                    try:
                        generate_reels_for_model(name, config, state, ref_cache, reel_target, args)
                    except Exception:
                        logger.exception("FATAL reel for %s - continuing", name)
                    print_reel_progress(state, config, reel_target)

            elapsed = time.time() - start_time
            h, rem = divmod(elapsed, 3600)
            m, s = divmod(rem, 60)

            logger.info("")
            logger.info("=" * 70)
            logger.info("BULK REELS DONE | Time: %dh %dm %ds", h, m, s)
            logger.info("=" * 70)
            print_reel_progress(state, config, reel_target)
            return

        if not args.model:
            parser.error("--model is required in single reel mode (or use --reel --bulk)")
        run_reel(args.model, config, args)
        return

    # --- Single mode (one carousel per model) ---
    if not args.bulk and not args.cron:
        if not args.model:
            parser.error("--model is required in single mode (or use --bulk / --cron)")
        run_single(args.model, config)
        return

    # --- Cron mode (one carousel per model, all models) ---
    if args.cron:
        for model_name in config["models"]:
            try:
                run_single(model_name, config)
            except Exception:
                logger.exception("Failed for model %s", model_name)
        return

    # --- Bulk mode ---
    models_to_run = [args.model] if args.model else list(config["models"].keys())
    ref_cache = RefCache()

    logger.info("Bulk Generator | Models: %s | Target: %d/model | Parallel: %s",
                models_to_run, target, args.parallel)
    print_progress(state, config, target)

    start_time = time.time()

    if args.parallel:
        num_workers = min(args.workers, len(models_to_run))
        with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix="model") as executor:
            futures = {
                executor.submit(
                    generate_for_model, name, config, state, ref_cache, target, True
                ): name
                for name in models_to_run if name in config["models"]
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                    logger.info("[%s] Worker finished", name)
                except Exception:
                    logger.exception("[%s] Worker FAILED", name)
    else:
        for name in models_to_run:
            try:
                generate_for_model(name, config, state, ref_cache, target, False)
            except Exception:
                logger.exception("FATAL for %s - continuing", name)
            print_progress(state, config, target)

    elapsed = time.time() - start_time
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)

    logger.info("")
    logger.info("=" * 70)
    logger.info("ALL DONE | Time: %dh %dm %ds", h, m, s)
    logger.info("=" * 70)
    print_progress(state, config, target)


if __name__ == "__main__":
    main()
