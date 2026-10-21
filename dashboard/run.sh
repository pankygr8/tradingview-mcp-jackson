#!/usr/bin/env bash
set -e
echo ""
echo " Installing / verifying Python dependencies..."
pip3 install -r requirements.txt -q
echo ""
echo " Starting Trading Dashboard..."
echo " Open your browser at: http://localhost:5000"
echo " Press Ctrl+C to stop."
echo ""
python3 app.py
