"""
serve.py — Production entry point using Waitress (Windows-safe WSGI server).

Usage:
    python serve.py

Waitress is multi-threaded, handles concurrent signal scans properly,
and is safe to expose behind Cloudflare Tunnel.
"""

from waitress import serve
from app import app

HOST = "127.0.0.1"   # keep local-only; Cloudflare tunnel handles public access
PORT = 5000

if __name__ == "__main__":
    print("\n" + "=" * 55)
    print("  📊  Trading Dashboard — production server (Waitress)")
    print("=" * 55)
    print(f"  Local  → http://{HOST}:{PORT}")
    print(f"  Public → via Cloudflare Tunnel")
    print(f"  Stop   → Ctrl+C")
    print("=" * 55 + "\n")
    serve(app, host=HOST, port=PORT, threads=8)
