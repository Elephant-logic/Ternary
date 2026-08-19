"""Runtime persistence for diskless/free deployments.

Local state remains plain JSON. When Upstash REST credentials are present, a
single versioned JSON snapshot containing AppState + the event journal is copied
to durable remote storage and restored before the service boots.

No SQL database is required for this mode.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import asdict

SNAPSHOT_VERSION = 1


def _atomic_json_write(path: str, obj) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class UpstashJsonStore:
    """Dependency-free Upstash REST client storing one JSON string value."""
    def __init__(self, url: str, token: str, key: str = "ternary:runtime:snapshot"):
        self.url = url.rstrip("/")
        self.token = token
        self.key = key

    @classmethod
    def from_env(cls):
        url = os.environ.get("UPSTASH_REDIS_REST_URL")
        token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        if not url or not token:
            return None
        return cls(url, token, os.environ.get("TERN_REMOTE_STATE_KEY", "ternary:runtime:snapshot"))

    def _command(self, parts):
        body = json.dumps(parts, separators=(",", ":")).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode())
        if "error" in payload:
            raise RuntimeError(f"remote state store error: {payload['error']}")
        return payload.get("result")

    def load(self):
        raw = self._command(["GET", self.key])
        if raw is None:
            return None
        if isinstance(raw, dict):
            return raw
        return json.loads(raw)

    def save(self, snapshot: dict):
        raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        self._command(["SET", self.key, raw])


class RuntimePersistence:
    """Coordinates local JSON files and an optional durable remote JSON snapshot."""
    def __init__(self, state_path: str, eventlog_path: str, remote=None):
        self.state_path = state_path
        self.eventlog_path = eventlog_path
        self.remote = remote if remote is not None else UpstashJsonStore.from_env()
        self.last_error = None
        self.last_saved_ns = None
        self.restored = False

    @property
    def remote_enabled(self) -> bool:
        return self.remote is not None

    def restore(self) -> bool:
        if not self.remote_enabled:
            return False
        try:
            snap = self.remote.load()
            if not snap:
                return False
            if snap.get("version") != SNAPSHOT_VERSION:
                raise ValueError(f"unsupported runtime snapshot version {snap.get('version')!r}")
            state, events = snap.get("state"), snap.get("events")
            if not isinstance(state, dict) or not isinstance(events, list):
                raise ValueError("malformed runtime snapshot")
            _atomic_json_write(self.state_path, state)
            _atomic_json_write(self.eventlog_path, events)
            self.restored = True
            self.last_error = None
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    def save(self, state, eventlog) -> bool:
        if not self.remote_enabled:
            return False
        try:
            sd = asdict(state)
            sd.pop("path", None)
            snap = {"version": SNAPSHOT_VERSION, "saved_at_ns": time.time_ns(),
                    "state": sd, "events": [asdict(e) for e in eventlog.all()],
                    "head": eventlog.head()}
            self.remote.save(snap)
            self.last_saved_ns = snap["saved_at_ns"]
            self.last_error = None
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    def status(self) -> dict:
        return {"remote_json": self.remote_enabled, "restored_from_remote": self.restored,
                "last_saved_ns": self.last_saved_ns, "error": self.last_error}
