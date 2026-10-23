from dataclasses import dataclass, field
import os


DEFAULT_SHIPMENT_TERMINAL_STATUSES = (
    "blocked",
    "cancelado",
    "booking canceled",
    "booking cancelled",
    "canceled",
    "cancelled",
    "vacío devuelto",
    "vacio devuelto",
    "empty returned",
    "embarque cerrado",
    "closed",
    "completo en wf pagado",
)


@dataclass
class Settings:
    clickup_api_token: str | None
    clickup_oauth_access_token: str | None
    clickup_oauth_client_id: str | None
    clickup_oauth_client_secret: str | None
    clickup_oauth_redirect_uri: str | None
    clickup_list_id: str
    clickup_list_ids: list[str]
    clickup_team_id: str | None
    clickup_space_ids: list[str]
    clickup_folder_ids: list[str]
    clickup_discover_from_spaces: bool
    clickup_discover_from_team: bool
    cf_container_no: str
    cf_booking_no: str
    cf_shipping_line: str
    cf_shipment_status: str | None
    cf_status_last_checked: str | None
    cf_track_trace_snapshot: str | None
    cf_eta: str | None
    cf_etd: str | None
    cf_discharge_date: str | None
    cf_gate_in_full: str | None
    cf_gate_out_empty: str | None
    cf_gate_out_delivery: str | None
    cf_gate_in_empty: str | None
    cf_vessel_voyage: str | None = None
    cf_master_bl: str | None = "8f9d6623-4723-482b-84ba-180dfba29643"
    clickup_include_closed: bool = False
    clickup_include_archived: bool = False
    clickup_use_task_status: bool = False
    clickup_task_status_on_update: str | None = None
    clickup_status_pending_booking: str = "Pendiente de booking"
    clickup_status_booking_confirmed: str = "BK confirmado"
    clickup_status_collected: str = "Recolectado"
    clickup_status_origin_port: str = "En puerto Origen"
    clickup_status_in_transit: str = "Tránsito"
    clickup_status_arriving: str = "Por arribar"
    clickup_status_arrived_port: str = "arribado en puerto"
    clickup_status_en_route_warehouse: str = "en ruta a almacén"
    clickup_status_in_warehouse: str = "en almacén"
    clickup_status_empty_returned: str = "Vacío devuelto"
    status_comment_prefix: str = "Shipment update"
    eta_only_mode: bool = True
    recent_moves_limit: int = 0
    shipment_allowed_lines: list[str] | None = None
    shipment_excluded_lines: list[str] | None = None
    shipment_terminal_statuses: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_SHIPMENT_TERMINAL_STATUSES
    )
    shipment_skip_unsupported_lines: bool = True
    shipment_preflight_enabled: bool = True
    shipment_preflight_timeout_seconds: int = 8
    shipment_min_sync_interval_hours: int = 0
    shipment_comment_on_no_change: bool = False
    shipment_audit_db_path: str | None = None
    shipment_audit_source: str | None = None
    wan_hai_reference_hints_from_comments: bool = True
    wan_hai_reference_comment_limit: int = 5
    wan_hai_manual_capture_comment: bool = True
    wan_hai_manual_capture_comment_cooldown_hours: int = 24
    wan_hai_manual_capture_url: str = "https://www.wanhai.com/views/quick/cargo_tracking.xhtml"
    clickup_discovery_validate_schema: bool = True
    clickup_discovery_list_name_include: list[str] | None = None
    clickup_discovery_list_name_exclude: list[str] | None = None
    clickup_discovery_cache_path: str | None = None
    clickup_discovery_cache_ttl_seconds: int = 86400
    clickup_max_lists_per_run: int = 100
    clickup_max_pages_per_list: int = 250
    clickup_max_tasks_per_list: int = 25_000
    clickup_max_total_tasks: int = 50_000

    @property
    def clickup_auth_header_value(self) -> str:
        if self.clickup_oauth_access_token:
            return f"Bearer {self.clickup_oauth_access_token}"
        if self.clickup_api_token:
            return self.clickup_api_token
        raise ValueError(
            "Missing ClickUp credentials. Set CLICKUP_OAUTH_ACCESS_TOKEN "
            "or CLICKUP_API_TOKEN."
        )

    @property
    def clickup_auth_mode(self) -> str:
        if self.clickup_oauth_access_token:
            return "oauth"
        if self.clickup_api_token:
            return "personal_token"
        return "missing"

    @classmethod
    def from_env(cls) -> "Settings":
        primary_list_id = _must("CLICKUP_LIST_ID")
        parsed_list_ids = _csv("CLICKUP_LIST_IDS")
        if primary_list_id not in parsed_list_ids:
            parsed_list_ids.insert(0, primary_list_id)

        clickup_api_token = _optional("CLICKUP_API_TOKEN")
        clickup_oauth_access_token = _optional("CLICKUP_OAUTH_ACCESS_TOKEN")
        if not clickup_oauth_access_token and not clickup_api_token:
            raise ValueError(
                "Missing ClickUp credentials. Set CLICKUP_OAUTH_ACCESS_TOKEN "
                "or CLICKUP_API_TOKEN."
            )

        cf_shipment_status = _optional("CLICKUP_CF_SHIPMENT_STATUS")
        cf_status_last_checked = _optional("CLICKUP_CF_STATUS_LAST_CHECKED")
        cf_track_trace_snapshot = _optional("CLICKUP_CF_TRACK_TRACE_SNAPSHOT")
        if cf_track_trace_snapshot and cf_status_last_checked and cf_track_trace_snapshot == cf_status_last_checked:
            cf_track_trace_snapshot = None

        return cls(
            clickup_api_token=clickup_api_token,
            clickup_oauth_access_token=clickup_oauth_access_token,
            clickup_oauth_client_id=_optional("CLICKUP_OAUTH_CLIENT_ID"),
            clickup_oauth_client_secret=_optional("CLICKUP_OAUTH_CLIENT_SECRET"),
            clickup_oauth_redirect_uri=_optional("CLICKUP_OAUTH_REDIRECT_URI"),
            clickup_list_id=primary_list_id,
            clickup_list_ids=parsed_list_ids,
            clickup_team_id=_optional("CLICKUP_TEAM_ID"),
            clickup_space_ids=_csv("CLICKUP_SPACE_IDS"),
            clickup_folder_ids=_csv("CLICKUP_FOLDER_IDS"),
            clickup_discover_from_spaces=_bool("CLICKUP_DISCOVER_LISTS_FROM_SPACES", default=True),
            clickup_discover_from_team=_bool("CLICKUP_DISCOVER_LISTS_FROM_TEAM", default=False),
            clickup_discovery_validate_schema=_bool("CLICKUP_DISCOVERY_VALIDATE_SCHEMA", default=True),
            clickup_discovery_list_name_include=_csv_optional(
                "CLICKUP_DISCOVERY_LIST_NAME_INCLUDE",
                default=["shipment", "maritimo"],
            ),
            clickup_discovery_list_name_exclude=_csv_optional("CLICKUP_DISCOVERY_LIST_NAME_EXCLUDE"),
            clickup_discovery_cache_path=_optional("CLICKUP_DISCOVERY_CACHE_PATH"),
            clickup_discovery_cache_ttl_seconds=_int(
                "CLICKUP_DISCOVERY_CACHE_TTL_SECONDS",
                default=86400,
                min_value=0,
            ),
            clickup_max_lists_per_run=_int("CLICKUP_MAX_LISTS_PER_RUN", default=100),
            clickup_max_pages_per_list=_int("CLICKUP_MAX_PAGES_PER_LIST", default=250),
            clickup_max_tasks_per_list=_int("CLICKUP_MAX_TASKS_PER_LIST", default=25_000),
            clickup_max_total_tasks=_int("CLICKUP_MAX_TOTAL_TASKS", default=50_000),
            cf_container_no=_must("CLICKUP_CF_CONTAINER_NO"),
            cf_booking_no=_must("CLICKUP_CF_BOOKING_NO"),
            cf_master_bl=_optional("CLICKUP_CF_MASTER_BL") or "8f9d6623-4723-482b-84ba-180dfba29643",
            cf_shipping_line=_must("CLICKUP_CF_SHIPPING_LINE"),
            cf_shipment_status=cf_shipment_status,
            cf_status_last_checked=cf_status_last_checked,
            cf_track_trace_snapshot=cf_track_trace_snapshot,
            cf_eta=_optional("CLICKUP_CF_ETA"),
            cf_etd=_optional("CLICKUP_CF_ETD"),
            cf_discharge_date=_optional("CLICKUP_CF_DISCHARGE_DATE"),
            cf_gate_in_full=_optional("CLICKUP_CF_GATE_IN_FULL"),
            cf_gate_out_empty=_optional("CLICKUP_CF_GATE_OUT_EMPTY"),
            cf_gate_out_delivery=_optional("CLICKUP_CF_GATE_OUT_DELIVERY"),
            cf_gate_in_empty=_optional("CLICKUP_CF_GATE_IN_EMPTY"),
            cf_vessel_voyage=_optional("CLICKUP_CF_VESSEL_VOYAGE"),
            clickup_include_closed=_bool("CLICKUP_INCLUDE_CLOSED", default=False),
            clickup_include_archived=_bool("CLICKUP_INCLUDE_ARCHIVED", default=False),
            clickup_use_task_status=_bool("CLICKUP_USE_TASK_STATUS", default=False),
            clickup_task_status_on_update=_optional("CLICKUP_TASK_STATUS_ON_UPDATE"),
            clickup_status_pending_booking=os.getenv("CLICKUP_STATUS_PENDING_BOOKING", "Pendiente de booking"),
            clickup_status_booking_confirmed=os.getenv("CLICKUP_STATUS_BOOKING_CONFIRMED", "BK confirmado"),
            clickup_status_collected=os.getenv("CLICKUP_STATUS_COLLECTED", "Recolectado"),
            clickup_status_origin_port=os.getenv("CLICKUP_STATUS_ORIGIN_PORT", "En puerto Origen"),
            clickup_status_in_transit=os.getenv("CLICKUP_STATUS_IN_TRANSIT", "Tránsito"),
            clickup_status_arriving=os.getenv("CLICKUP_STATUS_ARRIVING", "Por arribar"),
            clickup_status_arrived_port=os.getenv("CLICKUP_STATUS_ARRIVED_PORT", "arribado en puerto"),
            clickup_status_en_route_warehouse=os.getenv("CLICKUP_STATUS_EN_ROUTE_WAREHOUSE", "en ruta a almacén"),
            clickup_status_in_warehouse=os.getenv("CLICKUP_STATUS_IN_WAREHOUSE", "en almacén"),
            clickup_status_empty_returned=os.getenv("CLICKUP_STATUS_EMPTY_RETURNED", "Vacío devuelto"),
            status_comment_prefix=os.getenv("CLICKUP_STATUS_COMMENT_PREFIX", "Shipment update"),
            eta_only_mode=_bool("SHIPMENT_ETA_ONLY", default=True),
            recent_moves_limit=_int("SHIPMENT_RECENT_MOVES_LIMIT", default=0, min_value=0),
            shipment_allowed_lines=_csv_normalized("SHIPMENT_ALLOWED_LINES"),
            shipment_excluded_lines=_csv_normalized("SHIPMENT_EXCLUDED_LINES"),
            shipment_terminal_statuses=_csv_tuple(
                "SHIPMENT_TERMINAL_STATUSES",
                default=DEFAULT_SHIPMENT_TERMINAL_STATUSES,
            ),
            shipment_skip_unsupported_lines=_bool("SHIPMENT_SKIP_UNSUPPORTED_LINES", default=True),
            shipment_preflight_enabled=_bool("SHIPMENT_PREFLIGHT_ENABLED", default=True),
            shipment_preflight_timeout_seconds=_int("SHIPMENT_PREFLIGHT_TIMEOUT_SECONDS", default=8, min_value=1),
            shipment_min_sync_interval_hours=_int("SHIPMENT_MIN_SYNC_INTERVAL_HOURS", default=0, min_value=0),
            shipment_comment_on_no_change=_bool("SHIPMENT_COMMENT_ON_NO_CHANGE", default=False),
            shipment_audit_db_path=_optional("SHIPMENT_AUDIT_DB_PATH"),
            shipment_audit_source=_optional("SHIPMENT_AUDIT_SOURCE"),
            wan_hai_reference_hints_from_comments=_bool("WAN_HAI_REFERENCE_HINTS_FROM_COMMENTS", default=True),
            wan_hai_reference_comment_limit=_int("WAN_HAI_REFERENCE_COMMENT_LIMIT", default=5, min_value=0),
            wan_hai_manual_capture_comment=_bool("WAN_HAI_MANUAL_CAPTURE_COMMENT", default=True),
            wan_hai_manual_capture_comment_cooldown_hours=_int(
                "WAN_HAI_MANUAL_CAPTURE_COMMENT_COOLDOWN_HOURS",
                default=24,
                min_value=0,
            ),
            wan_hai_manual_capture_url=os.getenv(
                "WAN_HAI_MANUAL_CAPTURE_URL",
                "https://www.wanhai.com/views/quick/cargo_tracking.xhtml",
            ),
        )


def _must(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Missing required env var: {key}")
    return value


def _optional(key: str) -> str | None:
    value = os.getenv(key)
    if not value:
        return None
    stripped = value.strip()
    return stripped or None


def _csv(key: str) -> list[str]:
    value = os.getenv(key, "")
    return [x.strip() for x in value.split(",") if x.strip()]


def _csv_optional(key: str, *, default: list[str] | None = None) -> list[str] | None:
    if key not in os.environ:
        return default
    items = _csv(key)
    return items or None


def _csv_normalized(key: str) -> list[str] | None:
    items = [x.strip().lower() for x in _csv(key) if x.strip()]
    if not items:
        return None
    return list(dict.fromkeys(items))


def _csv_tuple(key: str, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if key not in os.environ:
        return default
    items = tuple(dict.fromkeys(x.strip() for x in _csv(key) if x.strip()))
    return items or default


def _bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(key: str, default: int, *, min_value: int = 1) -> int:
    value = os.getenv(key)
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except Exception:
        return default
    return parsed if parsed >= min_value else default
