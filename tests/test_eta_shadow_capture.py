from dataclasses import replace
from datetime import datetime, timezone

import pytest

from shipment_sync.eta_shadow_capture import (
    EtaShadowCapture, SqliteObservationStore, DynamoObservationStore, decode_observation,
)
from shipment_sync.models import ShipmentRef

NOW = datetime(2026, 10, 26, 20, tzinfo=timezone.utc)


def shipment(**kwargs):
    args = dict(task_id="pilot-1", task_name="private", shipping_line="ONE", booking_no="BOOK1",
                container_no="ONEU1234567", list_id="list-1", destination_port="Puerto Quetzal",
                expected_container_count=1, current_task_status="tránsito")
    return ShipmentRef(**(args | kwargs))


def event(code="ARRI", classifier="EST", port="GTPRQ", when="2026-11-10T06:00:00-06:00", mode="VESSEL"):
    return {"eventID":"evt-1", "eventType":"TRANSPORT", "transportEventTypeCode":code,
        "eventClassifierCode":classifier, "eventDateTime":when, "eventCreatedDateTime":"2026-10-25T10:00:00Z",
        "equipmentReference":"ONEU1234567", "documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"BOOK1"}],
        "transportCall":{"UNLocationCode":port,"modeOfTransport":mode,"vessel":{"vesselName":"TEST VESSEL"}},
        "authorization":"must-not-persist", "nested":{"clientSecret":"also-private"}}


class Source:
    def __init__(self, events=None, error=None):
        self.events, self.error, self.calls = events if events is not None else [event()], error, []

    def fetch(self, task, **kwargs):
        self.calls.append(task)
        if self.error:raise self.error
        return self.events, "https://apix.one-line.com/v2/events?apikey=private", "carrier-native", {}


def runner(tmp_path, source=None):
    store = SqliteObservationStore(str(tmp_path / "shadow.sqlite"))
    return EtaShadowCapture(store, source=source or Source(), clock=lambda: NOW), store


def capture(r, task=None, run="run-1"):
    return r.run([task or shipment()], run_id=run, allowed_task_ids={(task or shipment()).task_id})


def item(store):
    import sqlite3, json
    with sqlite3.connect(store.path) as c:
        return json.loads(c.execute("SELECT body FROM eta_observations ORDER BY rowid DESC LIMIT 1").fetchone()[0])


def test_raw_evidence_and_revisions_are_retained_without_projection(tmp_path):
    source=Source();r,store=runner(tmp_path,source)
    task=shipment();before=repr(task)
    assert capture(r,task)["created"]==1
    body=decode_observation(item(store))
    assert body["events"][0]["authorization"]=="[REDACTED]"
    assert body["events"][0]["nested"]["clientSecret"]=="[REDACTED]"
    assert body["source_endpoint"]=="https://apix.one-line.com/v2/events"
    assert body["normalized_events"][0]["forecast_pod_arrival"] is True
    assert body["original_confirmed_eta"] is None
    assert repr(task)==before
    assert capture(r,task)["reused"]==1 and len(source.calls)==1
    # Identical observations at a later poll remain new evidence, not event-level duplicates.
    assert capture(r,task,"run-2")["created"]==1
    source.events=[event(when="2026-11-20T06:00:00-06:00")]
    assert capture(r,task,"run-3")["created"]==1
    assert decode_observation(item(store))["events"][0]["eventDateTime"].startswith("2026-11-20")


@pytest.mark.parametrize("kwargs",[
    {"tags":("legal case",)}, {"tags":("legal-case-do-not-update",)},
    {"task_id":"86e3bj80p","shipping_line":"MSC"}, {"shipping_line":"Maersk"},
    {"current_task_status":"cancelled"},
])
def test_exclusions_never_call_source_or_persist(tmp_path, kwargs):
    source=Source();r,store=runner(tmp_path,source)
    r.terminal_statuses={"cancelled"}
    result=capture(r,shipment(**kwargs))
    assert result["created"]==0 and result["skipped"]==1 and not source.calls


def test_explicit_one_hold_never_calls_source(tmp_path,monkeypatch):
    monkeypatch.setattr("shipment_sync.eta_shadow_capture.write_hold_reason",lambda _:"hold")
    source=Source();r,_=runner(tmp_path,source)
    assert capture(r)["skipped"]==1 and not source.calls


@pytest.mark.parametrize("kwargs",[
    {"booking_no":"BOOK1, BOOK2"}, {"booking_no":None},
    {"container_no":"ONEU1234567, ONEU1234567"}, {"expected_container_count":3},
    {"container_no":"TBC"}, {"container_no":"ONEU1234567 + OTHER"},
])
def test_identity_mismatch_is_notice_only_evidence_not_a_lookup(tmp_path,kwargs):
    source=Source();r,store=runner(tmp_path,source)
    result=capture(r,shipment(**kwargs))
    assert result["failures"]==1 and not source.calls
    assert decode_observation(item(store))["issues"]==["ambiguous_shipment_identity"]


@pytest.mark.parametrize("raw,arrived,departed,forecast,issue",[
    (event("ARRI","ACT","GTPRQ","2026-10-25T01:00:00Z"),True,False,False,None),
    (event("ARRI","ACT","PACON","2026-10-25T01:00:00Z"),False,False,False,None),
    (event("DISC","ACT","GTPRQ","2026-10-25T01:00:00Z"),False,False,False,None),
    (event("DEPA","ACT","CNNGB","2026-10-01T01:00:00Z"),False,True,False,None),
    (event("DEPA","ACT","CNNGB","2026-10-01T01:00:00Z","TRUCK"),False,False,False,None),
    (event("ARRI","ACT","GTPRQ","2026-11-25T01:00:00Z"),False,False,False,"future_actual"),
    (event(when="2026-11-10T06:00:00"),False,False,False,"missing_event_timezone"),
    (event(classifier="NEW"),False,False,False,"unknown_classifier"),
])
def test_milestone_guards(tmp_path,raw,arrived,departed,forecast,issue):
    r,store=runner(tmp_path,Source([raw]));capture(r)
    body=decode_observation(item(store));normalized=body["normalized_events"][0]
    assert normalized["actual_pod_arrival"]==arrived
    assert normalized["actual_vessel_departure"]==departed
    assert normalized["forecast_pod_arrival"]==forecast
    if issue:assert issue in body["issues"]


def test_failed_lookup_is_preserved_without_exception_secret_and_retries_remain_failed(tmp_path):
    r,store=runner(tmp_path,Source(error=RuntimeError("Bearer secret https://private")))
    assert capture(r)["failures"]==1
    assert capture(r)["failures"]==1
    body=decode_observation(item(store))
    assert body["completeness"]=="unverified" and body["events"]==[]
    assert "secret" not in str(body)


def test_changed_identity_cannot_reuse_capture_id(tmp_path):
    r,_=runner(tmp_path);capture(r)
    with pytest.raises(ValueError,match="identity changed"):
        capture(r,shipment(destination_port="Puerto Cortes"))


def test_capture_is_bounded_and_explicit(tmp_path):
    r,_=runner(tmp_path)
    with pytest.raises(ValueError):r.run([shipment()],run_id="run",allowed_task_ids=set())
    with pytest.raises(ValueError):r.run([shipment()],run_id="run",allowed_task_ids={"pilot-1"},max_shipments=11)
    result=r.run([shipment()],run_id="run",allowed_task_ids={"other"})
    assert result["selected"]==0


def test_corrupted_observation_cannot_pass_retry_validation(tmp_path):
    r,store=runner(tmp_path);capture(r)
    import sqlite3, json
    record=item(store);record['payload_gzip_base64']='corrupted'
    with sqlite3.connect(store.path) as conn:
        conn.execute('UPDATE eta_observations SET body=?',(json.dumps(record),))
    with pytest.raises(Exception):capture(r)


def test_cli_defaults_and_gate_need_no_inventory_or_store(monkeypatch,capsys):
    from shipment_sync import eta_shadow_capture_main as cli
    monkeypatch.setattr('sys.argv',['eta-shadow-capture'])
    monkeypatch.setattr(cli,'build_observation_store',lambda:pytest.fail('unexpected store'))
    cli.main()
    assert 'config-check' in capsys.readouterr().out
    monkeypatch.setattr('sys.argv',['eta-shadow-capture','--run'])
    monkeypatch.delenv('ETA_SHADOW_ENABLED',raising=False)
    with pytest.raises(SystemExit):cli.main()


def test_booking_only_is_not_a_physical_container_observation(tmp_path):
    r,store=runner(tmp_path,Source([event("ARRI","ACT","GTPRQ","2026-10-25T01:00:00Z")]))
    capture(r,shipment(container_no=None))
    assert not decode_observation(item(store))["normalized_events"][0]["actual_pod_arrival"]


def test_missing_allocation_and_pod_remain_unscorable(tmp_path):
    raw=event();raw.pop("documentReferences")
    r,store=runner(tmp_path,Source([raw]))
    capture(r,shipment(destination_port=None))
    body=decode_observation(item(store))
    assert set(body["issues"])=={"allocation_identity_not_proven","missing_authoritative_pod"}
    assert not body["normalized_events"][0]["forecast_pod_arrival"]


def test_dynamo_uses_immutable_put_and_isolated_namespaces(tmp_path):
    r,local=runner(tmp_path);capture(r);record=item(local)
    class Duplicate(Exception):
        response={"Error":{"Code":"ConditionalCheckFailedException"}}
    class Table:
        saved=None
        def get_item(self,**kwargs):return {"Item":self.saved} if self.saved else {}
        def put_item(self,**kwargs):
            assert kwargs["ConditionExpression"]=="attribute_not_exists(event_key)"
            if self.saved:raise Duplicate()
            self.saved=kwargs["Item"]
    table=Table();store=DynamoObservationStore(table)
    assert store.record(record) is True and store.record(record) is False
    assert record["carrier"].startswith("eta-capture#") and record["task_id"].startswith("eta-capture#")
    with pytest.raises(ValueError):store.record(record|{"content_hash":"wrong"})


def test_opaque_cma_booking_requires_complete_cross_query_proof(tmp_path):
    from shipment_sync.eta_shadow_capture import _cma_allocation
    raw=event();raw['documentReferences'][0]['documentReferenceValue']='a'*64
    task=shipment(shipping_line='CMA CGM')
    assert not _cma_allocation([raw],[],task,('ONEU1234567',))
    assert not _cma_allocation([raw],[raw],task,('ONEU1234567','ONEU7654321'))
    assert _cma_allocation([raw],[raw],task,('ONEU1234567',))
    unit_only=raw|{'eventID':'unit-only'}
    assert _cma_allocation([raw,unit_only],[raw],task,('ONEU1234567',))
    assert not _cma_allocation([unit_only],[raw],task,('ONEU1234567',))
    altered=raw|{'eventDateTime':'2026-12-01T12:00:00Z'}
    assert not _cma_allocation([altered],[raw],task,('ONEU1234567',))
    class Verified(Source):
        def fetch(self,task,**kwargs):
            return [raw], 'https://apis.cma-cgm.net/operation/trackandtrace/v1/events', 'CMA-TNT-2.2', {
                'kind':'cma_independent_booking_query','booking_query':'BOOK1','booking_events':[raw],
                'recorded_containers':['ONEU1234567']}
    r,store=runner(tmp_path,Verified());result=capture(r,task)
    assert result['needs_review']==0
    assert decode_observation(item(store))['normalized_events'][0]['identity_basis']=='independent_booking_and_container_queries'
