"""Compatibility launcher for installs whose service runs `python server.py` (pre-2.2 systemd unit).
Prefer the `backlogr-server` entry point; this file will be removed in 3.0."""
from backlogr.server import main_entry

if __name__ == "__main__":
    main_entry()
