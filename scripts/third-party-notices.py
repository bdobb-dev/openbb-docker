#!/usr/bin/env python3
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Regenerate THIRD_PARTY_NOTICES.md from the images this stack runs.

    python3 scripts/third-party-notices.py            # every image compose names
    python3 scripts/third-party-notices.py openbb-local:4.7.2 openbb-live-grid:latest

Each Python image is asked, with the standard library only, for every
installed distribution: name, version, licence, and the licence text the wheel
carried in its dist-info. The images are what ship, so the images are what is
read -- a requirements file would miss the transitive set and the OpenBB
extensions the Dockerfile installs. The non-Python components (MinIO,
Tailscale, the inlined q, kdb+) are a fixed section, because their licences
do not change with a lockfile.

Run it after rebuilding an image; the header records which image tags and
build dates the file describes, so a stale file is legible as stale.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "THIRD_PARTY_NOTICES.md"

# Services whose images are Python and built from this repository.
DEFAULT_SERVICES = ["openbb-api", "stores-mcp", "stores-explorer", "key-maint", "live-grid"]

DUMP = r'''
import importlib.metadata as m, json, sys
rows = []
for d in m.distributions():
    meta = d.metadata
    lic = meta.get("License-Expression") or ""
    if not lic:
        cls = [c.split("::")[-1].strip() for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
        raw = meta.get("License") or ""
        lic = cls[0] if cls else (raw if raw and len(raw) <= 60 and "\n" not in raw else ("see licence text" if raw else "Unknown"))
    texts = []
    for f in d.files or []:
        n = f.name.lower()
        if ".dist-info" in str(f) and (n.startswith(("license", "licence", "copying", "notice")) or "/licenses/" in str(f).lower()):
            try: texts.append(f.read_binary().decode("utf-8", "replace").strip())
            except Exception: pass
    if not texts:
        raw = meta.get("License") or ""
        if raw and "\n" in raw: texts.append(raw.strip())
    rows.append({"name": meta.get("Name"), "version": meta.get("Version"), "license": lic,
                 "home": meta.get("Home-page") or "", "texts": texts})
json.dump(sorted(rows, key=lambda r: ((r["name"] or "").lower(), r["version"] or "")), sys.stdout)
'''


def compose_images() -> dict[str, str]:
    """service -> image tag, from docker-compose.yml, for the default services."""
    text = (ROOT / "docker-compose.yml").read_text()
    out: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^  ([a-z0-9-]+):\s*$", line)
        if m:
            current = m.group(1)
        m = re.match(r"^\s+image:\s*(\S+)", line)
        if m and current in DEFAULT_SERVICES:
            out[current] = m.group(1)
    return out


def image_created(tag: str) -> str | None:
    r = subprocess.run(["docker", "image", "inspect", tag, "--format", "{{.Created}}"], capture_output=True, text=True)
    return r.stdout.strip()[:10] if r.returncode == 0 else None


def dump(tag: str) -> list[dict]:
    r = subprocess.run(["docker", "run", "--rm", "-i", tag, "python", "-"], input=DUMP,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "docker run failed")
    return json.loads(r.stdout)


FIXED = """\
## Components that are not Python packages

- **OpenBB Platform** (AGPL-3.0-only) — the application every image serves.
  Its licence still governs OpenBB inside the images; this repository's
  modifications to it (the CFTC startup guard in the Dockerfile, `api_app.py`)
  are in this tree, which is the source offer that licence requires.
- **MinIO** (AGPL-3.0) — runs unmodified in the `openbb-minio` image
  (`quay.io/minio/minio`), which also copies the **Tailscale** `tailscaled`
  and `tailscale` binaries (BSD-3-Clause, https://github.com/tailscale/tailscale)
  from `tailscale/tailscale`. Both licences travel with those upstream images.
- **ws.q** by Jonathon McMurray (MIT) — about forty lines of its `.wsu`
  namespace are inlined in `kdb-ws/startup.q`; the MIT notice is reproduced
  in that file.
- **kdb+ / kdb-x and the `kc.lic` licence** (KX personal licence) — **not
  redistributed.** The `kdb/` and `kdb-license/` directories are empty and
  git-ignored; you supply your own. `pykx` in the Python images is KX's
  client library under KX's own licence terms and runs unlicensed as an IPC
  client.
- **FirstRate Data tick sample** (commercial data licence) — not in this
  repository; `tick-lab` reads a copy you download yourself.
- **EODHD** — data under your own EODHD subscription; the Python SDK is MIT
  and listed below.
- **rss-feedhandler** (MIT) — its own repository carries its own notices.
"""


def main(argv: list[str]) -> int:
    images = argv or list(dict.fromkeys(compose_images().values()))
    sections: list[str] = []
    header_rows: list[str] = []
    for tag in images:
        created = image_created(tag)
        if created is None:
            header_rows.append(f"| `{tag}` | not present locally — section omitted |")
            continue
        try:
            rows = dump(tag)
        except Exception as e:  # noqa: BLE001
            header_rows.append(f"| `{tag}` | read failed: {e} |")
            continue
        header_rows.append(f"| `{tag}` | built {created}, {len(rows)} distributions |")
        body = [f"## Image `{tag}`", "", f"Built {created}. {len(rows)} Python distributions.", ""]
        for r in rows:
            body.append(f"### {r['name']} {r['version']}")
            body.append("")
            body.append(f"Licence: {r['license']}" + (f" · {r['home']}" if r["home"] else ""))
            body.append("")
            if r["texts"]:
                for t in r["texts"]:
                    body += ["```", t, "```", ""]
            else:
                body += ["_The wheel ships no licence file; the licence is as declared above._", ""]
        sections.append("\n".join(body))

    doc = [
        "# Third-party notices",
        "",
        "openbb-docker is Apache-2.0 (see `LICENSE`). The images it builds bundle the",
        "open-source work below, each under its own licence, reproduced here as those",
        "licences require. Generated by `python3 scripts/third-party-notices.py` by",
        "reading each image's installed distributions; do not edit by hand.",
        "",
        "| Image | State when this file was generated |",
        "|---|---|",
        *header_rows,
        "",
        FIXED,
    ] + sections
    OUT.write_text("\n".join(doc))
    print(f"{len(sections)} image(s) -> {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
