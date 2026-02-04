#!/bin/bash
# Load environment variables from .env if it exists
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# Run the bot with environment variables as defaults
# Extra arguments can be passed to this script: ./run.sh --dry-run
python3 review_bot.py "$@"