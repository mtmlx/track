import json
import hashlib
from pathlib import Path
import signal
from urllib.parse import urlparse
import requests
from shipment_sync.config import Settings
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.carriers.registry import build_carrier_registry
from shipment_sync import movement_archive
from shipment_sync.scheduled_movement_archive import read_only_scope

signal.alarm(240)
original_send = requests.Session.send
counts = {'clickup_reads': 0, 'blocked_writes': 0}
def guarded_send(self, request, **kwargs):
    host = (urlparse(request.url).hostname or '').lower()
    if host == 'clickup.com' or host.endswith('.clickup.com'):
        if request.method.upper() not in {'GET', 'HEAD'}:
            counts['blocked_writes'] += 1
            raise RuntimeError('Read-only pilot blocked a ClickUp write')
        counts['clickup_reads'] += 1
    return original_send(self, request, **kwargs)
requests.Session.send = guarded_send
client = ClickUpClient(Settings.from_env())
lists, line = read_only_scope()
if lists:
    publisher = movement_archive.MovementArchivePublisher.from_env()
    assert publisher is not None
    assert hashlib.sha256(Path(movement_archive.__file__).read_bytes()).hexdigest() == publisher.release
    archive_lists = {str(entry['binding']['list_id']) for entry in publisher.catalog()['tasks'].values()}
    assert lists <= archive_lists
    print(json.dumps({'movement_publisher':'verified', 'publisher_carrier':line, 'operational_writes':0}))
shipments = client.list_shipments()
if not shipments:
    print(json.dumps({'result':'inconclusive','reason':'no candidate shipments',**counts}))
    raise SystemExit(2)
shipment = shipments[0]
adapter = build_carrier_registry().get(shipment.shipping_line)
if adapter is None:
    raise RuntimeError('No adapter for candidate carrier')
status = adapter.fetch_status(shipment)
plan = client.plan_shipment_update(shipment, status)
assert counts['blocked_writes'] == 0
print(json.dumps({'result':'passed','candidate_count':len(shipments),'carrier_checked':shipment.shipping_line,'shipments_previewed':1,'planned_field_count':len(plan.custom_field_updates),'operational_writes':0,**counts}))
