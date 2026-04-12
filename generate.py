#!/usr/bin/env python3
"""
Bloggers Factory - AI Instagram Carousel Generator.

Unified CLI that replaces generate_posts.py, generate_bulk_posts.py,
and generate_bulk_parallel.py.

Usage:
    python generate.py --model Andrea                          # single carousel
    python generate.py --bulk --model Andrea --min-carousels 60   # bulk sequential
    python generate.py --bulk --parallel --workers 4              # bulk parallel
    python generate.py --status                                   # show progress
    python generate.py --reset --model Andrea                     # reset one model
    python generate.py --reset                                    # reset all
"""

import argparse
import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

from lib.utils import setup_logging
from lib.state import State, RefCache
from lib.instagram import fetch_all_blogger_posts, cache_posts, load_cached_posts
from lib.prompts import generate_prompts
from lib.image_gen import (
    ensure_fal_key,
    get_reference_image_urls,
    generate_carousel_images,
    download_images,
    save_metadata,
)

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bloggers Factory - AI Carousel Generator")
    parser.add_argument("--model", type=str, help="Run for a single model")
    parser.add_argument("--bulk", action="store_true", help="Bulk mode (multiple carousels with resume)")
    parser.add_argument("--parallel", action="store_true", help="Run models in parallel (bulk mode)")
    parser.add_argument("--workers", type=int, default=4, help="Parallel model workers (default: 4)")
    parser.add_argument("--min-carousels", type=int, default=DEFAULT_TARGET,
                        help=f"Target carousels per model (default: {DEFAULT_TARGET})")
    parser.add_argument("--config", type=str, default="config.json", help="Config file path")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--status", action="store_true", help="Show progress and exit")
    parser.add_argument("--reset", action="store_true", help="Reset state (use --model for single)")
    parser.add_argument("--cron", action="store_true", help="Run single carousel for all models")
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
