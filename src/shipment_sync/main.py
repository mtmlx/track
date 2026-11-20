import argparse
import sys

from dotenv import load_dotenv
import requests

from shipment_sync.carriers.registry import build_carrier_registry
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.config import Settings
from shipment_sync.date_utils import format_port_local_time
from shipment_sync.models import ShipmentRef, ShipmentUpdatePlan
from shipment_sync.scheduled_movement_archive import publish_read_only, read_only_scope
from shipment_sync.sync import run_sync
from shipment_sync.terminal import terminal_safe_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync shipment status to ClickUp")
    parser.add_argument("--dry-run", action="store_true", help="List shipments without updating")
    parser.add_argument(
        "--preview-updates",
        action="store_true",
        help="Fetch carrier data and print the exact field/comment updates without writing to ClickUp.",
    )
    parser.add_argument("--task-id", help="Filter to one ClickUp task ID.")
    parser.add_argument("--booking", help="Filter to one booking number.")
    parser.add_argument("--container", help="Filter to one container number.")
    args = parser.parse_args()

    load_dotenv()
    try:
        settings = Settings.from_env()
        client = ClickUpClient(settings)
        shipments = _filter_shipments(
            client.list_shipments(),
            task_id=args.task_id,
            booking=args.booking,
            container=args.container,
        )

        if args.dry_run:
            print(f"Found {len(shipments)} candidate shipment tasks")

            by_list: dict[str, int] = {}
            for s in shipments:
                label = f"{s.list_name} ({s.list_id})" if s.list_name else s.list_id
                by_list[label] = by_list.get(label, 0) + 1
                print(
                    terminal_safe_text(
                        f"- [{label}] {s.task_name} | line={s.shipping_line} | "
                        f"booking={s.booking_no} | container={s.container_no}"
                    )
                )

            if by_list:
                print("Candidates by list:")
                for label, count in sorted(by_list.items()):
                    print(terminal_safe_text(f"- {label}: {count}"))
            return

        if args.preview_updates:
            _preview_updates(client, shipments)
            return

        read_only_lists, carrier = read_only_scope()
        if read_only_lists:
            # Keep these lists outside the existing ClickUp writer, including
            # when the archive itself is unavailable or incomplete.
            shipments = [item for item in shipments if str(item.list_id) not in read_only_lists]
            if not (args.task_id or args.booking or args.container):
                try:
                    publish_read_only(client.get_shipments_by_task_ids, read_only_lists, carrier,
                                      settings.shipment_terminal_statuses)
                except Exception as exc:
                    print(f"Scheduled movement archive unavailable ({type(exc).__name__}).", file=sys.stderr)
        stats = run_sync(client, shipments=shipments)
        updated_items = stats.updated_items
        skipped = stats.skipped
        print(
            f"Sync complete. Candidates: {stats.total_candidates}, Updated: {len(updated_items)}, "
            f"Unchanged: {stats.unchanged}, skipped: {skipped}"
        )

        if stats.candidates_by_list:
            print("Candidates by list:")
            for label, count in sorted(stats.candidates_by_list.items()):
                print(terminal_safe_text(f"- {label}: {count}"))

        if stats.updated_by_list:
            print("Updated by list:")
            for label, count in sorted(stats.updated_by_list.items()):
                print(terminal_safe_text(f"- {label}: {count}"))

        if stats.unchanged_by_list:
            print("Unchanged by list:")
            for label, count in sorted(stats.unchanged_by_list.items()):
                print(terminal_safe_text(f"- {label}: {count}"))

        if updated_items:
            print("Updated shipments:")
            for item in updated_items:
                eta_text = _format_port_local(item.eta_local_text, item.eta_time)
                label = f"{item.list_name} ({item.list_id})" if item.list_name else item.list_id
                if settings.eta_only_mode:
                    latest_move_bits: list[str] = []
                    if item.latest_move_name:
                        latest_move_bits.append(f"latest_move={item.latest_move_name}")
                    if item.latest_move_time_local_text:
                        latest_move_bits.append(
                            f"latest_move_time={_format_port_local(item.latest_move_time_local_text, None)}"
                        )
                    if item.latest_move_location:
                        latest_move_bits.append(f"latest_move_location={item.latest_move_location}")
                    if item.vessel_voyage:
                        latest_move_bits.append(f"vessel_voyage={item.vessel_voyage}")
                    latest_move_suffix = f" | {' | '.join(latest_move_bits)}" if latest_move_bits else ""
                    print(
                        terminal_safe_text(
                            f"- [{label}] {item.task_name} | line={item.shipping_line} | "
                            f"booking={item.booking_no} | container={item.container_no} | "
                            f"eta_local={eta_text}{latest_move_suffix}"
                        )
                    )
                else:
                    time_text = item.event_time.date().isoformat() if item.event_time else "n/a"
                    movement_text = item.movement_details if item.movement_details else "n/a"
                    location_text = item.location if item.location else "n/a"
                    print(
                        terminal_safe_text(
                            f"- [{label}] {item.task_name} | line={item.shipping_line} | "
                            f"booking={item.booking_no} | container={item.container_no} | "
                            f"status={item.status_text} | location={location_text} | "
                            f"last_move_time={time_text} | eta_local={eta_text} | "
                            f"last_move={movement_text}"
                        )
                    )
    except requests.HTTPError as exc:
        _print_http_error(exc)
        raise SystemExit(1) from exc
    except requests.RequestException as exc:
        print(terminal_safe_text(f"Network request failed: {exc}"), file=sys.stderr)
        raise SystemExit(1) from exc
    except ValueError as exc:
        print(terminal_safe_text(exc), file=sys.stderr)
        raise SystemExit(1) from exc


def _filter_shipments(
    shipments: list[ShipmentRef],
    *,
    task_id: str | None,
    booking: str | None,
    container: str | None,
) -> list[ShipmentRef]:
    task_id_norm = (task_id or "").strip()
    booking_norm = (booking or "").strip().lower()
    container_norm = (container or "").strip().lower()
    if not any([task_id_norm, booking_norm, container_norm]):
        return shipments

    filtered: list[ShipmentRef] = []
    for shipment in shipments:
        if task_id_norm and shipment.task_id != task_id_norm:
            continue
        if booking_norm and (shipment.booking_no or "").strip().lower() != booking_norm:
            continue
        if container_norm and (shipment.container_no or "").strip().lower() != container_norm:
            continue
        filtered.append(shipment)
    return filtered


def _preview_updates(client: ClickUpClient, shipments: list[ShipmentRef]) -> None:
    if not shipments:
        print("No shipment matches the selected filter.")
        return

    adapters = build_carrier_registry()
    print(f"Previewing {len(shipments)} shipment(s)")
    for shipment in shipments:
        adapter = adapters.get(shipment.shipping_line)
        if adapter is None:
            print(
                terminal_safe_text(
                    f"- {shipment.task_name} | task={shipment.task_id} | line={shipment.shipping_line} | "
                    "no adapter registered"
                )
            )
            continue

        try:
            status = adapter.fetch_status(shipment)
            plan = client.plan_shipment_update(shipment, status)
            _print_preview_plan(shipment, plan)
        except Exception as exc:
            print(
                terminal_safe_text(
                    f"- {shipment.task_name} | task={shipment.task_id} | line={shipment.shipping_line} | "
                    f"skipped: {exc}"
                )
            )


def _print_preview_plan(shipment: ShipmentRef, plan: ShipmentUpdatePlan) -> None:
    label = f"{shipment.list_name} ({shipment.list_id})" if shipment.list_name else shipment.list_id
    print(
        terminal_safe_text(
            f"- [{label}] {shipment.task_name} | task={shipment.task_id} | line={shipment.shipping_line} | "
            f"booking={shipment.booking_no} | container={shipment.container_no} | changed={plan.changed}"
        )
    )
    if plan.custom_field_updates:
        print("  Planned field writes:")
        for update in plan.custom_field_updates:
            value = update.value.isoformat() if hasattr(update.value, "isoformat") else str(update.value)
            label = update.label or update.field_id
            print(terminal_safe_text(f"  - {label}: {value} [{update.field_id}]"))
    else:
        print("  Planned field writes: none")

    if plan.task_status_update:
        print(terminal_safe_text(f"  Planned task status: {plan.task_status_update}"))
    if plan.comment_text:
        print("  Planned comment:")
        for line in plan.comment_text.splitlines():
            print(terminal_safe_text(f"    {line}"))
    else:
        print("  Planned comment: none")


def _format_port_local(local_text: str | None, event_time) -> str:
    return format_port_local_time(local_text, event_time)


def _print_http_error(exc: requests.HTTPError) -> None:
    response = exc.response
    if response is None:
        print(terminal_safe_text(f"HTTP request failed: {exc}"), file=sys.stderr)
        return

    url = response.url or "unknown URL"
    status_code = response.status_code
    print(terminal_safe_text(f"HTTP {status_code} while calling {url}"), file=sys.stderr)

    if status_code == 401 and "api.clickup.com" in url:
        print("ClickUp authorization failed.", file=sys.stderr)
        print(
            "Check CLICKUP_OAUTH_ACCESS_TOKEN or CLICKUP_API_TOKEN in .env "
            "and confirm that credential can access the configured list/space.",
            file=sys.stderr,
        )
        print("If this computer should only use a specific list, set CLICKUP_DISCOVER_LISTS_FROM_SPACES=false.", file=sys.stderr)
        return

    snippet = (response.text or "").strip().replace("\n", " ")[:240]
    if snippet:
        print(terminal_safe_text(f"Response snippet: {snippet}"), file=sys.stderr)


if __name__ == "__main__":
    main()
