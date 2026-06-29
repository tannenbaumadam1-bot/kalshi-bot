#!/usr/bin/env bash
# Run this ONCE on a fresh Ubuntu cloud server, from inside the bot folder.
set -e
echo "Installing Python + tools..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip
echo "Creating a virtual environment and installing requirements..."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p logs
echo ""
echo "Setup done. Quick test:"
.venv/bin/python run.py check
echo ""
echo "If you saw your balance above, you're ready to install the 24/7 service."
echo "See deploy/DEPLOY_24_7.md for the next steps."
