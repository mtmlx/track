"""Review operator-transcribed Wan Hai B/L histories; publish comments only."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import re

from .clickup_client import ClickUpClient, _field_map, _field_text, _shipment_destination
from .config import Settings
from .destination import same_port
from .tracking_failures import carrier_name


def render_register(booking, roster, evidence):
    histories = evidence["histories"]
    if (evidence["bl"] != booking or not same_port(evidence["pod"], "GTPRQ")
            or len(roster) < 2 or set(histories) != roster):
        raise ValueError("Wan Hai B/L, destination or full container roster did not match")
    rows = []
    for ref in sorted(roster):
        h = histories[ref]
        if (h["booking"] != booking or h["pickup_facility"] != "Terminal De Contenedores Quetzal"
                or h["empty_facility"] != "Contenedores Y Servicios, S.A."
                or h["pickup_event"] != "Full Container Withdrawn By Consignee From Pier/Terminal"
                or h["empty_event"] != "Empty Container Discharged From Vessel Or Gate In To Pier/Terminal/Off-Dock Depot (Empty Available)"
                or h["empty_at"] != h["bl_status_at"]):
            raise ValueError(f"Wan Hai movement/depot evidence did not match for {ref}")
        gate, returned = (datetime.strptime(h[k], "%Y-%m-%d %H:%M") for k in ("pickup_at", "empty_at"))
        if not gate < returned < datetime.now():
            raise ValueError(f"Wan Hai movement sequence is not historical for {ref}")
        rows.append(f"{ref} | {h['pickup_at']} | {h['empty_at']}")
    n = len(rows)
    return "\n".join([
        f"Wan Hai container movement register | B/L {booking} | Puerto Quetzal", "",
        f"Destination gate-out: {n}/{n}. Empty returned to depot: {n}/{n}, based on the post-pickup depot entries and matching B/L status dates.",
        "", "Container | Full gate-out | Empty depot receipt", *rows, "",
        "Gate-out facility: Terminal de Contenedores Quetzal. Empty depot: Contenedores y Servicios, S.A. Times are as displayed by Wan Hai; no timezone is shown. Wan Hai uses a combined empty-discharge/depot-gate-in label; the depot facility and sequence after consignee pickup support the return interpretation.",
        "", "Source: Wan Hai B/L Data and each linked Container Movement Information List.",
    ])


def review(client, task_id, evidence, *, publish=False):
    if not re.fullmatch(r"[a-z0-9]+", task_id):
        raise ValueError("An exact ClickUp task ID is required")
    url = f"{client.base_url}/task/{task_id}"
    settings = client.settings

    def read():
        response = client._request("get", url, timeout=30)
        response.raise_for_status()
        return response.json()

    def identity(task):
        fields = _field_map(task["custom_fields"])
        return (_field_text(fields.get(settings.cf_shipping_line)),
                _field_text(fields.get(settings.cf_booking_no)),
                _field_text(fields.get(settings.cf_container_no)), _shipment_destination(fields),
                task["status"], fields.get(settings.cf_eta, {}).get("value"))

    before = read()
    line, booking, containers, pod, _, _ = identity(before)
    roster = set(re.findall(r"\b[A-Z]{4}\d{7}\b", (containers or "").upper()))
    from .clickup_client import _expected_container_count

    if (before.get("id") != task_id or before.get("archived") or not booking
            or carrier_name(line or "") != "Wan Hai" or not same_port(pod, "GTPRQ")
            or _expected_container_count(before["custom_fields"]) != len(roster)):
        raise ValueError("ClickUp shipment is not an eligible matching Wan Hai roster")
    report = render_register(booking, roster, evidence)
    if not publish:
        return {"outcome": "preview", "report": report}

    def matches():
        response = client._request("get", url + "/comment", timeout=30)
        response.raise_for_status()
        return [str(c["id"]) for c in response.json()["comments"]
                if (c.get("comment_text") or "").strip() == report]

    saved = matches()
    outcome = "unchanged"
    if not saved:
        if identity(read()) != identity(before):
            raise ValueError("Shipment changed after preview")
        client._post_comment(task_id, report)
        outcome = "published"
        saved = matches()
    if len(saved) != 1 or identity(read()) != identity(before):
        raise ValueError("Register or unchanged shipment fields could not be verified")
    return {"outcome": outcome, "comment_id": saved[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    print(json.dumps(review(ClickUpClient(Settings.from_env()), args.task_id,
                            json.loads(args.evidence.read_text()), publish=args.publish)))


if __name__ == "__main__":
    main()
