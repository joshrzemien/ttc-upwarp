#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

FIELD_RE = re.compile(r"([A-Za-z_]+)=([^ ]+)")


def parse_fields(line: str) -> dict[str, str]:
    return dict(FIELD_RE.findall(line))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    runs: list[dict[str, object]] = []
    candidates: list[dict[str, str]] = []
    incomplete: list[str] = []

    for path in sorted(args.directory.glob("*.log")):
        done: dict[str, str] | None = None
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("FUZZ_CANDIDATE "):
                candidate = parse_fields(line)
                candidate["log"] = path.name
                candidates.append(candidate)
            elif line.startswith("FUZZ_DONE "):
                done = parse_fields(line)
        if done is None:
            incomplete.append(path.name)
            continue
        runs.append(
            {
                "log": path.name,
                "seed": int(done["seed"]),
                "speed_mode": int(done["speed_mode"]),
                "horizon": int(done.get("horizon", "1")),
                "reset_mode": int(done.get("reset_mode", "0")),
                "profile": int(done.get("profile", "0")),
                "trials": int(done["trials"]),
                "valid_trials": int(done["valid_trials"]),
                "simulated_frames": int(
                    done.get("simulated_frames", done["valid_trials"])
                ),
                "candidates": int(done["candidates"]),
                "largest_delta": float(done["largest_delta"]),
                "largest_trial": int(done["largest_trial"]),
                "largest_step": int(done.get("largest_step", "0")),
                "seconds": float(done["seconds"]),
                "trials_per_second": float(done["trials_per_second"]),
                "frames_per_second": float(
                    done.get("frames_per_second", done["trials_per_second"])
                ),
            }
        )

    largest_run = max(runs, key=lambda run: float(run["largest_delta"]), default=None)
    by_speed_mode: dict[str, dict[str, object]] = {}
    for speed_mode in sorted({int(run["speed_mode"]) for run in runs}):
        matching = [
            run for run in runs if int(run["speed_mode"]) == speed_mode
        ]
        speed_largest = max(
            matching, key=lambda run: float(run["largest_delta"])
        )
        by_speed_mode[str(speed_mode)] = {
            "runs": len(matching),
            "trials": sum(int(run["trials"]) for run in matching),
            "valid_trials": sum(
                int(run["valid_trials"]) for run in matching
            ),
            "simulated_frames": sum(
                int(run["simulated_frames"]) for run in matching
            ),
            "candidate_count": sum(
                int(run["candidates"]) for run in matching
            ),
            "largest_delta": speed_largest["largest_delta"],
            "largest_delta_run": speed_largest["log"],
        }
    summary = {
        "directory": str(args.directory),
        "log_count": len(runs) + len(incomplete),
        "completed_runs": len(runs),
        "incomplete_logs": incomplete,
        "total_trials": sum(int(run["trials"]) for run in runs),
        "total_valid_trials": sum(int(run["valid_trials"]) for run in runs),
        "total_simulated_frames": sum(
            int(run["simulated_frames"]) for run in runs
        ),
        "candidate_count": len(candidates),
        "reported_candidate_count": sum(
            int(run["candidates"]) for run in runs
        ),
        "largest_delta": None if largest_run is None else largest_run["largest_delta"],
        "largest_delta_run": largest_run,
        "aggregate_trials_per_second": sum(
            float(run["trials_per_second"]) for run in runs
        ),
        "aggregate_frames_per_second": sum(
            float(run["frames_per_second"]) for run in runs
        ),
        "by_speed_mode": by_speed_mode,
        "runs": runs,
        "candidates": candidates,
    }

    encoded = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
