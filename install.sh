#!/bin/bash

# Installation script for mm-example bot

echo "Installing market making bot dependencies..."

# Check if running in virtual environment
if [[ -z "$VIRTUAL_ENV" ]]; then
    echo "Warning: Not running in a virtual environment"
    echo "It's recommended to create one with: python3 -m venv venv && source venv/bin/activate"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Install all requirements (including kuru-sdk-py from PyPI)
echo "Installing dependencies..."
python3 -m pip install -r requirements.txt

echo "Installation complete!"
echo ""
echo "Next steps:"
echo "1. Copy .env.example to .env and configure your settings"
echo "2. Run the bot with: ./run.sh"
