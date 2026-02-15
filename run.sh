#!/bin/bash
# Market Making Bot Runner

# Change to script directory
cd "$(dirname "$0")"

# Check if virtual environment is activated
if [[ -z "$VIRTUAL_ENV" ]]; then
    echo "⚠️  Virtual environment not activated"
    echo "Run: source venv/bin/activate"
    exit 1
fi

# Check if .env exists
if [[ ! -f .env ]]; then
    echo "❌ .env file not found"
    echo "Run: cp .env.example .env"
    echo "Then edit .env with your configuration"
    exit 1
fi

# Run the bot
echo "🚀 Starting market making bot..."
PYTHONPATH=. python3 mm_bot/main.py
