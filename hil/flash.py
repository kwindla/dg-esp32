#!/usr/bin/env python3
"""Build/flash under the capture lock, saving exact image hashes and build path."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone
from esp_serial import ROOT, port_lock
from board_port import resolve_port

def firmware_info(project):
    build = project / "build"
    args = json.loads((build / "flasher_args.json").read_text())
    images = {}
    for offset, name in args["flash_files"].items():
        path = (build / name).resolve()
        images[offset] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"project": str(project), "build_dir": str(build.resolve()), "images": images}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", type=Path, default=ROOT)
    p.add_argument("--port", default=None)
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    project = a.project if a.project.is_absolute() else ROOT / a.project
    out = a.out or ROOT / "hil/out" / datetime.now(timezone.utc).strftime("flash-%Y%m%dT%H%M%S.%fZ")
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    port = resolve_port(a.port)
    # Build before taking the shared bench lock.
    subprocess.run([str(ROOT / "scripts/idf.sh"), "build"], cwd=project, check=True, timeout=600)
    with port_lock(), (out / "flash.log").open("w") as log:
        subprocess.run([str(ROOT / "scripts/idf.sh"), "-p", port, "flash"],
                       cwd=project, env={**os.environ, "DG_SERIAL_LOCK_HELD": "1"},
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
        info = firmware_info(project)
        (out / "firmware.json").write_text(json.dumps(info, indent=2) + "\n")
        print(json.dumps(info, indent=2))

if __name__ == "__main__":
    main()
