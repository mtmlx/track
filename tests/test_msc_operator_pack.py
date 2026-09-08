from datetime import datetime, timedelta, timezone
import json

import pytest

from shipment_sync.msc_operator_pack import build_package, main
from test_msc_browser_assisted import _capture


@pytest.fixture
def setup_capture(tmp_path):
    now = datetime.now(timezone.utc)
    manifest = {
        "schema_version": 1, "task_id": "testtask", "task_name": "TEST ONLY",
        "list_id": "testlist", "booking_no": "TESTBOOK", "operator": "test operator",
        "destination_port": "Miami, US", "containers": ["MSCU1234567"],
        "task_reviewed_at": (now-timedelta(minutes=10)).isoformat(),
        "captured_at": (now-timedelta(minutes=5)).isoformat(),
    }
    directory = tmp_path / "captures"
    directory.mkdir()
    (directory / "MSCU1234567.txt").write_text(_capture("MSCU1234567", "19/09/2026", "ANTWERP 81W"))
    return manifest, directory, now


def test_package_is_existing_importer_compatible(setup_capture, tmp_path):
    from shipment_sync.msc_browser_assisted import read_import_batch
    manifest, directory, now = setup_capture
    batch, report = build_package(manifest, directory, now=now)
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch))
    captures, failures = read_import_batch(path)
    assert len(captures) == 1 and not failures
    assert captures[0].task_id == "testtask"
    assert report["destination_port"] == "Miami, US"
    assert len(report["capture_sha256"]["MSCU1234567.txt"]) == 64


@pytest.mark.parametrize("change", [
    {"destination_port": "HNPCR"}, {"destination_port": "unknown"},
    {"containers": ["MSCU1234567", "MSCU7654321"]},
    {"containers": ["MSCU1234567", "MSCU1234567"]},
    {"containers": ["../../secret"]}, {"task_id": "MTMLXGT-27395"},
    {"captured_at": "2020-01-01T00:00:00Z"},
    {"captured_at": "2999-01-01T00:00:00Z"},
    {"captured_at": "2026-09-08T00:00:00"},
])
def test_rejects_invalid_manifest(setup_capture, change):
    manifest, directory, now = setup_capture
    manifest.update(change)
    with pytest.raises(ValueError):
        build_package(manifest, directory, now=now)


def test_rejects_wrong_visible_container(setup_capture):
    manifest, directory, now = setup_capture
    path = directory / "MSCU1234567.txt"
    path.write_text(path.read_text().replace("MSCU1234567", "MSCU7654321"))
    with pytest.raises(ValueError, match="approved container"):
        build_package(manifest, directory, now=now)


def test_cli_is_offline_private_and_no_overwrite(setup_capture, tmp_path, monkeypatch):
    import hashlib
    import requests
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline packaging must not use the network")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    manifest, directory, _ = setup_capture
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    output = tmp_path / "handoff"
    monkeypatch.setattr("sys.argv", ["pack", "--manifest", str(path), "--captures-dir", str(directory), "--output-dir", str(output)])
    main()
    report = json.loads((output / "report.json").read_text())
    assert report["batch_sha256"] == hashlib.sha256((output / "batch.json").read_bytes()).hexdigest()
    assert (output.stat().st_mode & 0o777) == 0o700
    assert ((output / "batch.json").stat().st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):
        main()
