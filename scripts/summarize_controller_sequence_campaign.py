#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_projection(result: dict[str, Any]) -> tuple[Any, ...]:
    fields = (
        "final_level",
        "final_action",
        "final_x",
        "final_y",
        "final_z",
        "final_vx",
        "final_vy",
        "final_vz",
        "final_face_yaw",
        "final_floor_height",
        "final_ceil_height",
        "final_floor",
        "final_platform",
    )
    return tuple(result[field] for field in fields)


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Distill a restored controller sequence campaign"
    )
    parser.add_argument("campaign", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--baseline-y", type=float, required=True)
    parser.add_argument("--large-rise", type=float, default=500.0)
    args = parser.parse_args()

    campaign = json.loads(args.campaign.read_text())
    results: list[dict[str, Any]] = campaign["all_results"]
    deltas = [float(result["final_y"]) - args.baseline_y for result in results]
    actions = Counter(str(result["final_action"]) for result in results)
    floors = Counter(str(result["final_floor"]) for result in results)
    levels = Counter(int(result["final_level"]) for result in results)
    projected_states = {state_projection(result) for result in results}
    ranked_delta = sorted(
        zip(results, deltas), key=lambda item: item[1], reverse=True
    )
    large_rises = [
        {**result, "baseline_to_final_y_delta": delta}
        for result, delta in ranked_delta
        if delta >= args.large_rise
    ]
    nonfinite_fields: list[dict[str, Any]] = []
    numeric_fields = (
        "final_x",
        "final_y",
        "final_z",
        "final_vx",
        "final_vy",
        "final_vz",
        "final_floor_height",
        "final_ceil_height",
        "nearest_distance",
        "max_y",
    )
    for result in results:
        for field in numeric_fields:
            if not finite_number(result[field]):
                nonfinite_fields.append(
                    {"id": result["id"], "field": field, "value": result[field]}
                )

    summary = {
        "schema_version": 1,
        "complete": bool(campaign["complete"]),
        "campaign": str(args.campaign),
        "campaign_sha256": sha256(args.campaign),
        "candidate_count": int(campaign["candidate_count"]),
        "result_count": len(results),
        "worker_count": int(campaign["worker_count"]),
        "elapsed_seconds": float(campaign["elapsed_seconds"]),
        "throughput_candidates_per_second": float(
            campaign["throughput_candidates_per_second"]
        ),
        "inputs": campaign["inputs"],
        "scope": {
            "horizon_updates": sorted({int(result["steps"]) for result in results}),
            "buttons": ["A", "B", "Z", "R"],
            "analog_domain": "12,269 normalized classes from raw X [-61,61], Y [-63,63] rectangular superset",
            "restoration": "Complete Mupen64Plus state restored before every branch after the first",
            "state_projection": [
                "level",
                "action",
                "position",
                "velocity",
                "face_yaw",
                "floor_height",
                "ceiling_height",
                "floor_pointer",
                "platform_pointer",
            ],
            "projection_limit": "This is not a full-state equivalence relation; camera, objects, action timers, and other RAM are omitted",
        },
        "outcomes": {
            "baseline_y": args.baseline_y,
            "largest_baseline_to_final_y_delta": ranked_delta[0][1],
            "largest_delta_candidate": ranked_delta[0][0],
            "large_rise_threshold": args.large_rise,
            "large_rise_count": len(large_rises),
            "large_rises": large_rises[:100],
            "top_contact_candidate_count": sum(
                int(result["top_contact_updates"]) > 0 for result in results
            ),
            "exited_ttc_count": sum(
                str(result["exited_ttc"]).lower() == "true" for result in results
            ),
            "nonfinite_field_count": len(nonfinite_fields),
            "nonfinite_fields": nonfinite_fields[:100],
            "projected_final_state_count": len(projected_states),
            "final_action_histogram": dict(sorted(actions.items())),
            "final_floor_histogram": dict(sorted(floors.items())),
            "final_level_histogram": {
                str(level): count for level, count in sorted(levels.items())
            },
            "top_baseline_to_final_delta": [
                {**result, "baseline_to_final_y_delta": delta}
                for result, delta in ranked_delta[:100]
            ],
            "top_nearest": campaign["ranked_nearest"][:100],
        },
        "integrity": {
            "missing_ids": campaign["missing_ids"],
            "duplicate_results": campaign["duplicate_results"],
            "worker_failures": [
                worker
                for worker in campaign["workers"]
                if worker["returncode"] != 0
                or worker["timed_out"]
                or worker["done"] is None
            ],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        f"complete={summary['complete']} candidates={len(results)} "
        f"projected_states={len(projected_states)} "
        f"largest_delta={ranked_delta[0][1]:.9g} "
        f"large_rises={len(large_rises)} output={args.output}"
    )
    if not summary["complete"] or summary["integrity"]["worker_failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
