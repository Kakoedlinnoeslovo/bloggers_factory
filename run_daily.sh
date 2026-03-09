#!/bin/bash
# Glam Bloggers Factory - Daily cron job wrapper
# Crontab entry: 0 9 * * * /Users/roman/Desktop/glam_bloggers_factory/run_daily.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export PATH="/opt/anaconda3/bin:$PATH"

python generate_posts.py --cron 2>&1
