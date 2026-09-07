from __future__ import annotations

import json

import pytest

from shipment_sync.dcsa_shadow_main import main


def test_shadow_cli_config_check_is_disabled_and_does_not_require_external_settings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DCSA_TNT_SHADOW_ENABLED", "false")
    monkeypatch.setenv("DCSA_TNT_SHADOW_CARRIERS", "")
    monkeypatch.delenv("DCSA_TNT_SHADOW_MAERSK_VERSION", raising=False)
    monkeypatch.setattr("sys.argv", ["dcsa-tnt-shadow"])

    main()

    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "carrier_versions": {},
        "clickup_writes": False,
        "enabled": False,
        "max_shipments": 25,
        "mode": "config-check",
    }


def test_shadow_cli_refuses_a_run_without_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DCSA_TNT_SHADOW_ENABLED", "false")
    monkeypatch.setenv("DCSA_TNT_SHADOW_CARRIERS", "cma cgm")
    monkeypatch.setattr("sys.argv", ["dcsa-tnt-shadow", "--run"])

    with pytest.raises(ValueError, match="DCSA shadow run is disabled"):
        main()
