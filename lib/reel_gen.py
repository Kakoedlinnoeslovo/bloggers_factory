import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import fal_client
from openai import OpenAI

from .nanobanana_ugc_prompt import UGC_SYSTEM_PROMPT, UGC_USER_SUFFIX
from .utils import download_file

logger = logging.getLogger("bloggers_factory")


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

SCENE_RECREATION_SYSTEM_PROMPT = UGC_SYSTEM_PROMPT

VISION_SYSTEM_PROMPT = """\
You are a video motion analyst creating prompts for image-to-video generation.
You will receive a small temporal sequence of frames from a short video and a master creative prompt.
Your task is to infer motion, camera movement, subject behavior, scene progression, and consistency constraints.
Do not simply caption each frame.
Output strict JSON only.
Focus on animation instructions that can be applied to a single generated start image.
Prefer concise, concrete motion language.
Do not mention things not visible or strongly implied by the frames."""

VISION_USER_TEMPLATE = """\
Master prompt:
{master_prompt}

Analyze these frames as a temporal sequence.

Return strict JSON with keys:
subject,
environment,
style,
action_progression,
camera_motion,
consistency_constraints,
kling_prompt,
negative_prompt

Requirements:
- subject: the main subject
- environment: scene/background
- style: visual style and mood
- action_progression: ordered temporal changes
- camera_motion: camera movement across frames
- consistency_constraints: elements that should remain stable
- kling_prompt: final prompt under 120 words for image-to-video from a single still image
- negative_prompt: concise unwanted artifacts or motion problems"""


# ---------------------------------------------------------------------------
# Scene description (GPT-4o) for Nano Banana prompt
# ---------------------------------------------------------------------------

def generate_scene_prompt(frame_path: Path) -> str | None:
    """Describe a reel frame for Nano Banana scene recreation."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in .env")

    client = OpenAI(api_key=api_key)

    frame_url = fal_client.upload_file(str(frame_path))
    logger.info("Uploaded frame for scene analysis: %s", frame_path.name)

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": SCENE_RECREATION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": UGC_USER_SUFFIX,
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": frame_url, "detail": "high"},
                            },
                        ],
                    },
                ],
                temperature=0.7,
                max_tokens=500,
            )
            prompt = response.choices[0].message.content.strip()
            logger.info("Scene prompt generated (%d chars)", len(prompt))
            return prompt

        except Exception as e:
            logger.warning("Scene prompt generation failed (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))

    return None


# ---------------------------------------------------------------------------
# Vision-based motion analysis (fal any-llm)
# ---------------------------------------------------------------------------

def analyze_motion_with_vision(
    frame_paths: list[Path],
    master_prompt: str,
    vision_model: str = "google/gemini-2.5-flash",
) -> dict | None:
    """Send frames to a vision model via fal.ai and get a structured motion analysis."""
    frame_urls = []
    for fp in frame_paths:
        url = fal_client.upload_file(str(fp))
        frame_urls.append(url)
        logger.info("Uploaded frame for vision: %s", fp.name)

    user_prompt = VISION_USER_TEMPLATE.format(master_prompt=master_prompt)

    for attempt in range(3):
        try:
            result = fal_client.subscribe(
                "openrouter/router/vision",
                arguments={
                    "model": vision_model,
                    "prompt": user_prompt,
                    "system_prompt": VISION_SYSTEM_PROMPT,
                    "image_urls": frame_urls,
                },
                with_logs=False,
            )

            raw = result.get("output", "")
            if not raw:
                raw = result.get("choices", [{}])[0].get("message", {}).get("content", "")

            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()

            parsed = json.loads(cleaned)
            logger.info("Vision analysis complete — kling_prompt: %d chars",
                        len(parsed.get("kling_prompt", "")))
            return parsed

        except json.JSONDecodeError as e:
            logger.warning("Vision response not valid JSON (attempt %d/3): %s\nRaw: %s",
                           attempt + 1, e, raw[:300] if raw else "(empty)")
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
        except Exception as e:
            logger.warning("Vision analysis failed (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(10 * (attempt + 1))

    return None


# ---------------------------------------------------------------------------
# Kling image-to-video generation
# ---------------------------------------------------------------------------

def generate_kling_video(
    image_url: str,
    prompt: str,
    negative_prompt: str = "",
    duration: int = 5,
    aspect_ratio: str = "9:16",
) -> dict | None:
    """Generate a video from a still image using Kling via fal.ai."""
    duration_str = str(duration)

    for attempt in range(3):
        try:
            logger.info("Submitting Kling img2vid (duration=%ss, aspect=%s)...", duration_str, aspect_ratio)
            result = fal_client.subscribe(
                "fal-ai/kling-video/v2.1/master/image-to-video",
                arguments={
                    "image_url": image_url,
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "duration": duration_str,
                    "aspect_ratio": aspect_ratio,
                },
                with_logs=False,
            )

            video_url = result.get("video", {}).get("url", "")
            if video_url:
                logger.info("Kling video generated: %s", video_url[:80])
            else:
                logger.warning("Kling returned no video URL")
            return result

        except Exception as e:
            logger.warning("Kling generation failed (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(15 * (attempt + 1))

    return None


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def save_reel_metadata(
    output_dir: Path,
    model_name: str,
    blogger: str,
    reel_source: str,
    reel_code: str,
    scene_prompt: str,
    vision_result: dict,
    generated_files: list[str],
    duration: int,
    vision_model: str,
):
    """Save reel-to-video pipeline metadata in project style."""
    meta = {
        "mode": "reel-to-video",
        "model": model_name,
        "blogger_source": blogger,
        "reel_source": reel_source,
        "reel_code": reel_code,
        "scene_prompt": scene_prompt,
        "vision_model": vision_model,
        "vision_analysis": vision_result,
        "kling_prompt": vision_result.get("kling_prompt", ""),
        "negative_prompt": vision_result.get("negative_prompt", ""),
        "duration_seconds": duration,
        "generated_files": generated_files,
        "generated_at": datetime.now().isoformat(),
    }
    meta_file = output_dir / "metadata.json"
    with open(meta_file, "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    logger.info("Metadata saved -> %s", meta_file)
