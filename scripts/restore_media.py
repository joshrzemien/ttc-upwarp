#!/usr/bin/env python3
"""Recover public incident video as a new, independently hash-bound dataset."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOD = "https://www.youtube.com/watch?v=TTh3LY-5KKg"
HIGHLIGHT = "https://www.youtube.com/watch?v=bhBf5crp0i8"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "media")
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    downloader = shutil.which("yt-dlp") or str(ROOT / ".venv/bin/yt-dlp")
    base = [downloader, "--ignore-config", "--no-playlist", "--no-overwrites"]
    commands = []
    for name, end in [("vod_segment", "3483.0"), ("vod_extended", "3486.0")]:
        command = base + ["--format", "134", "--download-sections", f"*3467.0-{end}",
                          "--output", str(directory / f"{name}.%(ext)s"), "--no-part", VOD]
        commands.append(command)
        subprocess.run(command, check=True)
    probe = directory / "vod_source_pts.mp4"
    probe_description = "ffmpeg -ss 3460 -to 3487 -i FORMAT_134_URL -map 0:v:0 -c copy -copyts -start_at_zero -avoid_negative_ts disabled vod_source_pts.mp4"
    if not probe.exists():
        url = subprocess.run(base + ["--no-warnings", "--format", "134", "--print", "urls", VOD],
                             capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "3460", "-to", "3487",
                        "-i", url, "-map", "0:v:0", "-c", "copy", "-copyts", "-start_at_zero",
                        "-avoid_negative_ts", "disabled", str(probe)], check=True)
    command = base + ["--format", "400+251", "--merge-output-format", "webm",
                      "--output", str(directory / "ttc-highlight.%(ext)s"), HIGHLIGHT]
    commands.append(command)
    subprocess.run(command, check=True)
    version = subprocess.run([downloader, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run([sys.executable, str(ROOT / "scripts/analyze_vod_cadence.py"),
                    "--input-video", str(directory / "vod_segment.mp4"), "--source-offset", "3467",
                    "--event-interval", "3481.4", "3481.47", "--crop", "0", "145", "340", "215",
                    "--source-start-probe-path", str(probe), "--source-start-probe-command", probe_description,
                    "--source-id", "TTh3LY-5KKg", "--source-url", VOD, "--format-id", "134",
                    "--yt-dlp-version", version, "--retrieval-command", shlex.join(commands[0]),
                    "--output", str(directory / "vod_cadence_audit.json")], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/extract_vod_fit_targets.py"),
                    "--input-video", str(directory / "vod_segment.mp4"),
                    "--extension-video", str(directory / "vod_extended.mp4"),
                    "--audit", str(directory / "vod_cadence_audit.json"),
                    "--result-dir", str(directory / "vod_fit_targets"),
                    "--output", str(directory / "vod_fit_targets.json")], check=True)
    files = [directory / name for name in ["vod_segment.mp4", "vod_extended.mp4", "vod_source_pts.mp4", "ttc-highlight.webm"]]
    report = {
        "kind": "local_media_recovery",
        "historical_identity_claimed": False,
        "note": "Fresh retrievals are not replacements for historical hash-bound bytes. Use the local cadence audit and regenerate downstream manifests.",
        "commands": [shlex.join(command) for command in commands],
        "source_pts_probe": probe_description,
        "files": [{"path": str(path), "size_bytes": path.stat().st_size,
                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in files],
    }
    (directory / "recovery.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Recovered media, fresh cadence audit, and target crops in {directory}")


if __name__ == "__main__":
    main()
