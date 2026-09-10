from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from datetime import date, datetime, timezone
import re
import sys
import time
import unicodedata
from typing import Any

import requests

from .carriers.common import extract_container_numbers
from .config import Settings
from .destination import same_port
from .date_utils import format_display_date, format_port_local_time
from .models import (
    MovementEvent,
    ShipmentFieldWrite,
    ShipmentRef,
    ShipmentStatus,
    ShipmentUpdatePlan,
    ShipmentWriteResult,
)


class ClickUpWorkloadLimitError(RuntimeError):
    """A ClickUp inventory exceeded the configured per-run safety budget."""


class ClickUpClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = "https://api.clickup.com/api/v2"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": settings.clickup_auth_header_value,
                "Content-Type": "application/json",
            }
        )
        self._list_field_cache: dict[str, list[dict[str, Any]]] = {}
        self._carrier_filter_warning_lists: set[str] = set()
        self._discovery_warning_keys: set[str] = set()

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        max_retries = _env_int("CLICKUP_REQUEST_MAX_RETRIES", default=4, minimum=0)
        retry_delay_seconds = _env_float("CLICKUP_REQUEST_RETRY_DELAY_SECONDS", default=2.0, minimum=0.0)
        request_method = getattr(self.session, method)
        last_error: requests.RequestException | None = None

        for attempt in range(max_retries + 1):
            try:
                return request_method(url, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                sleep_seconds = min(retry_delay_seconds * (2**attempt), 30.0)
                print(
                    f"ClickUp {method.upper()} {url} failed on attempt {attempt + 1}/{max_retries + 1}: "
                    f"{exc}. Retrying in {sleep_seconds:g}s.",
                    file=sys.stderr,
                )
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

        assert last_error is not None
        raise last_error

    def list_shipments(self, *, require_carrier_prefilter: bool = False) -> list[ShipmentRef]:
        target_lists = self._resolve_target_lists()
        total_lists = len(target_lists)
        if total_lists > self.settings.clickup_max_lists_per_run:
            raise ClickUpWorkloadLimitError(
                "ClickUp target list discovery returned "
                f"{total_lists} lists, exceeding CLICKUP_MAX_LISTS_PER_RUN="
                f"{self.settings.clickup_max_lists_per_run}"
            )
        print(f"Loading ClickUp tasks from {total_lists} list(s)...", file=sys.stderr)

        tasks: list[dict[str, Any]] = []
        seen_task_ids: set[str] = set()

        def add_task_if_open(task: dict[str, Any]) -> None:
            if not _is_open_task(task):
                return
            task_id = str(task.get("id") or "")
            if not task_id or task_id in seen_task_ids:
                return
            if len(tasks) >= self.settings.clickup_max_total_tasks:
                raise ClickUpWorkloadLimitError(
                    "ClickUp shipment inventory exceeds CLICKUP_MAX_TOTAL_TASKS="
                    f"{self.settings.clickup_max_total_tasks}"
                )
            tasks.append(task)
            seen_task_ids.add(task_id)

        for idx, list_id in enumerate(target_lists.keys(), start=1):
            print(f"ClickUp list {idx}/{total_lists}: {list_id}", file=sys.stderr)
            carrier_filter_value = self._shipping_line_filter_value_for_list(list_id)
            if require_carrier_prefilter and carrier_filter_value is None:
                raise ClickUpWorkloadLimitError(
                    "ClickUp carrier prefilter is required for this run but could not be established "
                    f"for list {list_id}."
                )
            open_tasks = self._fetch_tasks(
                list_id=list_id,
                archived=False,
                carrier_filter_value=carrier_filter_value,
                require_carrier_prefilter=require_carrier_prefilter,
            )
            for t in open_tasks:
                add_task_if_open(t)

            if self.settings.clickup_include_archived:
                archived_tasks = self._fetch_tasks(
                    list_id=list_id,
                    archived=True,
                    carrier_filter_value=carrier_filter_value,
                    require_carrier_prefilter=require_carrier_prefilter,
                )
                for t in archived_tasks:
                    add_task_if_open(t)

        shipments: list[ShipmentRef] = []
        for task in tasks:
            fields = _field_map(task.get("custom_fields", []))
            shipping_line = _field_text(fields.get(self.settings.cf_shipping_line))
            booking_no = _field_text(fields.get(self.settings.cf_booking_no))
            container_no = _field_text(fields.get(self.settings.cf_container_no))
            expected_container_count = _expected_container_count(task.get("custom_fields", []))
            current_status_value = (
                _field_text(fields.get(self.settings.cf_shipment_status))
                if self.settings.cf_shipment_status
                else None
            )
            last_checked_at = (
                _parse_last_checked(fields.get(self.settings.cf_status_last_checked))
                if self.settings.cf_status_last_checked
                else None
            )
            track_trace_snapshot_hash = (
                _field_text(fields.get(self.settings.cf_track_trace_snapshot))
                if self.settings.cf_track_trace_snapshot
                else None
            )

            if not shipping_line:
                continue
            reference_hints: list[str] = []
            if _is_wan_hai_line(shipping_line) and self.settings.wan_hai_reference_hints_from_comments:
                reference_hints = self._fetch_wan_hai_reference_hints(str(task.get("id") or ""))
            if not booking_no and not container_no:
                if not reference_hints:
                    continue

            list_obj = task.get("list") if isinstance(task.get("list"), dict) else {}
            list_id = str(list_obj.get("id") or "")
            list_name = list_obj.get("name") if isinstance(list_obj.get("name"), str) else None
            if not list_id:
                list_id = self.settings.clickup_list_id
                list_name = list_name or target_lists.get(list_id)

            shipments.append(
                ShipmentRef(
                    task_id=task["id"],
                    task_name=task.get("name", ""),
                    shipping_line=shipping_line.strip().lower(),
                    booking_no=booking_no,
                    container_no=container_no,
                    list_id=list_id,
                    list_name=list_name,
                    last_checked_at=last_checked_at,
                    current_status_value=current_status_value,
                    current_task_status=_task_status_text(task),
                    track_trace_snapshot_hash=track_trace_snapshot_hash,
                    expected_container_count=expected_container_count,
                    current_field_values={
                        field_id: field_payload.get("value")
                        for field_id, field_payload in fields.items()
                    },
                    reference_hints=reference_hints,
                    destination_port=_shipment_destination(fields),
                )
            )
        print(f"ClickUp candidate shipment tasks: {len(shipments)}", file=sys.stderr)
        return shipments

    def get_shipments_by_task_ids(
        self,
        task_ids: list[str],
        *,
        allowed_list_ids: set[str] | None = None,
    ) -> list[ShipmentRef]:
        """Load an explicitly reviewed set of shipment tasks without list discovery.

        This is used by operator-assisted imports. The task must still belong to
        the configured production list scope and carry the expected tracking
        fields; a batch cannot expand its own authority by naming another task.
        """
        allowed_list_ids = allowed_list_ids or set(self.settings.clickup_list_ids)
        shipments: list[ShipmentRef] = []
        seen_task_ids: set[str] = set()
        for task_id in task_ids:
            task_id = str(task_id or "").strip()
            if not task_id or task_id in seen_task_ids:
                continue
            seen_task_ids.add(task_id)
            response = self._request("get", f"{self.base_url}/task/{task_id}", timeout=30)
            response.raise_for_status()
            task = response.json()
            if not isinstance(task, dict) or not _is_open_task(task):
                continue

            list_obj = task.get("list") if isinstance(task.get("list"), dict) else {}
            list_id = str(list_obj.get("id") or "")
            if list_id not in allowed_list_ids:
                raise ClickUpWorkloadLimitError(
                    f"Task {task_id} is outside the configured Track & Trace list scope."
                )

            fields = _field_map(task.get("custom_fields", []))
            shipping_line = _field_text(fields.get(self.settings.cf_shipping_line))
            booking_no = _field_text(fields.get(self.settings.cf_booking_no))
            container_no = _field_text(fields.get(self.settings.cf_container_no))
            if not shipping_line or (not booking_no and not container_no):
                raise ClickUpWorkloadLimitError(
                    f"Task {task_id} is missing the carrier or a tracking reference."
                )
            shipments.append(
                ShipmentRef(
                    task_id=str(task.get("id") or task_id),
                    task_name=task.get("name", ""),
                    shipping_line=shipping_line.strip().lower(),
                    booking_no=booking_no,
                    container_no=container_no,
                    list_id=list_id,
                    list_name=list_obj.get("name") if isinstance(list_obj.get("name"), str) else None,
                    last_checked_at=(
                        _parse_last_checked(fields.get(self.settings.cf_status_last_checked))
                        if self.settings.cf_status_last_checked
                        else None
                    ),
                    current_status_value=(
                        _field_text(fields.get(self.settings.cf_shipment_status))
                        if self.settings.cf_shipment_status
                        else None
                    ),
                    current_task_status=_task_status_text(task),
                    track_trace_snapshot_hash=(
                        _field_text(fields.get(self.settings.cf_track_trace_snapshot))
                        if self.settings.cf_track_trace_snapshot
                        else None
                    ),
                    expected_container_count=_expected_container_count(task.get("custom_fields", [])),
                    destination_port=_shipment_destination(fields),
                    current_field_values={
                        field_id: field_payload.get("value")
                        for field_id, field_payload in fields.items()
                    },
                )
            )
        print(f"ClickUp explicit shipment tasks: {len(shipments)}", file=sys.stderr)
        return shipments

    def _resolve_target_lists(self) -> dict[str, str]:
        target: dict[str, str] = {lid: None for lid in self.settings.clickup_list_ids}

        for lid, name in self._resolve_discovered_lists().items():
            target[lid] = name

        return {k: (v or "") for k, v in target.items()}

    def _resolve_discovered_lists(self) -> dict[str, str]:
        has_space_discovery = self.settings.clickup_discover_from_spaces and (
            self.settings.clickup_space_ids
            or (self.settings.clickup_discover_from_team and self.settings.clickup_team_id)
        )
        if not self.settings.clickup_folder_ids and not has_space_discovery:
            return {}

        cached = self._load_discovery_cache(allow_stale=False)
        if cached is not None:
            print(f"Using cached ClickUp discovery list set ({len(cached)} list(s)).", file=sys.stderr)
            return cached

        discovered: dict[str, str] = {}

        for folder_id in self.settings.clickup_folder_ids:
            try:
                folder_lists = self._fetch_folder_lists(folder_id)
            except requests.RequestException as exc:
                self._warn_discovery_failure(f"folder {folder_id}", exc)
                continue
            discovered.update(self._filter_discovered_lists(folder_lists, scope=f"folder {folder_id}"))

        discovered_space_ids: list[str] = []
        if self.settings.clickup_discover_from_team and self.settings.clickup_team_id:
            try:
                discovered_space_ids.extend(self._fetch_team_space_ids(self.settings.clickup_team_id))
            except requests.RequestException as exc:
                self._warn_discovery_failure(f"team {self.settings.clickup_team_id}", exc)
        discovered_space_ids.extend(self.settings.clickup_space_ids)
        discovered_space_ids = list(dict.fromkeys([s for s in discovered_space_ids if s]))

        if self.settings.clickup_discover_from_spaces:
            for space_id in discovered_space_ids:
                try:
                    folderless_lists = self._fetch_space_folderless_lists(space_id)
                except requests.RequestException as exc:
                    self._warn_discovery_failure(f"space {space_id}", exc)
                    continue
                discovered.update(self._filter_discovered_lists(folderless_lists, scope=f"space {space_id}"))
                try:
                    space_folders = self._fetch_space_folders(space_id)
                except requests.RequestException as exc:
                    self._warn_discovery_failure(f"space folders {space_id}", exc)
                    continue
                for folder in space_folders:
                    folder_id = str(folder.get("id") or "")
                    if not folder_id:
                        continue
                    try:
                        folder_lists = self._fetch_folder_lists(folder_id)
                    except requests.RequestException as exc:
                        self._warn_discovery_failure(f"folder {folder_id}", exc)
                        continue
                    discovered.update(self._filter_discovered_lists(folder_lists, scope=f"folder {folder_id}"))

        if not discovered:
            stale = self._load_discovery_cache(allow_stale=True)
            if stale is not None:
                print(
                    f"ClickUp discovery returned no eligible lists; using stale cached set ({len(stale)} list(s)).",
                    file=sys.stderr,
                )
                return stale

        self._write_discovery_cache(discovered)
        return discovered

    def _filter_discovered_lists(self, lists: list[dict[str, Any]], *, scope: str) -> dict[str, str]:
        eligible: dict[str, str] = {}
        for lst in lists:
            lid = str(lst.get("id") or "")
            if not lid:
                continue
            name = _name_or_none(lst.get("name")) or ""
            if not self._discovered_list_name_allowed(name):
                self._warn_discovery_skip_once(
                    f"name:{lid}",
                    f"ClickUp discovery skipped list {lid} ({name or 'unnamed'}) from {scope}: name filter did not match.",
                )
                continue
            if self.settings.clickup_discovery_validate_schema and not self._list_has_tracking_schema(lid):
                self._warn_discovery_skip_once(
                    f"schema:{lid}",
                    f"ClickUp discovery skipped list {lid} ({name or 'unnamed'}) from {scope}: missing T&T fields.",
                )
                continue
            self._warn_discovery_skip_once(
                f"accepted:{lid}",
                f"ClickUp discovery accepted list {lid} ({name or 'unnamed'}) from {scope}.",
            )
            eligible[lid] = name
        return eligible

    def _discovered_list_name_allowed(self, name: str) -> bool:
        normalized_name = _normalize_discovery_label(name)
        excluded = self.settings.clickup_discovery_list_name_exclude or []
        for pattern in excluded:
            normalized_pattern = _normalize_discovery_label(pattern)
            if normalized_pattern and normalized_pattern in normalized_name:
                return False

        included = self.settings.clickup_discovery_list_name_include or []
        if not included:
            return True
        return any(
            normalized_pattern in normalized_name
            for pattern in included
            if (normalized_pattern := _normalize_discovery_label(pattern))
        )

    def _list_has_tracking_schema(self, list_id: str) -> bool:
        required_field_ids = [
            self.settings.cf_container_no,
            self.settings.cf_booking_no,
            self.settings.cf_shipping_line,
        ]
        if self.settings.cf_status_last_checked:
            required_field_ids.append(self.settings.cf_status_last_checked)
        required = {field_id for field_id in required_field_ids if field_id}
        if not required:
            return True
        try:
            fields = self._fetch_list_fields(list_id)
        except requests.RequestException as exc:
            self._warn_discovery_failure(f"list fields {list_id}", exc)
            return False
        available = {str(field.get("id") or "") for field in fields if isinstance(field, dict)}
        return required.issubset(available)

    def _load_discovery_cache(self, *, allow_stale: bool) -> dict[str, str] | None:
        path_value = self.settings.clickup_discovery_cache_path
        ttl_seconds = self.settings.clickup_discovery_cache_ttl_seconds
        if not path_value or ttl_seconds <= 0:
            return None
        path = Path(path_value)
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except Exception as exc:
            self._warn_discovery_skip_once(
                "cache-read",
                f"ClickUp discovery cache ignored: {exc}.",
            )
            return None

        generated_at = payload.get("generated_at")
        lists = payload.get("lists")
        if not isinstance(generated_at, (int, float)) or not isinstance(lists, list):
            return None
        if not allow_stale and (time.time() - float(generated_at)) > ttl_seconds:
            return None

        resolved: dict[str, str] = {}
        for item in lists:
            if not isinstance(item, dict):
                continue
            lid = str(item.get("id") or "")
            if not lid:
                continue
            resolved[lid] = _name_or_none(item.get("name")) or ""
        return resolved

    def _write_discovery_cache(self, lists: dict[str, str]) -> None:
        path_value = self.settings.clickup_discovery_cache_path
        if not path_value or self.settings.clickup_discovery_cache_ttl_seconds <= 0:
            return
        path = Path(path_value)
        payload = {
            "generated_at": time.time(),
            "lists": [{"id": lid, "name": name} for lid, name in sorted(lists.items())],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        except Exception as exc:
            self._warn_discovery_skip_once(
                "cache-write",
                f"ClickUp discovery cache write failed: {exc}.",
            )

    def _warn_discovery_skip_once(self, key: str, message: str) -> None:
        if key in self._discovery_warning_keys:
            return
        self._discovery_warning_keys.add(key)
        print(message, file=sys.stderr)

    def _warn_discovery_failure(self, scope: str, exc: requests.RequestException) -> None:
        print(
            f"ClickUp discovery skipped for {scope}: {exc}. "
            f"Continuing with explicit list IDs only.",
            file=sys.stderr,
        )


    def _fetch_team_space_ids(self, team_id: str) -> list[str]:
        url = f"{self.base_url}/team/{team_id}/space"
        response = self._request("get", url, timeout=30)
        response.raise_for_status()
        spaces = response.json().get("spaces", [])
        ids: list[str] = []
        for s in spaces:
            sid = str(s.get("id") or "")
            if sid:
                ids.append(sid)
        return ids

    def _fetch_space_folders(self, space_id: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/space/{space_id}/folder"
        response = self._request("get", url, timeout=30)
        response.raise_for_status()
        return response.json().get("folders", [])

    def _fetch_folder_lists(self, folder_id: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/folder/{folder_id}/list"
        response = self._request("get", url, timeout=30)
        response.raise_for_status()
        return response.json().get("lists", [])

    def _fetch_space_folderless_lists(self, space_id: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/space/{space_id}/list"
        response = self._request("get", url, timeout=30)
        response.raise_for_status()
        return response.json().get("lists", [])

    def _fetch_tasks(
        self,
        list_id: str,
        archived: bool,
        carrier_filter_value: str | int | None = None,
        require_carrier_prefilter: bool = False,
    ) -> list[dict[str, Any]]:
        try:
            return self._fetch_tasks_page_loop(
                list_id=list_id,
                archived=archived,
                carrier_filter_value=carrier_filter_value,
            )
        except requests.RequestException as exc:
            if carrier_filter_value is None or require_carrier_prefilter:
                raise
            if list_id not in self._carrier_filter_warning_lists:
                print(
                    f"ClickUp carrier prefilter failed for list {list_id}: {exc}. "
                    "Retrying without the API-side carrier filter.",
                    file=sys.stderr,
                )
                self._carrier_filter_warning_lists.add(list_id)
            return self._fetch_tasks_page_loop(
                list_id=list_id,
                archived=archived,
                carrier_filter_value=None,
            )

    def _fetch_tasks_page_loop(
        self,
        *,
        list_id: str,
        archived: bool,
        carrier_filter_value: str | int | None,
    ) -> list[dict[str, Any]]:
        url = f"{self.base_url}/list/{list_id}/task"
        base_params = {
            "archived": "true" if archived else "false",
            "subtasks": "true",
            "include_closed": "false",
        }
        if carrier_filter_value is not None:
            base_params["custom_fields"] = json.dumps(
                [
                    {
                        "field_id": self.settings.cf_shipping_line,
                        "operator": "=",
                        "value": carrier_filter_value,
                    }
                ]
            )
        all_tasks: list[dict[str, Any]] = []
        page = 0
        while True:
            if page >= self.settings.clickup_max_pages_per_list:
                raise ClickUpWorkloadLimitError(
                    f"ClickUp list {list_id} exceeds CLICKUP_MAX_PAGES_PER_LIST="
                    f"{self.settings.clickup_max_pages_per_list}"
                )
            params = dict(base_params)
            params["page"] = str(page)
            response = self._request("get", url, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            batch = payload.get("tasks", [])
            if not batch:
                break
            if not isinstance(batch, list):
                raise ValueError(f"ClickUp list {list_id} returned a non-list tasks payload")
            if len(all_tasks) + len(batch) > self.settings.clickup_max_tasks_per_list:
                raise ClickUpWorkloadLimitError(
                    f"ClickUp list {list_id} exceeds CLICKUP_MAX_TASKS_PER_LIST="
                    f"{self.settings.clickup_max_tasks_per_list}"
                )
            all_tasks.extend(batch)
            if payload.get("last_page") is True:
                break
            page += 1
        return all_tasks

    def _shipping_line_filter_value_for_list(self, list_id: str) -> str | int | None:
        allowed_lines = self.settings.shipment_allowed_lines or []
        if len(allowed_lines) != 1:
            return None

        carrier_name = allowed_lines[0]
        try:
            fields = self._fetch_list_fields(list_id)
        except requests.RequestException as exc:
            if list_id not in self._carrier_filter_warning_lists:
                print(
                    f"ClickUp carrier prefilter skipped for list {list_id}: {exc}. "
                    "Continuing without the API-side carrier filter.",
                    file=sys.stderr,
                )
                self._carrier_filter_warning_lists.add(list_id)
            return None

        shipping_line_field = next(
            (field for field in fields if field.get("id") == self.settings.cf_shipping_line),
            None,
        )
        if not isinstance(shipping_line_field, dict):
            return None

        type_config = shipping_line_field.get("type_config")
        options = type_config.get("options") if isinstance(type_config, dict) else None
        if not isinstance(options, list):
            return None

        wanted = _normalize_carrier_filter_label(carrier_name)
        for option in options:
            if not isinstance(option, dict):
                continue
            option_name = option.get("name")
            if not isinstance(option_name, str):
                continue
            if _normalize_carrier_filter_label(option_name) != wanted:
                continue
            option_id = option.get("id")
            if option_id is not None:
                return str(option_id)
            orderindex = option.get("orderindex")
            if orderindex is not None:
                return orderindex
        return None

    def _fetch_list_fields(self, list_id: str) -> list[dict[str, Any]]:
        cached = self._list_field_cache.get(list_id)
        if cached is not None:
            return cached
        url = f"{self.base_url}/list/{list_id}/field"
        response = self._request("get", url, timeout=30)
        response.raise_for_status()
        fields = response.json().get("fields", [])
        if not isinstance(fields, list):
            fields = []
        self._list_field_cache[list_id] = fields
        return fields

    def _fetch_wan_hai_reference_hints(self, task_id: str) -> list[str]:
        if not task_id or self.settings.wan_hai_reference_comment_limit <= 0:
            return []
        try:
            comment_texts = self._fetch_recent_task_comment_texts(
                task_id,
                limit=self.settings.wan_hai_reference_comment_limit,
            )
        except requests.RequestException as exc:
            print(
                f"Wan Hai reference hint lookup skipped for task {task_id}: {exc}",
                file=sys.stderr,
            )
            return []
        return _extract_wan_hai_reference_hints("\n".join(comment_texts))

    def _fetch_recent_task_comment_texts(self, task_id: str, *, limit: int) -> list[str]:
        comments = self._fetch_recent_task_comments(task_id, limit=limit)
        texts: list[str] = []
        for raw in comments:
            texts.extend(_comment_reference_texts(raw))
        return texts

    def _fetch_recent_task_comments(self, task_id: str, *, limit: int) -> list[dict[str, Any]]:
        url = f"{self.base_url}/task/{task_id}/comment"
        response = self._request("get", url, timeout=30)
        response.raise_for_status()
        raw_comments = response.json().get("comments")
        if not isinstance(raw_comments, list):
            return []

        comments: list[dict[str, Any]] = []
        for raw in raw_comments[: max(0, limit)]:
            if not isinstance(raw, dict):
                continue
            comments.append(raw)
        return comments

    def plan_shipment_update(self, shipment: ShipmentRef, status: ShipmentStatus) -> ShipmentUpdatePlan:
        if status.require_destination_evidence and shipment.destination_port:
            # A conflicting carrier POD must not override the shipment's destination.
            status = replace(status, destination_port=(
                shipment.destination_port
                if same_port(shipment.destination_port, status.destination_port)
                else None
            ))
        now_utc = datetime.now(timezone.utc)
        last_checked_display = now_utc.isoformat(timespec="seconds")
        eta_text = _format_event_time(status.eta_local_text, status.eta_time)
        eta_port_local_text = _format_port_local_time(status.eta_local_text, status.eta_time)
        status_value = f"ETA {eta_text}" if self.settings.eta_only_mode else status.status_text
        source_link = status.source_url or _extract_first_url(status.raw_source)

        always_field_updates: list[ShipmentFieldWrite] = []
        candidate_field_updates: list[ShipmentFieldWrite] = []
        if self.settings.cf_shipment_status:
            candidate_field_updates.append(
                ShipmentFieldWrite(
                    field_id=self.settings.cf_shipment_status,
                    value=status_value,
                    field_type="text",
                    label="Shipment status",
                )
            )
        if self.settings.cf_status_last_checked:
            always_field_updates.append(
                ShipmentFieldWrite(
                    field_id=self.settings.cf_status_last_checked,
                    value=now_utc,
                    field_type="datetime",
                    label="Last T&T Update",
                )
            )
        container_update = _build_container_field_update(
            shipment=shipment,
            status=status,
            field_id=self.settings.cf_container_no,
        )
        if container_update is not None:
            candidate_field_updates.append(container_update)
        candidate_field_updates.extend(_build_direct_event_field_updates(status=status, settings=self.settings))
        effective_field_values = _effective_field_values(
            shipment.current_field_values,
            candidate_field_updates,
        )
        vessel_voyage_value = _effective_vessel_voyage(
            shipment=shipment,
            status=status,
            settings=self.settings,
            field_values=effective_field_values,
            now_utc=now_utc,
        )
        if self.settings.cf_vessel_voyage and vessel_voyage_value:
            candidate_field_updates.append(
                ShipmentFieldWrite(
                    field_id=self.settings.cf_vessel_voyage,
                    value=vessel_voyage_value,
                    field_type="text",
                    label="Vessel/Voyage",
                )
            )
        snapshot_hash = _compute_snapshot_hash(
            shipment=shipment,
            status=status,
            status_value=status_value,
            eta_text=eta_text,
            eta_port_local_text=eta_port_local_text,
            vessel_voyage_value=vessel_voyage_value,
        )
        if self.settings.cf_track_trace_snapshot:
            candidate_field_updates.append(
                ShipmentFieldWrite(
                    field_id=self.settings.cf_track_trace_snapshot,
                    value=snapshot_hash,
                    field_type="text",
                    label="Track & Trace snapshot",
                )
            )

        previous_snapshot_hash = (shipment.track_trace_snapshot_hash or "").strip() or None
        previous_status_value = (shipment.current_status_value or "").strip() or None
        changed = True
        if previous_snapshot_hash and previous_snapshot_hash == snapshot_hash:
            changed = False
        elif not previous_snapshot_hash and previous_status_value and previous_status_value == status_value:
            changed = False

        candidate_field_updates = _dedupe_field_updates(candidate_field_updates)
        changed_field_updates = [
            update
            for update in candidate_field_updates
            if _field_value_changed(update, shipment.current_field_values.get(update.field_id))
        ]
        task_status_update = _build_task_status_update(
            shipment=shipment,
            status=status,
            settings=self.settings,
            candidate_field_updates=candidate_field_updates,
            now_utc=now_utc,
        )
        custom_field_updates = _dedupe_field_updates(always_field_updates + changed_field_updates)
        fields_changed = bool(changed_field_updates)
        plan_changed = fields_changed or task_status_update is not None

        if self.settings.recent_moves_limit > 0:
            recent_moves = status.recent_moves[: self.settings.recent_moves_limit]
            recent_moves_label = f"Recent moves (last {len(recent_moves)}):"
        else:
            recent_moves = status.recent_moves
            recent_moves_label = f"All moves ({len(recent_moves)}):"

        if self.settings.eta_only_mode:
            comment_lines = [
                f"{self.settings.status_comment_prefix}: ETA update",
                f"ETA (port local time): {eta_port_local_text}",
                f"Last checked (UTC): {last_checked_display}",
            ]
            if source_link:
                comment_lines.append(f"Carrier source: {source_link}")
            if vessel_voyage_value:
                comment_lines.append(f"Vessel/Voyage: {vessel_voyage_value}")
            if status.booking_status_text:
                comment_lines.append(f"Booking status: {status.booking_status_text}")
            if recent_moves:
                comment_lines.append(recent_moves_label)
                for idx, move in enumerate(recent_moves, start=1):
                    rendered = _format_move_line(move, now_utc=now_utc)
                    if rendered:
                        comment_lines.append(f"{idx}. {rendered}")
        else:
            comment_lines = [
                f"{self.settings.status_comment_prefix}: {status.status_text}",
                f"Line: {shipment.shipping_line}",
                f"Last checked (UTC): {last_checked_display}",
                f"ETA (port local time): {eta_port_local_text}",
            ]
            if source_link:
                comment_lines.append(f"Carrier source: {source_link}")
            if shipment.booking_no:
                comment_lines.append(f"Booking: {shipment.booking_no}")
            if shipment.container_no:
                comment_lines.append(f"Container: {shipment.container_no}")
            if status.location:
                comment_lines.append(f"Location: {status.location}")
            event_port_local_time = _format_port_local_time(
                status.latest_move.event_time_local_text if status.latest_move else None,
                status.event_time,
            )
            if event_port_local_time != "n/a":
                comment_lines.append(f"Event time (port local): {event_port_local_time}")
            elif status.event_time:
                comment_lines.append(f"Event time (UTC): {status.event_time.isoformat()}")
            if status.movement_details:
                comment_lines.append(f"Last movement details: {status.movement_details}")
            if vessel_voyage_value:
                comment_lines.append(f"Vessel/Voyage: {vessel_voyage_value}")
            if status.booking_status_text:
                comment_lines.append(f"Booking status: {status.booking_status_text}")
            if recent_moves:
                comment_lines.append(recent_moves_label)
                for idx, move in enumerate(recent_moves, start=1):
                    rendered = _format_move_line(move, now_utc=now_utc)
                    if rendered:
                        comment_lines.append(f"{idx}. {rendered}")
            if status.raw_source:
                comment_lines.append(f"Source trace: {status.raw_source}")

        if task_status_update:
            current_label = shipment.current_task_status or "unknown"
            comment_lines.append(f"Task status: {current_label} -> {task_status_update}")

        if not fields_changed and task_status_update and not self.settings.shipment_comment_on_no_change:
            comment_text = "\n".join(
                [
                    f"{self.settings.status_comment_prefix}: Workflow status update",
                    f"Task status: {shipment.current_task_status or 'unknown'} -> {task_status_update}",
                    f"Last checked (UTC): {last_checked_display}",
                ]
            )
        elif not fields_changed and self.settings.shipment_comment_on_no_change:
            no_change_lines = [
                f"{self.settings.status_comment_prefix}: No change found",
                f"T&T executed on {last_checked_display}",
            ]
            if source_link:
                no_change_lines.append(f"Carrier source: {source_link}")
            comment_text = "\n".join(no_change_lines)
        elif fields_changed:
            comment_text = "\n".join(comment_lines)
        else:
            comment_text = None

        return ShipmentUpdatePlan(
            changed=plan_changed,
            status_value=status_value,
            snapshot_hash=snapshot_hash,
            custom_field_updates=custom_field_updates,
            task_status_update=task_status_update,
            comment_text=comment_text,
        )

    def update_shipment_status(self, shipment: ShipmentRef, status: ShipmentStatus) -> ShipmentWriteResult:
        plan = self.plan_shipment_update(shipment, status)

        for update in plan.custom_field_updates:
            if update.field_type == "datetime":
                self._set_date_custom_field(shipment.task_id, update.field_id, update.value, include_time=True)
            elif update.field_type == "date":
                self._set_date_custom_field(shipment.task_id, update.field_id, update.value, include_time=False)
            else:
                self._set_custom_field(shipment.task_id, update.field_id, str(update.value))

        if plan.task_status_update:
            try:
                self._set_task_status(shipment.task_id, plan.task_status_update)
            except requests.RequestException as exc:
                print(
                    f"Task status update failed for {shipment.task_id}: {exc}",
                    file=sys.stderr,
                )

        if plan.comment_text:
            self._post_comment(shipment.task_id, plan.comment_text)
        return ShipmentWriteResult(
            changed=plan.changed,
            status_value=plan.status_value,
            snapshot_hash=plan.snapshot_hash,
        )

    def request_wan_hai_manual_capture(self, shipment: ShipmentRef, *, error: str | None = None) -> bool:
        if not self.settings.wan_hai_manual_capture_comment:
            return False
        if self._recent_wan_hai_manual_capture_comment_exists(shipment.task_id):
            return False
        self._post_comment(shipment.task_id, _wan_hai_manual_capture_comment(shipment, self.settings, error=error))
        return True

    def report_msc_tracking_failure(self, shipment: ShipmentRef, *, reference: str, error: str) -> bool:
        """Post a concise, idempotent carrier diagnostic without mutating shipment data."""
        marker = "MSC Track & Trace exception"
        try:
            comments = self._fetch_recent_task_comments(shipment.task_id, limit=10)
        except requests.RequestException as exc:
            print(f"MSC failure comment cooldown check skipped for task {shipment.task_id}: {exc}", file=sys.stderr)
            comments = []
        normalized_reference = reference.strip().upper()
        for raw in comments:
            text = "\n".join(_comment_reference_texts(raw))
            if marker in text and normalized_reference in text.upper():
                return False
        self._post_comment(
            shipment.task_id,
            "\n".join(
                [
                    marker,
                    "",
                    "MSC returned no usable tracking result during the normal public-browser review.",
                    f"Reference checked: {reference}",
                    f"Error: {error}",
                    f"Carrier source: https://www.msc.com/en/track-a-shipment",
                    "",
                    "This failed reference did not change shipment fields, Last T&T Update, or workflow status.",
                    "Action required: verify the booking/container reference with MSC or the booking party, then rerun Track & Trace.",
                ]
            ),
        )
        return True

    def report_msc_container_review_issue(self, shipment: ShipmentRef, *, error: str) -> bool:
        """Record a non-mutating multi-container MSC review problem once."""
        marker = "MSC Track & Trace container review"
        try:
            comments = self._fetch_recent_task_comments(shipment.task_id, limit=10)
        except requests.RequestException as exc:
            print(f"MSC review comment cooldown check skipped for task {shipment.task_id}: {exc}", file=sys.stderr)
            comments = []
        if any(marker in "\n".join(_comment_reference_texts(raw)) for raw in comments):
            return False
        self._post_comment(
            shipment.task_id,
            "\n".join(
                [
                    marker,
                    "",
                    "MSC results were reviewed per container but are incomplete or disagree on shipment-level tracking data.",
                    f"Error: {error}",
                    "Carrier source: https://www.msc.com/en/track-a-shipment",
                    "",
                    "No shipment fields, Last T&T Update, or workflow status were changed.",
                    "Action required: verify every listed container with MSC, then rerun Track & Trace.",
                ]
            ),
        )
        return True

    def _recent_wan_hai_manual_capture_comment_exists(self, task_id: str) -> bool:
        cooldown_hours = self.settings.wan_hai_manual_capture_comment_cooldown_hours
        if cooldown_hours <= 0:
            return False
        try:
            comments = self._fetch_recent_task_comments(task_id, limit=10)
        except requests.RequestException as exc:
            print(
                f"Wan Hai manual capture cooldown check skipped for task {task_id}: {exc}",
                file=sys.stderr,
            )
            return False

        now_utc = datetime.now(timezone.utc)
        for raw in comments:
            text = "\n".join(_comment_reference_texts(raw))
            if "Wan Hai Manual Capture needed" not in text:
                continue
            created_at = _parse_datetime_value(raw.get("date"))
            if created_at is None:
                return True
            age_hours = (now_utc - created_at).total_seconds() / 3600
            if age_hours <= cooldown_hours:
                return True
        return False

    def _set_custom_field(self, task_id: str, field_id: str, value: str) -> None:
        url = f"{self.base_url}/task/{task_id}/field/{field_id}"
        response = self._request("post", url, json={"value": value}, timeout=30)
        response.raise_for_status()

    def _set_date_custom_field(self, task_id: str, field_id: str, value: datetime | None, *, include_time: bool) -> None:
        url = f"{self.base_url}/task/{task_id}/field/{field_id}"
        payload: dict[str, Any] = {"value": None if value is None else int(value.timestamp() * 1000)}
        if include_time and value is not None:
            payload["value_options"] = {"time": True}
        response = self._request("post", url, json=payload, timeout=30)
        response.raise_for_status()

    def _set_task_status(self, task_id: str, status_name: str) -> None:
        url = f"{self.base_url}/task/{task_id}"
        response = self._request("put", url, json={"status": status_name}, timeout=30)
        response.raise_for_status()

    def _post_comment(self, task_id: str, comment: str) -> None:
        url = f"{self.base_url}/task/{task_id}/comment"
        response = self._request("post", url, json={"comment_text": comment, "notify_all": False}, timeout=30)
        response.raise_for_status()


def _field_map(custom_fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f["id"]: f for f in custom_fields if "id" in f}


def _shipment_destination(fields: dict[str, dict[str, Any]]) -> str | None:
    return _field_text(fields.get("303d68fa-f250-43ac-abe8-17b70e07b46d"))


def _expected_container_count(custom_fields: Any) -> int | None:
    if not isinstance(custom_fields, list):
        return None

    for field in custom_fields:
        if not isinstance(field, dict):
            continue
        if _normalize_workflow_status_name(str(field.get("name") or "")) != "numberofcontainers":
            continue
        value = field.get("value")
        try:
            parsed_count = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        if not parsed_count.is_integer():
            return None
        count = int(parsed_count)
        return count if count > 0 else None
    return None


def _build_direct_event_field_updates(*, status: ShipmentStatus, settings: Settings) -> list[ShipmentFieldWrite]:
    updates: list[ShipmentFieldWrite] = []

    eta_value = _coerce_display_date(status.eta_local_text, status.eta_time)
    if settings.cf_eta and eta_value is not None:
        updates.append(
            ShipmentFieldWrite(
                field_id=settings.cf_eta,
                value=eta_value,
                field_type="date",
                label="ETA",
            )
        )

    ordered_moves = _order_moves_ascending(status.recent_moves)
    if not ordered_moves:
        return updates

    destination_discharge_index = _validated_destination_discharge_index(status, ordered_moves)
    pre_discharge_moves = (
        ordered_moves[:destination_discharge_index]
        if destination_discharge_index is not None
        else ordered_moves
    )
    post_discharge_moves = (
        ordered_moves[destination_discharge_index:]
        if destination_discharge_index is not None
        else []
    )
    if status.require_destination_evidence:
        post_discharge_moves = [
            move for move in post_discharge_moves
            if _event_code_from_move(move) != "DISC"
            or _is_destination_discharge(status, move)
        ]

    updates.extend(
        _build_move_field_updates(
            moves=pre_discharge_moves,
            field_specs=[
                ("GTIN", settings.cf_gate_in_full, "Gate-in full", False),
                ("GTOT", settings.cf_gate_out_empty, "Gate out empty", False),
            ],
        )
    )
    etd_move = _pick_etd_move(pre_discharge_moves)
    if settings.cf_etd and etd_move is not None:
        etd_value = _coerce_display_date(etd_move.event_time_local_text, etd_move.event_time)
        if etd_value is not None:
            updates.append(
                ShipmentFieldWrite(
                    field_id=settings.cf_etd,
                    value=etd_value,
                    field_type="date",
                    label="ETD",
                )
            )
    updates.extend(
        _build_move_field_updates(
            moves=post_discharge_moves,
            field_specs=[
                ("DISC", settings.cf_discharge_date, "Discharge date", True),
                ("GTOT", settings.cf_gate_out_delivery, "Gate out delivery", True),
                ("GTIN", settings.cf_gate_in_empty, "Gate in empty", True),
            ],
        )
    )
    return updates


def _build_container_field_update(
    *,
    shipment: ShipmentRef,
    status: ShipmentStatus,
    field_id: str | None,
) -> ShipmentFieldWrite | None:
    if not field_id or not status.discovered_containers:
        return None

    current_tokens = _container_tokens_from_value(shipment.container_no)
    discovered_tokens = _container_tokens_from_value(status.discovered_containers)
    if not discovered_tokens:
        return None

    expected_count = shipment.expected_container_count
    if (
        expected_count is not None
        and status.container_discovery_authoritative
        and len(discovered_tokens) == expected_count
        and len(current_tokens) > expected_count
    ):
        return ShipmentFieldWrite(
            field_id=field_id,
            value=", ".join(discovered_tokens),
            field_type="text",
            label="Container",
        )

    merged_tokens = list(current_tokens)
    current_set = set(current_tokens)
    for token in discovered_tokens:
        if token in current_set:
            continue
        if expected_count is not None and len(merged_tokens) >= expected_count:
            break
        merged_tokens.append(token)
        current_set.add(token)

    if merged_tokens == current_tokens:
        return None

    return ShipmentFieldWrite(
        field_id=field_id,
        value=", ".join(merged_tokens),
        field_type="text",
        label="Container",
    )


def _build_task_status_update(
    *,
    shipment: ShipmentRef,
    status: ShipmentStatus,
    settings: Settings,
    candidate_field_updates: list[ShipmentFieldWrite],
    now_utc: datetime,
) -> str | None:
    if not settings.clickup_use_task_status:
        return None

    current_status = shipment.current_task_status
    if not current_status:
        return None

    status_by_step = _workflow_status_by_step(settings, shipment)
    current_step = _workflow_step_for_status(status_by_step, current_status)
    if current_step is None:
        return None
    if _workflow_step_order(current_step) >= _workflow_step_order("empty_returned"):
        return None

    effective_values = dict(shipment.current_field_values)
    for update in candidate_field_updates:
        effective_values[update.field_id] = update.value

    target_step = _derive_operational_status_step(
        shipment=shipment,
        status=status,
        settings=settings,
        field_values=effective_values,
        current_step=current_step,
        status_by_step=status_by_step,
        now_utc=now_utc,
    )
    target_status_step = _status_step_for_target(status_by_step, target_step)
    if target_status_step is None:
        return None

    sequence = _workflow_status_sequence(status_by_step)
    current_index = sequence.index(current_step)
    target_index = sequence.index(target_status_step)
    if target_index <= current_index:
        return None

    return status_by_step[target_status_step]


def _operational_status_sequence(settings: Settings) -> list[str]:
    return [
        settings.clickup_status_pending_booking,
        settings.clickup_status_booking_confirmed,
        settings.clickup_status_collected,
        settings.clickup_status_origin_port,
        settings.clickup_status_in_transit,
        settings.clickup_status_arriving,
        settings.clickup_status_arrived_port,
        settings.clickup_status_en_route_warehouse,
        settings.clickup_status_in_warehouse,
        settings.clickup_status_empty_returned,
    ]


_WORKFLOW_STEP_ORDER = (
    "pending_booking",
    "booking_confirmed",
    "collected",
    "origin_port",
    "in_transit",
    "arriving",
    "arrived_port",
    "at_rail",
    "arrived_ramp",
    "en_route_warehouse",
    "in_warehouse",
    "empty_returned",
)


def _workflow_status_by_step(settings: Settings, shipment: ShipmentRef) -> dict[str, str]:
    if _uses_rta_workflow(shipment):
        return {
            "pending_booking": "bk pending to confirm",
            "booking_confirmed": "bk confirmed",
            "in_transit": "transit",
            "arriving": "near arrival",
            "arrived_port": "at port",
            "at_rail": "at rail",
            "arrived_ramp": "container arrived at ramp",
            "en_route_warehouse": "released to consignee",
            "in_warehouse": "warehouse",
            "empty_returned": "empty returned",
        }

    return {
        "pending_booking": settings.clickup_status_pending_booking,
        "booking_confirmed": settings.clickup_status_booking_confirmed,
        "collected": settings.clickup_status_collected,
        "origin_port": settings.clickup_status_origin_port,
        "in_transit": settings.clickup_status_in_transit,
        "arriving": settings.clickup_status_arriving,
        "arrived_port": settings.clickup_status_arrived_port,
        "en_route_warehouse": settings.clickup_status_en_route_warehouse,
        "in_warehouse": settings.clickup_status_in_warehouse,
        "empty_returned": settings.clickup_status_empty_returned,
    }


def _uses_rta_workflow(shipment: ShipmentRef) -> bool:
    normalized_list_name = _normalize_workflow_status_name(shipment.list_name or "")
    if normalized_list_name == "rtashipments":
        return True

    normalized_status = _normalize_workflow_status_name(shipment.current_task_status or "")
    return normalized_status in {
        "bkpendingtoconfirm",
        "bkconfirmed",
        "neararrival",
        "atport",
        "atrail",
        "containerarrivedatramp",
        "releasedtoconsignee",
    }


def _workflow_status_sequence(status_by_step: dict[str, str]) -> list[str]:
    return [step for step in _WORKFLOW_STEP_ORDER if step in status_by_step]


def _workflow_step_for_status(status_by_step: dict[str, str], status: str) -> str | None:
    normalized_status = _normalize_workflow_status_name(status)
    for step, candidate in status_by_step.items():
        if _normalize_workflow_status_name(candidate) == normalized_status:
            return step
    return None


def _status_step_for_target(status_by_step: dict[str, str], target_step: str | None) -> str | None:
    if target_step is None:
        return None

    target_order = _workflow_step_order(target_step)
    for step in reversed(_WORKFLOW_STEP_ORDER[: target_order + 1]):
        if step in status_by_step:
            return step
    return None


def _max_workflow_step(current: str | None, candidate: str) -> str:
    if current is None:
        return candidate
    if _workflow_step_order(candidate) > _workflow_step_order(current):
        return candidate
    return current


def _workflow_step_order(step: str) -> int:
    try:
        return _WORKFLOW_STEP_ORDER.index(step)
    except ValueError:
        return -1


def _derive_operational_status_step(
    *,
    shipment: ShipmentRef,
    status: ShipmentStatus,
    settings: Settings,
    field_values: dict[str, Any],
    current_step: str,
    status_by_step: dict[str, str],
    now_utc: datetime,
) -> str | None:
    today = now_utc.date()
    carrier_set = bool((shipment.shipping_line or "").strip())
    booking_set = bool((shipment.booking_no or "").strip())

    eta_date = _field_date(settings.cf_eta, field_values)
    if eta_date is None and status.eta_time is not None:
        eta_date = status.eta_time.astimezone(timezone.utc).date()

    etd_date = _field_date(settings.cf_etd, field_values)
    gate_out_empty_date = _field_date(settings.cf_gate_out_empty, field_values)
    gate_in_full_date = _field_date(settings.cf_gate_in_full, field_values)
    gate_out_delivery_date = _field_date(settings.cf_gate_out_delivery, field_values)
    gate_in_empty_date = _field_date(settings.cf_gate_in_empty, field_values)
    discharge_date = _field_date(settings.cf_discharge_date, field_values)

    target_step: str | None = None

    if (
        carrier_set
        and booking_set
        and etd_date is not None
        and eta_date is not None
        and _carrier_booking_allows_confirmation(shipment, status)
    ):
        target_step = "booking_confirmed"

    # A confirmed feeder-barge leg is operational transit. Carrier feeds do not
    # reliably expose the origin gate events for these moves, so do not block
    # this transition on manually maintained gate fields.
    if eta_date is not None and eta_date > today and _has_origin_barge_transit_move(
        shipment=shipment,
        status=status,
        settings=settings,
        field_values=field_values,
        now_utc=now_utc,
        current_step=current_step,
    ):
        target_step = _max_workflow_step(target_step, "in_transit")

    if gate_out_empty_date is not None and gate_in_full_date is None:
        target_step = _max_workflow_step(target_step, "collected")

    if gate_out_empty_date is not None and gate_in_full_date is not None and etd_date is not None:
        if etd_date > today:
            target_step = _max_workflow_step(target_step, "origin_port")
            if eta_date is not None and eta_date > today and _has_origin_barge_transit_move(
                shipment=shipment,
                status=status,
                settings=settings,
                field_values=field_values,
                now_utc=now_utc,
                current_step=current_step,
            ):
                target_step = _max_workflow_step(target_step, "in_transit")
                days_until_eta = (eta_date - today).days
                if 5 <= days_until_eta <= 10:
                    target_step = _max_workflow_step(target_step, "arriving")
        elif eta_date is not None and eta_date > today:
            target_step = _max_workflow_step(target_step, "in_transit")
            days_until_eta = (eta_date - today).days
            if 5 <= days_until_eta <= 10:
                target_step = _max_workflow_step(target_step, "arriving")

    if discharge_date is not None and (
        not status.require_destination_evidence
        or _validated_destination_discharge_index(status, _order_moves_ascending(status.recent_moves)) is not None
    ):
        target_step = _max_workflow_step(target_step, "arrived_port")

    if "at_rail" in status_by_step and _has_actual_rail_departure(status, current_step=current_step):
        target_step = _max_workflow_step(target_step, "at_rail")

    if "arrived_ramp" in status_by_step and _has_actual_rail_ramp_arrival(status, current_step=current_step):
        target_step = _max_workflow_step(target_step, "arrived_ramp")

    if gate_out_delivery_date is not None and (
        target_step is not None and _workflow_step_order(target_step) >= _workflow_step_order("arrived_port")
        or _workflow_step_order(current_step) >= _workflow_step_order("arrived_port")
    ):
        target_step = _max_workflow_step(target_step, "en_route_warehouse")

    if gate_in_empty_date is not None and gate_in_empty_date <= today and (
        target_step is not None and _workflow_step_order(target_step) >= _workflow_step_order("en_route_warehouse")
        or _workflow_step_order(current_step) >= _workflow_step_order("en_route_warehouse")
        or _has_verified_one_empty_return(shipment, status, now_utc)
    ):
        target_step = _max_workflow_step(target_step, "empty_returned")

    return target_step


def _effective_field_values(
    current_values: dict[str, Any],
    candidate_field_updates: list[ShipmentFieldWrite],
) -> dict[str, Any]:
    values = dict(current_values)
    for update in candidate_field_updates:
        values[update.field_id] = update.value
    return values


def _effective_vessel_voyage(
    *,
    shipment: ShipmentRef,
    status: ShipmentStatus,
    settings: Settings,
    field_values: dict[str, Any],
    now_utc: datetime,
) -> str | None:
    current_step = _workflow_step_for_status(
        _workflow_status_by_step(settings, shipment),
        shipment.current_task_status or "",
    )
    if _has_origin_barge_transit_move(
        shipment=shipment,
        status=status,
        settings=settings,
        field_values=field_values,
        now_utc=now_utc,
        current_step=current_step,
    ):
        return "BARGE"

    current_vessel_voyage = _field_string(settings.cf_vessel_voyage, field_values)
    actual_event_vessel_voyage = _latest_actual_event_vessel_voyage(status, now_utc=now_utc)
    # Do not replace an active feeder-leg marker with a planned final vessel.
    # It advances only when a later vessel-bearing event is actually reported.
    if current_vessel_voyage and current_vessel_voyage.upper() == "BARGE":
        return actual_event_vessel_voyage or current_vessel_voyage

    # Maersk exposes its final leg as an estimated ARRIVAL event. Keep that
    # carrier-confirmed vessel visible before it departs the transshipment port.
    if status.final_vessel_voyage:
        return status.final_vessel_voyage.strip()

    if actual_event_vessel_voyage:
        return _enrich_voyage_only_event_vessel(
            actual_event_vessel_voyage,
            status.vessel_voyage,
        )

    return (status.vessel_voyage or "").strip() or None


def _latest_actual_event_vessel_voyage(status: ShipmentStatus, *, now_utc: datetime) -> str | None:
    candidates = [
        move
        for move in status.recent_moves
        if _event_code_from_move(move) in {"LOAD", "DEPA", "ARRI"}
        and (move.vessel_voyage or "").strip()
        and _move_is_effectively_actual(move, now_utc=now_utc)
    ]
    if not candidates:
        return None

    latest = max(
        enumerate(candidates),
        key=lambda item: (
            item[1].event_time is not None,
            item[1].event_time.isoformat() if item[1].event_time is not None else "",
            item[0],
        ),
    )[1]
    return (latest.vessel_voyage or "").strip() or None


def _enrich_voyage_only_event_vessel(event_vessel_voyage: str, carrier_vessel_voyage: str | None) -> str:
    """Keep the actual event selection while restoring a matching vessel name."""
    candidate = (carrier_vessel_voyage or "").strip()
    if not candidate or not _is_voyage_only(event_vessel_voyage):
        return event_vessel_voyage

    voyage = re.escape(event_vessel_voyage.strip())
    if re.search(rf"(?<![A-Z0-9]){voyage}(?![A-Z0-9])", candidate, flags=re.IGNORECASE):
        return candidate
    return event_vessel_voyage


def _is_voyage_only(value: str) -> bool:
    return bool(re.fullmatch(r"\d{2,5}[A-Z]{0,2}", value.strip(), flags=re.IGNORECASE))


def _has_origin_barge_transit_move(
    *,
    shipment: ShipmentRef,
    status: ShipmentStatus,
    settings: Settings,
    field_values: dict[str, Any],
    now_utc: datetime,
    current_step: str | None,
) -> bool:
    # BARGE is an origin-stage marker. An unknown or later workflow state must
    # not overwrite a carrier-reported vessel with the feeder placeholder.
    if current_step not in {"booking_confirmed", "collected", "origin_port"}:
        return False

    if not (shipment.shipping_line or "").strip():
        return False

    eta_date = _field_date(settings.cf_eta, field_values)
    if eta_date is None and status.eta_time is not None:
        eta_date = status.eta_time.astimezone(timezone.utc).date()
    if eta_date is not None and eta_date <= now_utc.date():
        return False

    ordered_moves = _order_moves_ascending(status.recent_moves)
    if not ordered_moves:
        return False

    origin_ready_index = next(
        (
            idx
            for idx, move in enumerate(ordered_moves)
            if _event_code_from_move(move) in {"GTIN", "LOAD"}
        ),
        None,
    )
    if origin_ready_index is None:
        return False

    if _has_actual_barge_yard_load(ordered_moves, now_utc=now_utc):
        return True

    barge_load = _pick_origin_barge_load(ordered_moves, origin_ready_index)
    if (
        barge_load is not None
        and _move_is_effectively_actual(barge_load, now_utc=now_utc)
        and _has_actual_intermediate_discharge(ordered_moves, barge_load)
    ):
        return True

    return False


def _carrier_booking_allows_confirmation(shipment: ShipmentRef, status: ShipmentStatus) -> bool:
    if _normalize_workflow_status_name(shipment.shipping_line or "") == "one":
        return _carrier_booking_is_confirmed(status)
    return not _carrier_booking_is_pending(status)


def _carrier_booking_is_confirmed(status: ShipmentStatus) -> bool:
    candidates = [
        status.booking_status_text,
        status.status_text,
        status.movement_details,
    ]
    for value in candidates:
        normalized = _normalize_workflow_status_name(value or "")
        if normalized in {"confirmed", "bookingconfirmed", "bkconfirmed"}:
            return True
    return False


def _carrier_booking_is_pending(status: ShipmentStatus) -> bool:
    candidates = [
        status.booking_status_text,
        status.status_text,
        status.movement_details,
        status.latest_move.name if status.latest_move else None,
    ]
    for value in candidates:
        normalized = _normalize_workflow_status_name(value or "")
        if normalized in {"processing", "dataprocessing", "pending", "bookingprocessing"}:
            return True
    return False


def _has_actual_rail_departure(status: ShipmentStatus, *, current_step: str) -> bool:
    return any(
        _move_is_actual(move) and _is_rail_departure_move(move)
        for move in _post_discharge_moves(status, current_step=current_step)
    )


def _has_actual_rail_ramp_arrival(status: ShipmentStatus, *, current_step: str) -> bool:
    return any(
        _move_is_actual(move) and _is_rail_ramp_arrival_move(move)
        for move in _post_discharge_moves(status, current_step=current_step)
    )


def _post_discharge_moves(status: ShipmentStatus, *, current_step: str) -> list[MovementEvent]:
    ordered_moves = _order_moves_ascending(status.recent_moves)
    discharge_index = _validated_destination_discharge_index(status, ordered_moves)
    if discharge_index is not None:
        return ordered_moves[discharge_index + 1 :]

    if not status.require_destination_evidence and _workflow_step_order(current_step) >= _workflow_step_order("arrived_port"):
        return ordered_moves
    return []


def _is_rail_departure_move(move: MovementEvent) -> bool:
    text = _move_search_text(move)
    if not _contains_any(text, ("rail", "intermodal", "ramp")):
        return False
    return _contains_any(
        text,
        (
            "depart",
            "departure",
            "loaded to rail",
            "loaded on rail",
            "on rail",
            "rail out",
            "rail ramp out",
            "interchanged to rail",
            "handed to rail",
            "tendered to rail",
        ),
    )


def _is_rail_ramp_arrival_move(move: MovementEvent) -> bool:
    text = _move_search_text(move)
    if not _contains_any(text, ("rail", "ramp", "intermodal")):
        return False
    return _contains_any(
        text,
        (
            "arriv",
            "available",
            "placed",
            "grounded",
            "notified",
            "at ramp",
            "rail in",
            "intermodal terminal",
        ),
    )


def _move_search_text(move: MovementEvent) -> str:
    return " ".join(
        part.strip().lower()
        for part in (move.name, move.location, move.event_time_local_text)
        if part and part.strip()
    )


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle in value for needle in needles)


def _field_date(field_id: str | None, values: dict[str, Any]) -> date | None:
    if not field_id:
        return None
    parsed = _parse_datetime_value(values.get(field_id))
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).date()


def _field_string(field_id: str | None, values: dict[str, Any]) -> str | None:
    if not field_id:
        return None
    value = values.get(field_id)
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _build_move_field_updates(
    *,
    moves: list[MovementEvent],
    field_specs: list[tuple[str, str | None, str, bool]],
) -> list[ShipmentFieldWrite]:
    updates: list[ShipmentFieldWrite] = []
    now_utc = datetime.now(timezone.utc)
    for event_code, field_id, label, actual_only in field_specs:
        if not field_id:
            continue
        move = next((candidate for candidate in moves if _event_code_from_move(candidate) == event_code), None)
        if move is None:
            continue
        if actual_only and not _move_is_effectively_actual(move, now_utc=now_utc):
            # Preserve the established behavior for an explicit estimate, but
            # do not discard an MSC completed event just because its feed omits
            # an explicit actual/estimated state.
            if _normalize_event_state(move.event_state) == "estimated":
                updates.append(
                    ShipmentFieldWrite(
                        field_id=field_id,
                        value=None,
                        field_type="date",
                        label=label,
                    )
                )
            continue
        date_value = _coerce_display_date(move.event_time_local_text, move.event_time)
        if date_value is None:
            continue
        updates.append(
            ShipmentFieldWrite(
                field_id=field_id,
                value=date_value,
                field_type="date",
                label=label,
            )
        )
    return updates


def _validated_destination_discharge_index(status: ShipmentStatus, moves: list[MovementEvent]) -> int | None:
    if not status.require_destination_evidence:
        return _find_destination_discharge_index(moves)
    for idx, move in enumerate(moves):
        if _is_destination_discharge(status, move):
            return idx
    return None


def _is_destination_discharge(status: ShipmentStatus, move: MovementEvent) -> bool:
    return (
        _event_code_from_move(move) == "DISC"
        and (move.event_state or "").strip().lower() == "actual"
        and move.event_time is not None
        and move.event_time.tzinfo is not None
        and move.event_time <= datetime.now(timezone.utc)
        and _move_at_destination(status, move)
    )


def _move_at_destination(status: ShipmentStatus, move: MovementEvent) -> bool:
    # A carrier port code takes precedence over display labels; conflicting
    # codes must not be rescued by a matching display name.
    return same_port(status.destination_port, move.location_code or move.location)


def _has_verified_one_empty_return(
    shipment: ShipmentRef, status: ShipmentStatus, now_utc: datetime,
) -> bool:
    if shipment.shipping_line.strip().lower() != "one" or not status.require_destination_evidence:
        return False
    moves = _order_moves_ascending(status.recent_moves)
    discharge_index = _validated_destination_discharge_index(status, moves)
    if discharge_index is None:
        return False
    return any(
        move.source_event_name == "Empty Container Returned from Customer"
        and _event_code_from_move(move) == "GTIN"
        and move.event_state == "actual"
        and move.event_time is not None
        and move.event_time.tzinfo is not None
        and move.event_time <= now_utc
        and _move_at_destination(status, move)
        for move in moves[discharge_index + 1:]
    )


def _find_destination_discharge_index(moves: list[MovementEvent]) -> int | None:
    discharge_indices = [
        idx for idx, move in enumerate(moves) if _event_code_from_move(move) == "DISC"
    ]
    if not discharge_indices:
        return None

    # Carrier feeds can append untimed itinerary placeholders after completed
    # events. A dated discharge is more reliable for splitting origin and
    # destination handling events; use untimed discharges only as a fallback.
    dated_discharge_indices = [idx for idx in discharge_indices if moves[idx].event_time is not None]
    if dated_discharge_indices:
        discharge_indices = dated_discharge_indices

    for idx in reversed(discharge_indices):
        later_codes = {
            _event_code_from_move(candidate)
            for candidate in moves[idx + 1 :]
            if candidate.event_time is not None
        }
        if later_codes.intersection({"LOAD", "DEPA", "ARRI"}):
            continue
        return idx
    return None


def _pick_etd_move(moves: list[MovementEvent]) -> MovementEvent | None:
    departures = [move for move in moves if _event_code_from_move(move) == "DEPA"]
    if not departures:
        return None

    first_origin_ready_index = next(
        (
            idx
            for idx, move in enumerate(moves)
            if _event_code_from_move(move) in {"GTIN", "LOAD"}
        ),
        None,
    )
    if first_origin_ready_index is not None:
        origin_ready_move = moves[first_origin_ready_index]
        later_moves = moves[first_origin_ready_index + 1 :]
        same_port_departures = [
            move
            for move in later_moves
            if _event_code_from_move(move) == "DEPA"
            and _locations_match(move.location, origin_ready_move.location)
        ]
        if same_port_departures:
            return same_port_departures[0]

        # MSC and other carrier feeds can represent an origin barge as LOAD at
        # the feeder port, DISC at the main port, then DEPA on the ocean vessel.
        # That LOAD is the operational ETD, even though it is not a DEPA event.
        barge_load = _pick_origin_barge_load(moves, first_origin_ready_index)
        if barge_load is not None:
            return barge_load

        later_departures = [
            move
            for move in later_moves
            if _event_code_from_move(move) == "DEPA" and move.event_time is not None
        ]
        if later_departures:
            return later_departures[0]

        later_departures = [move for move in later_moves if _event_code_from_move(move) == "DEPA"]
        if later_departures:
            return later_departures[0]

    return departures[0]


def _pick_origin_barge_load(moves: list[MovementEvent], origin_ready_index: int) -> MovementEvent | None:
    candidate_indices = list(range(origin_ready_index, len(moves)))
    for load_index in candidate_indices:
        load_move = moves[load_index]
        if _event_code_from_move(load_move) != "LOAD" or load_move.event_time is None:
            continue

        later_departure_index = next(
            (
                idx
                for idx in range(load_index + 1, len(moves))
                if _event_code_from_move(moves[idx]) == "DEPA" and moves[idx].event_time is not None
            ),
            None,
        )
        if later_departure_index is None:
            continue

        intermediate_discharge = next(
            (
                moves[idx]
                for idx in range(load_index + 1, later_departure_index)
                if _event_code_from_move(moves[idx]) == "DISC"
                and moves[idx].event_time is not None
                and not _locations_match(moves[idx].location, load_move.location)
            ),
            None,
        )
        if intermediate_discharge is not None:
            return load_move

        # Some MSC feeder records omit the discharge at the main port. An
        # actual LOAD at one origin location followed by departure from a
        # different location still identifies the feeder barge as the first
        # operational leg, and therefore as the ETD for pricing.
        departure_move = moves[later_departure_index]
        if not _locations_match(departure_move.location, load_move.location):
            return load_move

    return None


def _has_actual_barge_yard_load(moves: list[MovementEvent], *, now_utc: datetime) -> bool:
    actual_loads = [
        move
        for move in moves
        if _event_code_from_move(move) == "LOAD" and _move_is_effectively_actual(move, now_utc=now_utc)
    ]
    if not actual_loads:
        return False

    for move in moves:
        if not _is_barge_yard_move(move) or not _move_is_effectively_actual(move, now_utc=now_utc):
            continue
        if any(_locations_match(load.location, move.location) for load in actual_loads):
            return True
    return False


def _has_actual_intermediate_discharge(moves: list[MovementEvent], load_move: MovementEvent) -> bool:
    return any(
        _event_code_from_move(move) == "DISC"
        and _move_is_actual(move)
        and not _locations_match(move.location, load_move.location)
        and (
            load_move.event_time is None
            or move.event_time is None
            or move.event_time >= load_move.event_time
        )
        for move in moves
    )


def _is_barge_yard_move(move: MovementEvent) -> bool:
    normalized = (move.name or "").strip().lower()
    return "barge yard" in normalized or "(laden)" in normalized


def _locations_match(left: str | None, right: str | None) -> bool:
    left_key = _location_key(left)
    right_key = _location_key(right)
    return bool(left_key and right_key and left_key == right_key)


def _location_key(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value.strip().upper())
    return normalized or None


def _dedupe_field_updates(updates: list[ShipmentFieldWrite]) -> list[ShipmentFieldWrite]:
    deduped: dict[str, ShipmentFieldWrite] = {}
    for update in updates:
        deduped[update.field_id] = update
    return list(deduped.values())


def _container_tokens_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return extract_container_numbers(value)
    if isinstance(value, list):
        return extract_container_numbers(value)
    return extract_container_numbers(str(value))


def _field_value_changed(update: ShipmentFieldWrite, current_value: Any) -> bool:
    if update.field_type == "date":
        planned_dt = _parse_datetime_value(update.value)
        current_dt = _parse_datetime_value(current_value)
        if planned_dt is None:
            return current_dt is not None
        if current_dt is None:
            return True
        return planned_dt.date() != current_dt.date()

    if update.field_type == "datetime":
        planned_dt = _parse_datetime_value(update.value)
        current_dt = _parse_datetime_value(current_value)
        if planned_dt is None:
            return current_dt is not None
        if current_dt is None:
            return True
        return planned_dt.replace(microsecond=0) != current_dt.replace(microsecond=0)

    planned_text = "" if update.value is None else str(update.value).strip()
    current_text = "" if current_value is None else str(current_value).strip()
    return planned_text != current_text


def _order_moves_ascending(moves: list[MovementEvent]) -> list[MovementEvent]:
    indexed = list(enumerate(moves))
    return [
        move
        for _, move in sorted(
            indexed,
            key=lambda item: (
                item[1].event_time or datetime.max.replace(tzinfo=timezone.utc),
                item[0],
            ),
        )
    ]


def _event_code_from_move(move: MovementEvent | None) -> str | None:
    if move is None or not move.name:
        return None
    match = re.search(r"\(([A-Z]{4})\)\s*$", move.name.strip())
    if match:
        return match.group(1)

    normalized = move.name.strip().lower()
    if "empty container release to shipper" in normalized:
        return "GTOT"
    if "import to consignee" in normalized:
        return "GTOT"
    if "empty received at cy" in normalized:
        return "GTIN"
    if "export received at cy" in normalized:
        return "GTIN"
    if "empty to shipper" in normalized:
        return "GTOT"
    if "transport departed" in normalized:
        return "DEPA"
    if "container discharged" in normalized:
        return "DISC"
    if "container loaded" in normalized:
        return "LOAD"
    if "gated in" in normalized:
        return "GTIN"
    if "gated out" in normalized:
        return "GTOT"
    return None


def _coerce_display_date(local_text: str | None, event_time: datetime | None) -> datetime | None:
    rendered = _format_event_time(local_text, event_time)
    if rendered == "n/a":
        return None
    parsed = _parse_datetime_value(rendered)
    if parsed is not None:
        return _normalize_date_only(parsed)
    if event_time is not None:
        return _normalize_date_only(event_time)
    return None


def _normalize_date_only(value: datetime) -> datetime:
    normalized = value.astimezone(timezone.utc)
    return normalized.replace(hour=12, minute=0, second=0, microsecond=0)


def _move_is_actual(move: MovementEvent) -> bool:
    return _normalize_event_state(move.event_state) == "actual"


def _move_is_effectively_actual(move: MovementEvent, *, now_utc: datetime) -> bool:
    state = _normalize_event_state(move.event_state)
    if state == "actual":
        return True
    if state == "estimated":
        return False
    move_date = _move_event_date(move)
    return move_date is not None and move_date <= now_utc.date()


def _move_event_date(move: MovementEvent | None) -> date | None:
    if move is None:
        return None
    coerced = _coerce_display_date(move.event_time_local_text, move.event_time)
    if coerced is not None:
        return coerced.astimezone(timezone.utc).date()
    return None


def _is_open_task(task: dict[str, Any]) -> bool:
    status_obj = task.get("status") if isinstance(task.get("status"), dict) else {}
    status_type = str(status_obj.get("type") or "").strip().lower()
    if status_type in {"done", "closed"}:
        return False
    if task.get("archived") is True:
        return False
    return True


def _task_status_text(task: dict[str, Any]) -> str | None:
    status_obj = task.get("status")
    if isinstance(status_obj, dict):
        for key in ("status", "name", "label"):
            value = status_obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(status_obj, str) and status_obj.strip():
        return status_obj.strip()
    return None


def _normalize_workflow_status_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", ascii_text.lower())


def _field_text(field: dict[str, Any] | None) -> str | None:
    if not field:
        return None
    value = field.get("value")
    if value is None:
        return None

    if isinstance(value, str):
        return value

    if isinstance(value, (int, float)):
        option_name = _resolve_option_name(field, value)
        if option_name:
            return option_name
        return str(value)

    if isinstance(value, dict):
        for key in ("name", "label", "value"):
            v = value.get(key)
            if isinstance(v, str):
                return v
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, (str, int, float)):
                option_name = _resolve_option_name(field, item)
                parts.append(option_name or str(item))
            elif isinstance(item, dict):
                for key in ("name", "label", "value"):
                    v = item.get(key)
                    if isinstance(v, str):
                        parts.append(v)
                        break
        if parts:
            return ", ".join(parts)
    return str(value)


def _parse_last_checked(field: dict[str, Any] | None) -> datetime | None:
    if not field:
        return None
    return _parse_datetime_value(field.get("value"))


def _parse_datetime_value(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        return _from_unix_maybe_ms(float(value))

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            try:
                return _from_unix_maybe_ms(float(raw))
            except Exception:
                return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    if isinstance(value, dict):
        for key in ("value", "date", "timestamp", "time"):
            parsed = _parse_datetime_value(value.get(key))
            if parsed is not None:
                return parsed
    return None


def _from_unix_maybe_ms(raw: float) -> datetime | None:
    if raw <= 0:
        return None
    seconds = raw / 1000.0 if raw > 9_999_999_999 else raw
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except Exception:
        return None


def _format_event_time(local_text: str | None, event_time: datetime | None) -> str:
    return format_display_date(local_text, event_time)


def _format_port_local_time(local_text: str | None, event_time: datetime | None) -> str:
    return format_port_local_time(local_text, event_time)


def _format_move_line(move: MovementEvent | None, *, now_utc: datetime | None = None) -> str | None:
    if move is None:
        return None
    parts: list[str] = []
    if move.name and move.name.strip():
        parts.append(move.name.strip())
    event_time = _format_port_local_time(move.event_time_local_text, move.event_time)
    if event_time != "n/a":
        parts.append(event_time)
    if move.location and move.location.strip():
        parts.append(move.location.strip())
    state = _effective_event_state(move, now_utc=now_utc)
    if state:
        parts.append(state.upper())
    if not parts:
        return None
    return " | ".join(parts)


def _effective_event_state(move: MovementEvent, *, now_utc: datetime | None) -> str | None:
    baseline = now_utc or datetime.now(timezone.utc)
    if move.event_time is not None:
        return "actual" if move.event_time.date() <= baseline.date() else "estimate"

    state = _normalize_event_state(move.event_state)
    if state == "actual":
        return "actual"
    if state == "estimated":
        return "estimate"
    return None


def _normalize_event_state(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"actual", "act", "completed", "complete", "confirmed"}:
        return "actual"
    if normalized in {"estimated", "estimate", "est", "planned", "plan", "forecast", "expected", "scheduled"}:
        return "estimated"
    return normalized


def _resolve_option_name(field: dict[str, Any], raw_value: str | int | float) -> str | None:
    type_config = field.get("type_config")
    if not isinstance(type_config, dict):
        return None
    options = type_config.get("options")
    if not isinstance(options, list):
        return None

    value_as_text = str(raw_value)
    for option in options:
        if not isinstance(option, dict):
            continue
        option_id = option.get("id")
        orderindex = option.get("orderindex")
        if value_as_text == str(option_id) or value_as_text == str(orderindex):
            name = option.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _normalize_carrier_filter_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


def _normalize_discovery_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text.strip().lower())


def _name_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _is_wan_hai_line(value: str | None) -> bool:
    normalized = _normalize_carrier_filter_label(value or "")
    return normalized in {"wanhai", "wanhailines", "whl"}


def _wan_hai_manual_capture_comment(shipment: ShipmentRef, settings: Settings, *, error: str | None) -> str:
    references = _manual_capture_references(shipment)
    lines = [
        "Wan Hai Manual Capture needed",
        "",
        "Automated Track & Trace could not access Wan Hai for this shipment.",
        f"Wan Hai website: {settings.wan_hai_manual_capture_url}",
        "",
        "Action required:",
        "1. Open the Wan Hai website and search this shipment.",
        "2. Copy and paste the entire Wan Hai result screen/page into this ClickUp task.",
        "3. Mention TracyAI in the comment so TracyAI can update the shipment fields from the pasted screen.",
    ]
    if references:
        lines.extend(["", f"References to try: {', '.join(references)}"])
    if error:
        lines.extend(["", f"Automation error: {error}"])
    return "\n".join(lines)


def _manual_capture_references(shipment: ShipmentRef) -> list[str]:
    refs: list[str] = []
    for value in [shipment.booking_no, shipment.container_no, *shipment.reference_hints]:
        if not value:
            continue
        for token in re.split(r"[,\s]+", str(value)):
            token = token.strip()
            if not token:
                continue
            key = token.upper()
            if key not in {ref.upper() for ref in refs}:
                refs.append(token)
    return refs


def _comment_reference_texts(raw: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    comment_text = raw.get("comment_text")
    if isinstance(comment_text, str) and comment_text.strip():
        texts.append(comment_text)

    comment_parts = raw.get("comment")
    if isinstance(comment_parts, list):
        for part in comment_parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text)
            attachment = part.get("attachment")
            if isinstance(attachment, dict):
                for key in ("title", "url", "url_w_query", "url_w_host"):
                    value = attachment.get(key)
                    if isinstance(value, str) and value.strip():
                        texts.append(value)

    for key in ("text_content", "markdown_description"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value)
    return texts


def _extract_wan_hai_reference_hints(text: str) -> list[str]:
    if not text:
        return []

    patterns = [
        r"\b(?:BK|BOOKING|BOOK(?:ING)?\s*NO\.?)\s*#?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/-]{5,30})",
        r"\b(?:MBL|M/?B/?L|MASTER\s*B/?L)\s*#?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/-]{5,30})",
        r"\b(?:BL|B/?L)\s*#?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/-]{5,30})",
        r"\b(?:HBL|H/?B/?L|HOUSE\s*B/?L)\s*#?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/-]{5,30})",
    ]

    hints: list[str] = []
    seen: set[str] = set()
    upper_text = text.upper()
    for pattern in patterns:
        for match in re.finditer(pattern, upper_text):
            ref = _clean_reference_hint(match.group(1))
            if not ref:
                continue
            key = ref.upper()
            if key in seen:
                continue
            seen.add(key)
            hints.append(ref)
    return hints


def _clean_reference_hint(value: str) -> str | None:
    cleaned = re.sub(r"[^A-Z0-9/-]+$", "", value.strip().upper())
    cleaned = cleaned.strip("-/")
    if len(cleaned) < 6:
        return None
    if not any(char.isdigit() for char in cleaned):
        return None
    if cleaned in {"BOOKING", "NUMBER", "UNKNOWN"}:
        return None
    return cleaned


def _env_int(key: str, *, default: int, minimum: int) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_float(key: str, *, default: float, minimum: float) -> float:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def _extract_first_url(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"https?://\S+", value)
    if not match:
        return None
    return match.group(0).rstrip(".,;)")


def _compute_snapshot_hash(
    *,
    shipment: ShipmentRef,
    status: ShipmentStatus,
    status_value: str,
    eta_text: str,
    eta_port_local_text: str,
    vessel_voyage_value: str | None = None,
) -> str:
    latest_move = status.latest_move
    snapshot = {
        "line": shipment.shipping_line,
        "status_value": status_value,
        "status_text": status.status_text or "",
        "eta_text": eta_text,
        "eta_port_local_text": eta_port_local_text,
        "location": status.location or "",
        "event_time": status.event_time.isoformat() if status.event_time else "",
        "movement_details": status.movement_details or "",
        "vessel_voyage": vessel_voyage_value or status.vessel_voyage or "",
        "booking_status_text": status.booking_status_text or "",
        "latest_move_name": latest_move.name if latest_move else "",
        "latest_move_location": latest_move.location if latest_move else "",
        "latest_move_time": _format_port_local_time(
            latest_move.event_time_local_text if latest_move else None,
            latest_move.event_time if latest_move else None,
        ),
    }
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
