# Bloggers Factory

AI-powered Instagram carousel generator. Scrapes real blogger posts for inspiration, generates image prompts via GPT-4o, and produces photorealistic carousel images using fal.ai's Nano Banana 2 model with reference face images.

## How It Works

```
Instagram post (caption + image)
        |
        v
   GPT-4o analyzes the vibe, pose, outfit, setting
        |
        v
   Generates N image prompts per carousel
        |
        v
   fal.ai Nano Banana 2 renders each prompt
   using reference face images for consistency
        |
        v
   Downloads images + saves metadata to output/
```

## Setup

1. **Install dependencies:**

```bash
pip install -r requirements.txt
```

2. **Create a `.env` file** with your API keys:

```
RAPID_API_KEY=your_rapidapi_key
OPENAI_API_KEY=your_openai_key
FAL_AI_API_KEY=your_fal_ai_key
```

3. **Add reference images** for each AI model in `ai_models/<ModelName>/` (JPG/PNG face photos).

4. **Configure models** in `config.json` -- map each model to an Instagram blogger and a reference image directory.

## Usage

### Single carousel

Generate one carousel for a specific model:

```bash
python generate.py --model Andrea
```

### Bulk generation

Generate many carousels per model with resume support:

```bash
# Sequential (one model at a time)
python generate.py --bulk --min-carousels 60

# Single model only
python generate.py --bulk --model Andrea --min-carousels 60

# Parallel (multiple models at once)
python generate.py --bulk --parallel --workers 4
```

### Cron mode

Generate one carousel per model for all models (daily automation):

```bash
python generate.py --cron
```

### Check progress

```bash
python generate.py --status
```

### Reset state

```bash
# Reset one model
python generate.py --reset --model Andrea

# Reset all
python generate.py --reset
```

### All options

| Flag | Description |
|---|---|
| `--model NAME` | Run for a single model |
| `--bulk` | Bulk mode with resume state |
| `--parallel` | Process models concurrently (bulk mode) |
| `--workers N` | Number of parallel model workers (default: 4) |
| `--min-carousels N` | Target carousels per model (default: 60) |
| `--config PATH` | Config file path (default: `config.json`) |
| `--verbose` | Enable debug logging |
| `--status` | Show progress summary and exit |
| `--reset` | Reset generation state |
| `--cron` | One carousel per model, all models |

## Project Structure

```
bloggers_factory/
  generate.py              # CLI entry point
  config.json              # Model + blogger mappings
  requirements.txt         # Python dependencies
  run_daily.sh             # Cron wrapper script
  lib/
    utils.py               # Logging, retry decorator, download helpers
    state.py               # Thread-safe generation state persistence
    instagram.py           # Instagram API fetching + post caching
    prompts.py             # GPT-4o prompt generation
    image_gen.py           # fal.ai ref upload, image gen, downloads, metadata
  ai_models/               # Reference face images (batch 1)
  ai_models_batch_2/       # Reference face images (batch 2)
  output/                  # Generated carousels (batch 1)
  output2/                 # Generated carousels (batch 2)
  posts_cache/             # Cached Instagram posts per model
  logs/                    # Run logs
```

## Config

`config.json` maps each AI model to its Instagram inspiration source and reference images:

```json
{
  "models": {
    "Andrea": {
      "bloggers": ["beccaxbloom"],
      "ref_images_dir": "ai_models/Andrea"
    },
    "Bibi": {
      "bloggers": ["kendalljenner"],
      "ref_images_dir": "ai_models_batch_2/Bibi",
      "output_dir": "output2"
    }
  },
  "carousel_size": 5,
  "aspect_ratio": "4:5",
  "output_dir": "output"
}
```

Per-model `output_dir` overrides the global default when set.

## APIs Used

| Service | Purpose |
|---|---|
| [RapidAPI / instagram120](https://rapidapi.com/) | Fetch Instagram posts for inspiration |
| [OpenAI GPT-4o](https://platform.openai.com/) | Analyze posts and generate image prompts |
| [fal.ai Nano Banana 2](https://fal.ai/) | Generate photorealistic images from prompts + reference faces |
