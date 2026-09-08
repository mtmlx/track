from __future__ import annotations

from .spreadsheet import spreadsheet_safe_cell as _spreadsheet_safe_cell

import argparse
import csv
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
import requests

from .project_paths import artifacts_output_path

BASE_URL = "https://api.clickup.com/api/v2"
SPACE_REF_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SPACE_PATH_PATTERNS = (
    re.compile(r"/v/s/([A-Za-z0-9_-]+)"),
    re.compile(r"/s/([A-Za-z0-9_-]+)"),
    re.compile(r"/space/([A-Za-z0-9_-]+)"),
)


@dataclass(frozen=True)
class ListLocation:
    list_id: str
    list_name: str
    space_id: str
    space_name: str
    folder_id: str | None
    folder_name: str | None


@dataclass
class FieldAggregate:
    field_id: str
    names: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    required_values: set[str] = field(default_factory=set)
    hide_from_guests_values: set[str] = field(default_factory=set)
    type_config_jsons: set[str] = field(default_factory=set)
    locations_by_list_id: dict[str, ListLocation] = field(default_factory=dict)

    def add_occurrence(self, field_payload: dict[str, Any], location: ListLocation) -> None:
        name = str(field_payload.get("name") or "").strip()
        if name:
            self.names.add(name)

        field_type = str(field_payload.get("type") or "").strip()
        if field_type:
            self.types.add(field_type)

        if "required" in field_payload:
            self.required_values.add(_normalize_flag(field_payload.get("required")))
        if "hide_from_guests" in field_payload:
            self.hide_from_guests_values.add(_normalize_flag(field_payload.get("hide_from_guests")))

        if "type_config" in field_payload:
            self.type_config_jsons.add(_to_compact_json(field_payload.get("type_config")))

        self.locations_by_list_id[location.list_id] = location


@dataclass(frozen=True)
class ScopeEntry:
    applies_to: str
    location_id: str
    location_name: str
    list_ids: list[str]


class ClickUpInventoryClient:
    def __init__(self, token: str, *, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": token,
                "Content-Type": "application/json",
            }
        )

    def get_space(self, space_id: str) -> dict[str, Any]:
        return self._get(f"/space/{space_id}")

    def list_space_folders(self, space_id: str) -> list[dict[str, Any]]:
        payload = self._get(f"/space/{space_id}/folder")
        folders = payload.get("folders")
        return [f for f in folders if isinstance(f, dict)] if isinstance(folders, list) else []

    def list_folder_lists(self, folder_id: str) -> list[dict[str, Any]]:
        payload = self._get(f"/folder/{folder_id}/list")
        lists = payload.get("lists")
        return [l for l in lists if isinstance(l, dict)] if isinstance(lists, list) else []

    def list_space_folderless_lists(self, space_id: str) -> list[dict[str, Any]]:
        payload = self._get(f"/space/{space_id}/list")
        lists = payload.get("lists")
        return [l for l in lists if isinstance(l, dict)] if isinstance(lists, list) else []

    def list_list_fields(self, list_id: str) -> list[dict[str, Any]]:
        payload = self._get(f"/list/{list_id}/field")
        fields = payload.get("fields")
        return [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []

    def _get(self, path: str) -> dict[str, Any]:
        response = self.session.get(f"{BASE_URL}{path}", timeout=self.timeout_seconds)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            body = response.text.strip().replace("\n", " ")
            if len(body) > 280:
                body = f"{body[:280]}..."
            raise RuntimeError(
                f"ClickUp API request failed for GET {path}: "
                f"status={response.status_code}, body={body or '<empty>'}"
            ) from exc
        payload = response.json()
        return payload if isinstance(payload, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export deduplicated custom field metadata for all Lists under one or more ClickUp Spaces "
            "(Space refs can be IDs or Space URLs)."
        )
    )
    parser.add_argument(
        "--space",
        action="append",
        required=True,
        help="Space ID or Space URL. Repeat for multiple spaces.",
    )
    parser.add_argument("--token", help="ClickUp API token. Defaults to CLICKUP_API_TOKEN from env/.env.")
    parser.add_argument(
        "--output-csv",
        default=str(artifacts_output_path("spreadsheet", "clickup_custom_fields_normalized.csv")),
        help="Path for normalized deduplicated CSV.",
    )
    parser.add_argument(
        "--details-csv",
        help="Optional path for a per-list field occurrence CSV.",
    )
    parser.add_argument(
        "--xlsx-output",
        help="Optional path for normalized XLSX export (requires openpyxl).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=30,
        help="HTTP timeout for ClickUp API requests (default: 30).",
    )
    args = parser.parse_args()

    load_dotenv()
    token = (args.token or os.getenv("CLICKUP_API_TOKEN") or "").strip()
    if not token:
        raise ValueError("Missing ClickUp token. Set CLICKUP_API_TOKEN or pass --token.")

    if args.timeout_seconds < 1:
        raise ValueError("--timeout-seconds must be >= 1")

    resolved_spaces = _resolve_space_refs(args.space)
    print("Resolved Space references:")
    for original, space_id in resolved_spaces:
        print(f"- {original} -> {space_id}")

    client = ClickUpInventoryClient(token, timeout_seconds=args.timeout_seconds)

    unique_space_ids = list(dict.fromkeys([space_id for _, space_id in resolved_spaces]))
    space_names: dict[str, str] = {}
    list_locations: dict[str, ListLocation] = {}
    all_lists_by_space: dict[str, set[str]] = {}
    all_lists_by_folder: dict[tuple[str, str], set[str]] = {}

    for space_id in unique_space_ids:
        space_payload = client.get_space(space_id)
        space_name = str(space_payload.get("name") or "").strip() or f"Space {space_id}"
        space_names[space_id] = space_name
        all_lists_by_space.setdefault(space_id, set())

        _collect_space_lists(
            client=client,
            space_id=space_id,
            space_name=space_name,
            list_locations=list_locations,
            all_lists_by_space=all_lists_by_space,
            all_lists_by_folder=all_lists_by_folder,
        )

    print(
        f"Discovered {len(list_locations)} unique list(s) across {len(unique_space_ids)} space(s). "
        "Fetching custom fields..."
    )

    aggregates: dict[str, FieldAggregate] = {}
    detailed_rows: list[dict[str, str]] = []
    total_field_occurrences = 0

    for location in sorted(
        list_locations.values(),
        key=lambda item: (
            item.space_name.lower(),
            (item.folder_name or "").lower(),
            item.list_name.lower(),
            item.list_id,
        ),
    ):
        fields = client.list_list_fields(location.list_id)
        for field_payload in fields:
            field_id = str(field_payload.get("id") or "").strip()
            if not field_id:
                continue
            total_field_occurrences += 1
            aggregate = aggregates.setdefault(field_id, FieldAggregate(field_id=field_id))
            aggregate.add_occurrence(field_payload, location)
            detailed_rows.append(_build_detailed_row(field_payload, location))

    normalized_rows = _build_normalized_rows(
        aggregates=aggregates,
        all_lists_by_space=all_lists_by_space,
        all_lists_by_folder=all_lists_by_folder,
    )

    _write_csv(args.output_csv, normalized_rows, headers=NORMALIZED_HEADERS)
    print(f"Wrote normalized CSV: {Path(args.output_csv).resolve()}")

    if args.details_csv:
        _write_csv(args.details_csv, detailed_rows, headers=DETAILED_HEADERS)
        print(f"Wrote per-list details CSV: {Path(args.details_csv).resolve()}")

    if args.xlsx_output:
        _write_xlsx(args.xlsx_output, normalized_rows, headers=NORMALIZED_HEADERS)
        print(f"Wrote normalized XLSX: {Path(args.xlsx_output).resolve()}")

    print(
        "Done. "
        f"Unique fields={len(aggregates)}, "
        f"field occurrences={total_field_occurrences}, "
        f"spaces={len(unique_space_ids)}, "
        f"lists={len(list_locations)}."
    )


def _resolve_space_refs(space_refs: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in space_refs:
        cleaned = raw.strip()
        if not cleaned:
            continue
        out.append((cleaned, _extract_space_id(cleaned)))
    if not out:
        raise ValueError("At least one non-empty --space value is required.")
    return out


def _extract_space_id(space_ref: str) -> str:
    if SPACE_REF_RE.fullmatch(space_ref):
        return space_ref

    parsed = urlparse(space_ref)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            f"Could not parse Space reference: {space_ref}. "
            "Use a Space ID or a full URL like https://app.clickup.com/.../v/s/<space_id>"
        )

    for pattern in SPACE_PATH_PATTERNS:
        match = pattern.search(parsed.path)
        if match:
            return match.group(1)

    query = parse_qs(parsed.query)
    for key in ("space", "space_id", "sid"):
        values = query.get(key)
        if not values:
            continue
        candidate = values[0].strip()
        if SPACE_REF_RE.fullmatch(candidate):
            return candidate

    raise ValueError(
        f"Could not extract a Space ID from URL: {space_ref}. "
        "Expected path containing /v/s/<space_id>."
    )


def _collect_space_lists(
    *,
    client: ClickUpInventoryClient,
    space_id: str,
    space_name: str,
    list_locations: dict[str, ListLocation],
    all_lists_by_space: dict[str, set[str]],
    all_lists_by_folder: dict[tuple[str, str], set[str]],
) -> None:
    for raw_list in client.list_space_folderless_lists(space_id):
        _register_list(
            raw_list=raw_list,
            space_id=space_id,
            space_name=space_name,
            folder_id=None,
            folder_name=None,
            list_locations=list_locations,
            all_lists_by_space=all_lists_by_space,
            all_lists_by_folder=all_lists_by_folder,
        )

    for raw_folder in client.list_space_folders(space_id):
        folder_id = str(raw_folder.get("id") or "").strip()
        if not folder_id:
            continue
        folder_name = str(raw_folder.get("name") or "").strip() or f"Folder {folder_id}"
        for raw_list in client.list_folder_lists(folder_id):
            _register_list(
                raw_list=raw_list,
                space_id=space_id,
                space_name=space_name,
                folder_id=folder_id,
                folder_name=folder_name,
                list_locations=list_locations,
                all_lists_by_space=all_lists_by_space,
                all_lists_by_folder=all_lists_by_folder,
            )


def _register_list(
    *,
    raw_list: dict[str, Any],
    space_id: str,
    space_name: str,
    folder_id: str | None,
    folder_name: str | None,
    list_locations: dict[str, ListLocation],
    all_lists_by_space: dict[str, set[str]],
    all_lists_by_folder: dict[tuple[str, str], set[str]],
) -> None:
    list_id = str(raw_list.get("id") or "").strip()
    if not list_id:
        return

    list_name = str(raw_list.get("name") or "").strip() or f"List {list_id}"
    existing = list_locations.get(list_id)
    if existing and existing.folder_id and not folder_id:
        folder_id = existing.folder_id
        folder_name = existing.folder_name

    list_locations[list_id] = ListLocation(
        list_id=list_id,
        list_name=list_name,
        space_id=space_id,
        space_name=space_name,
        folder_id=folder_id,
        folder_name=folder_name,
    )
    all_lists_by_space.setdefault(space_id, set()).add(list_id)
    if folder_id:
        all_lists_by_folder.setdefault((space_id, folder_id), set()).add(list_id)


def _build_detailed_row(field_payload: dict[str, Any], location: ListLocation) -> dict[str, str]:
    return {
        "field_id": str(field_payload.get("id") or "").strip(),
        "field_name": str(field_payload.get("name") or "").strip(),
        "field_type": str(field_payload.get("type") or "").strip(),
        "required": _normalize_flag(field_payload.get("required")),
        "hide_from_guests": _normalize_flag(field_payload.get("hide_from_guests")),
        "type_config_json": _to_compact_json(field_payload.get("type_config")),
        "space_id": location.space_id,
        "space_name": location.space_name,
        "folder_id": location.folder_id or "",
        "folder_name": location.folder_name or "",
        "list_id": location.list_id,
        "list_name": location.list_name,
    }


def _build_normalized_rows(
    *,
    aggregates: dict[str, FieldAggregate],
    all_lists_by_space: dict[str, set[str]],
    all_lists_by_folder: dict[tuple[str, str], set[str]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    sorted_aggregates = sorted(
        aggregates.values(),
        key=lambda item: (
            min(item.names).lower() if item.names else "",
            item.field_id,
        ),
    )

    for aggregate in sorted_aggregates:
        locations = list(aggregate.locations_by_list_id.values())
        list_ids_seen = set(aggregate.locations_by_list_id.keys())
        list_names_by_id = {loc.list_id: loc.list_name for loc in locations}

        seen_by_space: dict[str, set[str]] = {}
        seen_by_folder: dict[tuple[str, str], set[str]] = {}
        for location in locations:
            seen_by_space.setdefault(location.space_id, set()).add(location.list_id)
            if location.folder_id:
                seen_by_folder.setdefault((location.space_id, location.folder_id), set()).add(location.list_id)

        covered_lists: set[str] = set()
        scope_entries: list[ScopeEntry] = []

        for space_id in sorted(seen_by_space.keys()):
            full_space_lists = all_lists_by_space.get(space_id, set())
            if not full_space_lists:
                continue
            if seen_by_space[space_id] >= full_space_lists:
                any_loc = next(loc for loc in locations if loc.space_id == space_id)
                scope_entries.append(
                    ScopeEntry(
                        applies_to="Space",
                        location_id=space_id,
                        location_name=any_loc.space_name,
                        list_ids=sorted(full_space_lists),
                    )
                )
                covered_lists.update(full_space_lists)

        for space_id, folder_id in sorted(seen_by_folder.keys()):
            all_folder_lists = all_lists_by_folder.get((space_id, folder_id), set())
            if not all_folder_lists:
                continue
            candidate_lists = all_folder_lists - covered_lists
            if not candidate_lists:
                continue
            if seen_by_folder[(space_id, folder_id)] >= candidate_lists:
                any_loc = next(
                    loc for loc in locations if loc.space_id == space_id and loc.folder_id == folder_id
                )
                scope_entries.append(
                    ScopeEntry(
                        applies_to="Folder",
                        location_id=folder_id,
                        location_name=any_loc.folder_name or f"Folder {folder_id}",
                        list_ids=sorted(candidate_lists),
                    )
                )
                covered_lists.update(candidate_lists)

        remaining_lists = sorted(list_ids_seen - covered_lists)
        for list_id in remaining_lists:
            location = aggregate.locations_by_list_id[list_id]
            scope_entries.append(
                ScopeEntry(
                    applies_to="List",
                    location_id=list_id,
                    location_name=location.list_name,
                    list_ids=[list_id],
                )
            )

        all_space_ids = sorted({location.space_id for location in locations})
        all_space_names = sorted({location.space_name for location in locations})
        all_folder_ids = sorted({location.folder_id for location in locations if location.folder_id})
        all_folder_names = sorted({location.folder_name for location in locations if location.folder_name})
        all_list_ids = sorted(list_ids_seen)
        all_list_names = _sort_names(list_names_by_id, set(all_list_ids))

        scope_entries = sorted(
            scope_entries,
            key=lambda item: (
                _scope_order(item.applies_to),
                item.location_name.lower(),
                item.location_id,
            ),
        )
        scope_levels = []
        for scope in scope_entries:
            if scope.applies_to not in scope_levels:
                scope_levels.append(scope.applies_to)

        rows.append(
            {
                "field_id": aggregate.field_id,
                "field_name": _first_sorted(aggregate.names),
                "field_name_variants": _join_sorted(aggregate.names, sep=" | "),
                "field_type": _first_sorted(aggregate.types),
                "field_type_variants": _join_sorted(aggregate.types, sep=" | "),
                "required": _join_sorted(aggregate.required_values, sep=" | "),
                "hide_from_guests": _join_sorted(aggregate.hide_from_guests_values, sep=" | "),
                "type_config_json": _join_sorted(aggregate.type_config_jsons, sep=" | "),
                "applies_to": " | ".join(scope_levels),
                "location_id": ";".join(
                    [f"{scope.applies_to}:{scope.location_id}" for scope in scope_entries]
                ),
                "location_name": ";".join(
                    [f"{scope.applies_to}:{scope.location_name}" for scope in scope_entries]
                ),
                "space_ids": ";".join(all_space_ids),
                "space_names": ";".join(all_space_names),
                "folder_ids": ";".join(all_folder_ids),
                "folder_names": ";".join(all_folder_names),
                "list_count": str(len(all_list_ids)),
                "list_ids": ";".join(all_list_ids),
                "list_names": ";".join(all_list_names),
            }
        )

    return rows


def _scope_order(scope: str) -> int:
    if scope == "Space":
        return 0
    if scope == "Folder":
        return 1
    return 2


def _sort_names(name_map: dict[str, str], list_ids: set[str]) -> list[str]:
    return [name_map[list_id] for list_id in sorted(list_ids, key=lambda lid: name_map[lid].lower())]


def _write_csv(path_text: str, rows: list[dict[str, str]], *, headers: list[str]) -> None:
    path = Path(path_text).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({header: _spreadsheet_safe_cell(row.get(header, "")) for header in headers})


def _write_xlsx(path_text: str, rows: list[dict[str, str]], *, headers: list[str]) -> None:
    try:
        from openpyxl import Workbook
    except Exception as exc:
        raise RuntimeError(
            "XLSX export requires openpyxl. Install it with: pip install openpyxl"
        ) from exc

    path = Path(path_text).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "fields"
    sheet.append(headers)
    for row in rows:
        sheet.append([_spreadsheet_safe_cell(row.get(header, "")) for header in headers])
    workbook.save(path)


def _normalize_flag(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().lower()


def _to_compact_json(value: Any) -> str:
    if value is None:
        return ""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return str(value)


def _join_sorted(values: set[str], *, sep: str) -> str:
    return sep.join(sorted([value for value in values if value]))


def _first_sorted(values: set[str]) -> str:
    filtered = sorted([value for value in values if value])
    return filtered[0] if filtered else ""


NORMALIZED_HEADERS = [
    "field_id",
    "field_name",
    "field_name_variants",
    "field_type",
    "field_type_variants",
    "required",
    "hide_from_guests",
    "type_config_json",
    "applies_to",
    "location_id",
    "location_name",
    "space_ids",
    "space_names",
    "folder_ids",
    "folder_names",
    "list_count",
    "list_ids",
    "list_names",
]

DETAILED_HEADERS = [
    "field_id",
    "field_name",
    "field_type",
    "required",
    "hide_from_guests",
    "type_config_json",
    "space_id",
    "space_name",
    "folder_id",
    "folder_name",
    "list_id",
    "list_name",
]


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
