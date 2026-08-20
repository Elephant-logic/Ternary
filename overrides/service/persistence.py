"""Runtime persistence for diskless/free deployments.

Local state remains plain JSON. When GitHub state credentials are present, a
single versioned JSON snapshot containing AppState + the event journal is copied
to a dedicated GitHub branch and restored before the service boots.

No SQL database is required for this mode.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
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


class GitHubJsonStore:
    """Dependency-free GitHub Contents API client for one JSON snapshot file."""

    def __init__(self, token: str, repo: str, branch: str = "state",
                 path: str = "runtime/ternary-state.json"):
        self.token = token
        self.repo = repo
        self.branch = branch
        self.path = path
        encoded_path = urllib.parse.quote(path, safe="/")
        self.url = f"https://api.github.com/repos/{repo}/contents/{encoded_path}"

    @classmethod
    def from_env(cls):
        token = os.environ.get("GITHUB_STATE_TOKEN")
        repo = os.environ.get("GITHUB_STATE_REPO")
        if not token or not repo:
            return None
        return cls(
            token,
            repo,
            os.environ.get("GITHUB_STATE_BRANCH", "state"),
            os.environ.get("GITHUB_STATE_PATH", "runtime/ternary-state.json"),
        )

    def _request(self, method: str, url: str, body=None):
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ternary-runtime-state",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

    def _metadata(self):
        url = self.url + "?ref=" + urllib.parse.quote(self.branch, safe="")
        try:
            return self._request("GET", url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def load(self):
        meta = self._metadata()
        if not meta:
            return None
        raw = base64.b64decode(meta["content"].replace("\n", "")).decode("utf-8")
        return json.loads(raw)

    def save(self, snapshot: dict):
        raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        payload = {
            "message": "Update Ternary runtime state",
            "content": base64.b64encode(raw.encode()).decode(),
            "branch": self.branch,
        }
        meta = self._metadata()
        if meta and meta.get("sha"):
            payload["sha"] = meta["sha"]
        self._request("PUT", self.url, payload)


class RuntimePersistence:
    """Coordinates local JSON files and an optional durable GitHub JSON snapshot."""

    def __init__(self, state_path: str, eventlog_path: str, remote=None):
        self.state_path = state_path
        self.eventlog_path = eventlog_path
        self.remote = remote if remote is not None else GitHubJsonStore.from_env()
        self.last_error = None
        self.last_saved_ns = None
        self.restored = False
        self._last_digest = None
        self.min_save_seconds = max(0, int(os.environ.get("TERN_REMOTE_SAVE_MIN_SECONDS", "300")))

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
            self.last_saved_ns = snap.get("saved_at_ns")
            self._last_digest = self._digest_payload(snap)
            self.last_error = None
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    def _snapshot(self, state, eventlog) -> dict:
        sd = asdict(state)
        sd.pop("path", None)
        return {
            "version": SNAPSHOT_VERSION,
            "saved_at_ns": time.time_ns(),
            "state": sd,
            "events": [asdict(e) for e in eventlog.all()],
            "head": eventlog.head(),
        }

    @staticmethod
    def _digest_payload(snapshot: dict) -> str:
        stable = dict(snapshot)
        stable.pop("saved_at_ns", None)
        raw = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    def save(self, state, eventlog, force: bool = False) -> bool:
        """Save a durable snapshot.

        Routine worker checkpoints remain rate-limited. Critical user actions and
        fills can pass ``force=True`` so a Render restart immediately afterwards
        does not roll the PAPER account or settings back to an older snapshot.
        """
        if not self.remote_enabled:
            return False
        now_ns = time.time_ns()
        if not force and self.last_saved_ns and self.min_save_seconds:
            if now_ns - self.last_saved_ns < self.min_save_seconds * 1_000_000_000:
                return True
        try:
            snap = self._snapshot(state, eventlog)
            digest = self._digest_payload(snap)
            if digest == self._last_digest:
                return True
            self.remote.save(snap)
            self.last_saved_ns = snap["saved_at_ns"]
            self._last_digest = digest
            self.last_error = None
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    def status(self) -> dict:
        return {
            "remote_json": self.remote_enabled,
            "remote_backend": "github" if self.remote_enabled else None,
            "restored_from_remote": self.restored,
            "last_saved_ns": self.last_saved_ns,
            "min_save_seconds": self.min_save_seconds,
            "error": self.last_error,
        }
