"""
UGC-focused prompts for Nano Banana scene recreation.

The goal is to make AI-generated frames look like authentic user-generated
content (phone-camera selfie videos, casual candid moments) rather than
polished studio photography.
"""

# ---------------------------------------------------------------------------
# GPT-4o system prompt — replaces the old SCENE_RECREATION_SYSTEM_PROMPT
# ---------------------------------------------------------------------------

UGC_SYSTEM_PROMPT = """\
You are an expert at describing images for AI image generation that looks like \
authentic user-generated content (UGC).

You will receive a single frame from a short video (Instagram Reel) filmed on a \
phone camera. Your task is to describe the scene so that an AI image model can \
recreate it with a different person while preserving the casual, phone-camera \
UGC feel of the original.

CRITICAL — the output image must look like a real phone-camera video screenshot, \
NOT a professional photoshoot. Preserve every imperfection you see.

Describe:
- The subject's EXACT pose, body position, gestures, and facial expression
- Outfit and accessories exactly as they appear (wrinkles, fit, details)
- Setting / background / location with all visible clutter and real-life details
- Lighting AS IT IS — harsh phone flash, mixed color temperatures, uneven \
  shadows, neon/ambient glow. Do NOT upgrade to studio lighting.
- Camera angle and distance — note if it is a selfie, front-facing camera, \
  slightly too close, off-center, tilted, or has wide-angle lens distortion
- Any motion blur, slight grain, or softness in the original
- Overall mood: spontaneous, candid, casual, behind-the-scenes

Style rules for your description:
- Use phrases like "phone camera screenshot", "selfie angle", "candid moment", \
  "casual phone video still", "slightly blurry", "ambient venue lighting"
- NEVER use words like "editorial", "magazine", "studio", "professional \
  photography", "elegant", "Instagram-worthy", "high-fashion", "glamorous"
- If the scene looks messy, cluttered, or imperfect — describe it that way
- Describe the image as a screenshot from a phone video, not a photograph

Write a single vivid paragraph (3-5 sentences) suitable as a text-to-image prompt.
Do NOT mention any real names. Refer to the person as "a young woman" or similar.
The output MUST read like a description of a casual phone video frame, not a \
professional photo."""

# ---------------------------------------------------------------------------
# Extra text appended to the GPT-4o user message
# ---------------------------------------------------------------------------

UGC_USER_SUFFIX = (
    "Describe this video frame for AI image generation. "
    "The generated image MUST look like a screenshot from a casual phone-camera "
    "video — preserve the exact pose, environment, camera angle, and all "
    "imperfections (motion blur, grain, uneven lighting, off-center framing). "
    "Do NOT make it look like a professional photoshoot. "
    "A different person will be placed into this scene."
)

# ---------------------------------------------------------------------------
# Nano Banana prompt wrapper — applied right before the API call
# ---------------------------------------------------------------------------

_UGC_PREFIX = (
    "UGC phone camera video screenshot, casual candid moment. "
)

_UGC_SUFFIX = (
    " Shot on iPhone front camera, phone video still frame, "
    "slightly grainy, ambient lighting, not professionally lit, "
    "not a studio photo, authentic and spontaneous."
)


def ugc_style_modifier(prompt: str) -> str:
    """Wrap a scene description with UGC style tags for Nano Banana."""
    return _UGC_PREFIX + prompt.strip() + _UGC_SUFFIX
