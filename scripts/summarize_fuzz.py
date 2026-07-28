#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DONE = re.compile(
    r"FUZZ_DONE,seed=(?P<seed>\d+),trials=(?P<trials>\d+),vis=(?P<vis>\d+),"
    r"max_step=(?P<max_step>[-+\deE.]+),max_step_trial=(?P<max_step_trial>\d+),"
    r"max_y=(?P<max_y>[-+\deE.]+),max_y_trial=(?P<max_y_trial>\d+)"
)
HIT = re.compile(r"SOFTWARE_UPWARP,[^\n]+")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()

    runs: list[dict[str, int | float]] = []
    hits: list[str] = []
    incomplete: list[str] = []
    for path in sorted(args.directory.glob("seed_*.log")):
        text = path.read_text(errors="replace")
        hits.extend(HIT.findall(text))
        match = DONE.search(text)
        if match is None:
            if not HIT.search(text):
                incomplete.append(path.name)
            continue
        row: dict[str, int | float] = {}
        for key, value in match.groupdict().items():
            row[key] = float(value) if key in {"max_step", "max_y"} else int(value)
        runs.append(row)

    result = {
        "runs": len(runs),
        "incomplete": incomplete,
        "software_upwarp_hits": hits,
        "total_trials": sum(int(run["trials"]) for run in runs),
        "total_vis": sum(int(run["vis"]) for run in runs),
        "largest_positive_step": max((float(run["max_step"]) for run in runs), default=None),
        "highest_y": max((float(run["max_y"]) for run in runs), default=None),
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
