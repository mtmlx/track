from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from shipment_sync.config import Settings
from shipment_sync.dcsa_event_ledger import build_dcsa_event_ledger_from_env
from shipment_sync.dcsa_shadow import DcsaShadowSettings, run_dcsa_shadow_from_clickup
from shipment_sync.terminal import terminal_safe_text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the non-projecting CMA/Maersk DCSA shadow lane. It never writes to ClickUp."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Read the existing shipment inventory, call official carrier APIs, and record only validated event evidence.",
    )
    args = parser.parse_args()
    load_dotenv(override=False)

    if not args.run:
        settings = DcsaShadowSettings.from_env(require_enabled=False)
        print(
            json.dumps(
                {
                    "mode": "config-check",
                    "enabled": settings.enabled,
                    "carrier_versions": settings.carrier_versions,
                    "max_shipments": settings.max_shipments,
                    "clickup_writes": False,
                },
                sort_keys=True,
            )
        )
        return

    shadow_settings = DcsaShadowSettings.from_env(require_enabled=True)
    summary = run_dcsa_shadow_from_clickup(
        settings=Settings.from_env(),
        shadow_settings=shadow_settings,
        ledger=build_dcsa_event_ledger_from_env(),
    )
    print(json.dumps(summary.as_dict(), sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except ValueError as exc:
        raise SystemExit(terminal_safe_text(exc)) from exc
