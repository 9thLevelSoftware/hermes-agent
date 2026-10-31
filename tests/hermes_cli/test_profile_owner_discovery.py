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


def _offloaded_home(tmp_path):
    """A storage-offloaded profile home: its ``profiles/<name>`` path resolves to a directory
    elsewhere (external SSD), which is how the macos storage offload relocates homes."""
    launch = tmp_path / ".hermes"
    physical = tmp_path / "offload" / "phoenixworker"
    (launch / "profiles").mkdir(parents=True)
    physical.mkdir(parents=True)
    (launch / "profiles" / "phoenixworker").symlink_to(physical, target_is_directory=True)
    return launch, physical


def test_offloaded_physical_home_maps_to_launch_root(tmp_path, monkeypatch):
    """`WorkerRPC` resolves its home, so every managed worker holds the PHYSICAL spelling of an
    offloaded home. `multiplexer_root_for` must still find the launch root that multiplexes it,
    or `_served_by_multiplexer` bails and `missing_owner_state` reads the offloaded home's own
    held gateway.lock as a forever-'starting' owner — every worker then died at `worker.adopt`."""
    multi = _load("gw_multiplex", "hermes_cli/gateway_runtime_multiplex.py")
    launch, physical = _offloaded_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch))

    assert multi.multiplexer_root_for(physical) == launch
    assert multi.multiplexer_root_for(physical / "workspace") == launch
    assert multi.multiplexer_root_for(tmp_path / "offload" / "other") is None


def test_offloaded_physical_home_missing_owner_matches_logical(tmp_path, monkeypatch):
    """Both spellings of the same offloaded home must reach the same missing-owner verdict."""
    discovery = _load("gw_disc_offload", "hermes_cli/gateway_runtime_discovery.py")
    from gateway import status as gw_status

    launch, physical = _offloaded_home(tmp_path)
    (launch / "gateway_state.json").write_text(
        '{"gateway_state":"running","pid":12345,"served_profiles":["default","phoenixworker"]}'
    )
    (physical / "gateway.lock").write_text("")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(gw_status, "_is_gateway_runtime_lock_active_strict", lambda lock: True)

    logical = launch / "profiles" / "phoenixworker"
    assert discovery.missing_owner_state(physical) == discovery.missing_owner_state(logical), (
        "the physical spelling of an offloaded home must not read its own held gateway.lock "
        "as a starting owner when the logical spelling resolves through the launch home"
    )


def test_offloaded_root_found_under_worker_env(tmp_path, monkeypatch):
    """Managed workers run with HERMES_HOME set to their profile home (and the bootstrap
    contract pins frame['home'] to the physical spelling), so root discovery must not depend
    on the process env: the native default home is the launch root that mirrors the home
    under profiles/<name>."""
    import hermes_constants
    multi = _load("gw_multiplex_worker_env", "hermes_cli/gateway_runtime_multiplex.py")
    launch, physical = _offloaded_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(physical))
    monkeypatch.setattr(hermes_constants, "_get_platform_default_hermes_home", lambda: launch)

    assert multi.multiplexer_root_for(physical) == launch, (
        "an offloaded profile home must map to the launch root even when HERMES_HOME "
        "points at the profile home itself (the managed-worker environment)"
    )
