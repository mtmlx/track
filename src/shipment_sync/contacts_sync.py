from __future__ import annotations

from .terminal import terminal_safe_text

from dataclasses import dataclass
import sys

from .clickup_contacts_client import ClickUpContactsClient
from .icloud_carddav import iCloudCardDAVClient


@dataclass
class ContactSyncStats:
    total_candidates: int
    upserted: int
    skipped: int
    errors: list[str]


def run_contacts_sync(
    clickup_client: ClickUpContactsClient, icloud_client: iCloudCardDAVClient | None, *, dry_run: bool
) -> ContactSyncStats:
    contacts = clickup_client.list_contacts()
    upserted = 0
    skipped = 0
    errors: list[str] = []

    for contact in contacts:
        summary = (
            f"{contact.full_name} | email={contact.email or 'n/a'} | phone={contact.phone or 'n/a'} "
            f"| clickup_task={contact.task_id}"
        )
        if dry_run:
            print(terminal_safe_text(f"DRY RUN: {summary}"))
            skipped += 1
            continue

        try:
            if icloud_client is None:
                raise RuntimeError("iCloud client is not configured")
            response = icloud_client.upsert_contact(contact)
            upserted += 1
            print(terminal_safe_text(f"UPSERTED ({response.status_code}): {summary}"))
        except Exception as exc:
            errors.append(f"{contact.task_id}: {exc}")
            print(terminal_safe_text(f"ERROR: {summary} -> {exc}"), file=sys.stderr)

    return ContactSyncStats(
        total_candidates=len(contacts),
        upserted=upserted,
        skipped=skipped,
        errors=errors,
    )
