#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


FAILURE_STATUSES = {"executor_error", "unknown_requires_reconciliation", "duplicate_submitted"}


def load_events(path: Path):
    if not path.exists():
        return []
    events = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"malformed JSON at {path}:{line_number}: {exc}") from exc
    return events


def main():
    parser = argparse.ArgumentParser(description="Generate a fail-closed paper-soak report")
    parser.add_argument("--orders", type=Path, required=True)
    parser.add_argument("--minimum-orders", type=int, required=True)
    parser.add_argument("--minimum-days", type=float, required=True)
    parser.add_argument("--started-at", required=True, help="ISO-8601 UTC timestamp recorded before soak")
    args = parser.parse_args()
    started = datetime.fromisoformat(args.started_at.replace("Z", "+00:00"))
    elapsed_days = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds() / 86400
    events = load_events(args.orders)
    failures = [event for event in events if event.get("status") in FAILURE_STATUSES]
    passed = len(events) >= args.minimum_orders and elapsed_days >= args.minimum_days and not failures
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started.isoformat(),
        "elapsed_days": round(elapsed_days, 3),
        "minimum_days": args.minimum_days,
        "order_events": len(events),
        "minimum_orders": args.minimum_orders,
        "safety_failures": len(failures),
        "passed": passed,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
