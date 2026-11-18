"""Publish Maersk container evidence without changing shipment fields or status."""

from .container_progress import container_progress, progress_comment
from .one_write_holds import write_hold_reason
from .tracking_failures import carrier_name


def publish_maersk_register(client, shipment, adapter):
    if (carrier_name(shipment.shipping_line) != "Maersk" or write_hold_reason(shipment)
            or not shipment.booking_no or not shipment.destination_port
            or (shipment.expected_container_count or 0) < 2):
        return "ineligible"
    progress = container_progress(
        shipment, adapter.fetch_container_progress_status(shipment), preview=True)
    if not progress or not progress["population_verified"]:
        return "unverified"
    report = progress_comment(progress)
    marker = report.splitlines()[-1]
    latest = next((text for text in client._fetch_recent_task_comment_texts(shipment.task_id, limit=25)
                   if "Container register fingerprint: " in text), "")
    if marker in latest.splitlines():
        return "unchanged"
    refs = client.get_shipments_by_task_ids([shipment.task_id], allowed_list_ids={shipment.list_id})
    def identity(ref):
        return (ref.shipping_line, ref.booking_no, ref.container_no,
                ref.expected_container_count, ref.destination_port, ref.current_task_status)
    if len(refs) != 1 or identity(refs[0]) != identity(shipment) or write_hold_reason(refs[0]):
        return "identity_changed"
    client._post_comment(shipment.task_id,
        "Maersk carrier evidence. Unconfirmed means no qualifying actual event was verified; "
        "it does not prove the movement did not occur.\n\n" + report)
    return "published"
