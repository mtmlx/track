from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ShipmentRef:
    task_id: str
    task_name: str
    shipping_line: str
    booking_no: str | None
    container_no: str | None
    list_id: str
    list_name: str | None = None
    last_checked_at: datetime | None = None
    current_status_value: str | None = None
    current_task_status: str | None = None
    track_trace_snapshot_hash: str | None = None
    expected_container_count: int | None = None
    current_field_values: dict[str, Any] = field(default_factory=dict)
    reference_hints: list[str] = field(default_factory=list)
    destination_port: str | None = None


@dataclass
class ShipmentStatus:
    status_text: str
    location: str | None = None
    event_time: datetime | None = None
    eta_time: datetime | None = None
    eta_local_text: str | None = None
    latest_move: "MovementEvent | None" = None
    recent_moves: list["MovementEvent"] = field(default_factory=list)
    discovered_containers: list[str] = field(default_factory=list)
    container_discovery_authoritative: bool = False
    raw_source: str | None = None
    source_url: str | None = None
    movement_details: str | None = None
    vessel_voyage: str | None = None
    final_vessel_voyage: str | None = None
    booking_status_text: str | None = None
    destination_port: str | None = None
    require_destination_evidence: bool = False


@dataclass
class MovementEvent:
    name: str
    location: str | None = None
    event_time: datetime | None = None
    event_time_local_text: str | None = None
    event_state: str | None = None
    vessel_voyage: str | None = None
    source_event_name: str | None = None
    location_code: str | None = None


@dataclass
class ShipmentWriteResult:
    changed: bool
    status_value: str
    snapshot_hash: str


@dataclass
class ShipmentFieldWrite:
    field_id: str
    value: Any
    field_type: str = "text"
    label: str | None = None


@dataclass
class ShipmentUpdatePlan:
    changed: bool
    status_value: str
    snapshot_hash: str
    custom_field_updates: list[ShipmentFieldWrite] = field(default_factory=list)
    task_status_update: str | None = None
    comment_text: str | None = None
