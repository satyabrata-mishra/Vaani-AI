"""Run the stand-in in a background thread of the current process (tests, experiments)."""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import replace

import uvicorn

from app.simulator.profiles import build_profiles
from app.simulator.server import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StandInThread:
    def __init__(self, profile: str = "normal", *, seed: int = 0, scale: float = 1.0, port: int | None = None,
                 **profile_changes):
        profiles = build_profiles()
        if profile_changes:
            profiles[profile] = replace(profiles[profile], **profile_changes)
        self.app = create_app(profile, seed=seed, scale=scale, profiles=profiles)
        self.port = port or free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.api_base = f"{self.base}/openai/v1"
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> "StandInThread":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("stand-in did not start")
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
