from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from shipment_sync.cma_cgm_comparison import (
    CmaCgmComparisonSettings,
    run_cma_cgm_comparison_from_clickup,
)
from shipment_sync.config import Settings
from shipment_sync.terminal import terminal_safe_text


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare CMA's existing API projection with validated DCSA TNT 2.2 events. "
            "The command never writes to ClickUp."
        )
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Read CMA shipment inventory and official API events, then emit a redacted comparison report.",
    )
    args = parser.parse_args()
    load_dotenv(override=False)

    if not args.run:
        settings = CmaCgmComparisonSettings.from_env(require_enabled=False)
        print(
            json.dumps(
                {
                    "mode": "config-check",
                    "enabled": settings.enabled,
                    "max_shipments": settings.max_shipments,
                    "clickup_writes": False,
                    "scheduler_changes": False,
                },
                sort_keys=True,
            )
        )
        return

    comparison_settings = CmaCgmComparisonSettings.from_env(require_enabled=True)
    summary = run_cma_cgm_comparison_from_clickup(
        settings=Settings.from_env(),
        comparison_settings=comparison_settings,
    )
    print(json.dumps(summary.as_dict(), sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except ValueError as exc:
        raise SystemExit(terminal_safe_text(exc)) from exc
