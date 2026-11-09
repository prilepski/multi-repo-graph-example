#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dmitri Prilepski <dmitri@vidukate.ai>
"""Stop workspace services (counterpart to up.py): viewer process + neo4j
container. --purge also removes the volume and materialized snapshot."""

import argparse
import subprocess
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
WS = RUNTIME.parent
def consumer_names():
    import json
    settings = json.loads((WS / "workspace.json").read_text()).get("settings", {})
    base = settings.get("neo4j", {}).get("container") or "codegraph-neo4j"
    return f"{base}-serve", f"{base}-serve-data"


def log(msg: str) -> None:
    print(f"[down] {msg}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--purge", action="store_true",
                    help="also remove the neo4j volume and snapshot/")
    args = ap.parse_args()

    pid_file = WS / "snapshot" / "viewer.pid"
    if pid_file.exists():
        try:
            import os
            pid = int(pid_file.read_text().strip())
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True)
            else:
                os.kill(pid, 15)
            log(f"viewer stopped (pid {pid})")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        pid_file.unlink(missing_ok=True)

    container, volume = consumer_names()
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    log("neo4j container removed")
    if args.purge:
        subprocess.run(["docker", "volume", "rm", "-f", volume],
                       capture_output=True)
        import shutil
        shutil.rmtree(WS / "snapshot", ignore_errors=True)
        log("volume + snapshot/ purged")


if __name__ == "__main__":
    main()
