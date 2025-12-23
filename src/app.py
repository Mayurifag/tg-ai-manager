import signal
import os
from src.web import create_app

app = create_app()

def _signal_handler(sig, frame):
    # This might not be strictly necessary with Hypercorn/Uvicorn managing signals,
    # but kept for consistency if running manually.
    os._exit(0)

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

if __name__ == "__main__":
    app.run()
