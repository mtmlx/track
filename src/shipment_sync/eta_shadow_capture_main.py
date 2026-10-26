"""Explicitly gated, small-scope capture; never invokes the operational sync runner."""
import argparse
import json
import os

from shipment_sync.config import Settings
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.eta_shadow_capture import EtaShadowCapture, build_observation_store


def main():
    parser = argparse.ArgumentParser(description="ONE/CMA ETA evidence pilot; no ClickUp writes or schedules")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--max-shipments", type=int, default=2)
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"mode": "config-check", "clickup_writes": False, "pilot_carriers": ["ONE", "CMA CGM"]}))
        return
    if (os.getenv("ETA_SHADOW_ENABLED") != "true" or not args.run_id or not args.task_id
            or not 1 <= args.max_shipments <= 10):
        parser.error("Enable ETA_SHADOW_ENABLED=true and supply run-id, task-id allowlist and limit 1..10")
    settings = Settings.from_env()
    result = EtaShadowCapture(build_observation_store(), terminal_statuses=settings.shipment_terminal_statuses).run(
        ClickUpClient(settings).get_shipments_by_task_ids(args.task_id), run_id=args.run_id,
        allowed_task_ids=set(args.task_id), max_shipments=args.max_shipments)
    print(json.dumps(result, sort_keys=True))
    if result["failures"] or result["needs_review"] or result["selected"] != len(set(args.task_id)):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
