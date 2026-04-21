#!/bin/bash
set -e
echo ">>> Pulling latest code from GitHub..."
git fetch origin main
git reset --hard origin/main
echo ">>> Starting app..."
exec python web_app.py
