"""Reconstruct the hardened Ternary source used by the Render Blueprint."""
from __future__ import annotations

import base64
import hashlib
import io
import lzma
import shutil
import tarfile
from pathlib import Path

BUNDLE_DIR = Path("bundle")
DEST = Path(".render_src")
EXPECTED_XZ_SHA256 = "7ede60de89ebde8fb064ca76d5913083dcfecc80c4be1aba3d77b46492d8f1be"

parts = sorted(BUNDLE_DIR.glob("xzchunk*.b64"))
if not parts:
    raise SystemExit("No source bundle chunks found")

encoded = "".join(p.read_text(encoding="ascii").strip() for p in parts)
compressed = base64.b64decode(encoded, validate=True)
actual = hashlib.sha256(compressed).hexdigest()
if actual != EXPECTED_XZ_SHA256:
    raise SystemExit(f"Source bundle checksum mismatch: {actual}")

tar_bytes = lzma.decompress(compressed)
if DEST.exists():
    shutil.rmtree(DEST)
DEST.mkdir(parents=True)

with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
    # This archive is generated and checksum-pinned by this repository.
    archive.extractall(DEST)

print(f"Unpacked {len(parts)} verified source chunks into {DEST}")
