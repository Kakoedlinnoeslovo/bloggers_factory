import json
import logging
import os

from openai import OpenAI

from .utils import download_image_as_base64, retry

logger = logging.getLogger("bloggers_factory")

CAROUSEL_SYSTEM_PROMPT = """\
You are an expert Instagram content strategist and AI image prompt engineer.

You will receive a real Instagram blogger's post (caption + image). Your job is to:
1. Analyze the scene, setting, pose, outfit, lighting, mood, and aesthetic from the image and caption.
2. Generate exactly {carousel_size} distinct image generation prompts for Nano Banana 2 (a text-to-image model that takes reference face images).
3. Each prompt should describe a photorealistic scene that recreates a similar vibe/aesthetic but with enough variety for an Instagram carousel post.
4. Prompts should describe the woman's pose, outfit, setting, lighting, and mood in detail.
5. Do NOT mention any names or usernames. Describe the person generically (e.g., "a young woman", "the woman").
6. Vary angles, poses, and backgrounds across the prompts while keeping a cohesive theme/aesthetic.
7. Each prompt should be 1-3 sentences, vivid and specific.
8. Use Instagram-worthy aesthetics: golden hour lighting, editorial fashion, lifestyle vibes.

Return ONLY valid JSON with this structure:
{{
  "theme": "brief theme description",
  "prompts": ["prompt 1", "prompt 2", ...]
}}"""


def generate_prompts(
    caption: str,
    image_url: str,
    carousel_size: int = 5,
    system_prompt: str | None = None,
) -> dict | None:
    """Use GPT-4o to analyze a blogger post and generate image prompts.

    Pass a custom system_prompt for different content types (e.g. dance/animation).
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in .env")

    client = OpenAI(api_key=api_key)

    if system_prompt is None:
        system_prompt = CAROUSEL_SYSTEM_PROMPT
    system = system_prompt.format(carousel_size=carousel_size)

    image_data_uri = download_image_as_base64(image_url)
    if not image_data_uri:
        logger.error("Could not download inspiration image")
        return None

    @retry(delay=5, backoff=2, default=None)
    def _call():
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Instagram caption:\n{caption}\n\n"
                                f"Analyze this post and generate {carousel_size} image prompts."
                            ),
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
        logger.info("Theme: %s | %d prompts generated",
                     result.get("theme", ""), len(result.get("prompts", [])))
        return result

    return _call()
