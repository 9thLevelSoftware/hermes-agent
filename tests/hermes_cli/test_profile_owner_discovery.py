"""Owner discovery must resolve a profile home to the launch home that publishes the owner.

The single host gateway serves every profile over a control socket and publishes exactly one
owner record, in its launch home. Discovering a profile home directly therefore finds no
owner record and reports `starting` forever, which is what made every non-default profile
unable to start a turn ("Gateway starting: deadline").
"""
import importlib.util
from pathlib import Path

HERMES_AGENT = Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, HERMES_AGENT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_profile_home_resolves_to_launch_home_owner(tmp_path, monkeypatch):
    discovery = _load("gw_disc", "hermes_cli/gateway_runtime_discovery.py")
    from gateway import status as gw_status

    launch = tmp_path / ".hermes"
    profile = launch / "profiles" / "phoenixworker"
    launch.mkdir(parents=True)
    profile.mkdir(parents=True)

    (launch / "gateway_state.json").write_text(
        '{"gateway_state":"running","pid":12345,"served_profiles":["default","phoenixworker"]}'
    )
    (profile / "gateway.lock").write_text("")

    monkeypatch.setattr(gw_status, "_is_gateway_runtime_lock_active_strict", lambda lock: True)

    state = discovery.missing_owner_state(profile)
    assert state != "starting", (
        "a profile home with an active lock and no local owner record must not be reported "
        "as 'starting' when its launch home publishes a live owner"
    )


def test_launch_home_still_reports_absent_when_unlocked(tmp_path, monkeypatch):
    discovery = _load("gw_disc", "hermes_cli/gateway_runtime_discovery.py")
    from gateway import status as gw_status
    home = tmp_path / "launch"
    home.mkdir()
    monkeypatch.setattr(gw_status, "_is_gateway_runtime_lock_active_strict", lambda lock: False)
    assert discovery.missing_owner_state(home) == "absent"


def test_profile_owner_mapping_cycle_raises_discovery_error(tmp_path):
    import pytest
    from hermes_cli.gateway_runtime import gateway_profile_home
    from hermes_cli.gateway_runtime_discovery import DiscoveryError

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "gateway.lock").symlink_to(second / "gateway.lock")
    (second / "gateway.lock").symlink_to(first / "gateway.lock")

    with pytest.raises(DiscoveryError, match="profile_home_cycle"):
        gateway_profile_home(first)
