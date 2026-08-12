#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def expected_ids(analog_classes: int, button_classes: int) -> set[str]:
    return {
        f"c{analog:05}_b{button:02}"
        for analog in range(analog_classes)
        for button in range(button_classes)
    }


def validate(
    aggregate: Path,
    expected_count: int,
    analog_classes: int,
    button_classes: int,
    workers: int,
) -> dict[str, Any]:
    data = json.loads(aggregate.read_text())
    rows = data.get("all_results")
    if not isinstance(rows, list):
        raise ValueError("aggregate all_results must be a list")
    worker_rows = data.get("workers")
    if not isinstance(worker_rows, list):
        raise ValueError("aggregate workers must be a list")

    ids = [str(row.get("id")) for row in rows]
    expected = expected_ids(analog_classes, button_classes)
    observed = set(ids)
    checks = {
        "complete": data.get("complete") is True,
        "candidate_count": data.get("candidate_count") == expected_count,
        "result_count": data.get("result_count") == expected_count,
        "result_row_count": len(rows) == expected_count,
        "unique_result_ids": len(observed) == expected_count,
        "exact_id_domain": observed == expected and len(expected) == expected_count,
        "missing_ids_empty": data.get("missing_ids") == [],
        "duplicate_results_zero": data.get("duplicate_results") == 0,
        "worker_count": data.get("worker_count") == workers
        and len(worker_rows) == workers,
        "workers_returncode_zero": all(
            worker.get("returncode") == 0 for worker in worker_rows
        ),
        "workers_not_timed_out": all(
            worker.get("timed_out") is False for worker in worker_rows
        ),
        "workers_done": all(
            isinstance(worker.get("done"), dict) for worker in worker_rows
        ),
        "workers_parsed_expected": all(
            worker.get("parsed") == worker.get("expected") for worker in worker_rows
        ),
        "worker_done_candidates": all(
            worker.get("done", {}).get("candidates") == worker.get("expected")
            for worker in worker_rows
        ),
        "worker_done_restores": all(
            worker.get("done", {}).get("load_requests")
            == max(0, int(worker.get("expected", 0)) - 1)
            for worker in worker_rows
        ),
        "worker_result_sum": sum(worker.get("parsed", 0) for worker in worker_rows)
        == expected_count,
    }
    results = {
        "aggregate": {
            "path": str(aggregate.resolve()),
            "sha256": sha256(aggregate),
            "size_bytes": aggregate.stat().st_size,
        },
        "domain": {
            "expected_count": expected_count,
            "analog_classes": analog_classes,
            "button_classes": button_classes,
            "id_format": "c{analog:05}_b{button:02}",
        },
        "checks": checks,
        "metrics": {
            "maximum_upward_step": max(
                float(row["max_upward_step"]) for row in rows
            ),
            "at_least_500_count": sum(
                float(row["max_upward_step"]) >= 500.0 for row in rows
            ),
            "upper_contact_count": sum(
                int(row["top_contact_updates"]) > 0 for row in rows
            ),
            "worker_count": len(worker_rows),
            "elapsed_seconds": data.get("elapsed_seconds"),
        },
        "valid": all(checks.values()),
    }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate exact controller-campaign aggregate count, ID domain, and worker integrity"
    )
    parser.add_argument("aggregate", type=Path)
    parser.add_argument("--expected-count", type=int, default=196304)
    parser.add_argument("--analog-classes", type=int, default=12269)
    parser.add_argument("--button-classes", type=int, default=16)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    if args.expected_count <= 0:
        raise SystemExit("--expected-count must be positive")
    if args.analog_classes <= 0 or args.button_classes <= 0:
        raise SystemExit("class counts must be positive")

    result = validate(
        args.aggregate,
        args.expected_count,
        args.analog_classes,
        args.button_classes,
        args.workers,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
