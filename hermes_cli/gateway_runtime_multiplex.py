"""Which daemon a named-profile client must start when nothing serves its home.

A ``<root>/profiles/<name>`` home that the default multiplexer serves has no daemon of its own.
When the multiplexer is down (restart, update, crash) the profile looks ``absent`` and the ensure
loop would spawn a per-profile gateway for it — a second owner that answers this client and then
blocks the multiplexer's next all-or-nothing reserve (``Cannot reserve gateway profiles``). The
client must start the multiplexer instead, so the start target is decided here from the same facts
the multiplexer boots on (``gateway_multiplex_mode.host_serves_named_profile``): a profile's own
``gateway.standalone: true``, the default profile's explicit ``true``, or — when the flag is unset or
the retired ``false`` that boot settles like unset — the served set the last multiplexer recorded in
the root's ``gateway_state.json`` (a standalone default gateway clears it).
"""
from __future__ import annotations

from pathlib import Path


def multiplexer_root_for(home: Path) -> Path | None:
    """Default root that may multiplex *home*, when *home* is a named profile under it."""
    from hermes_constants import named_profile_home
    if named_profile_home(home) is None:
        return _offloaded_profile_root(home)
    root = home.parent.parent
    return root if root != home else None


def _offloaded_profile_root(home: Path) -> Path | None:
    """Root whose ``profiles/<name>`` the storage-offloaded *home* physically is, else None.

    Storage offload relocates a profile home so its ``profiles/<name>`` path resolves to a
    directory elsewhere (e.g. an external SSD). ``named_profile_home`` cannot see through the
    relocation, so any client holding the physical spelling (``WorkerRPC`` resolves its home)
    fell into ``missing_owner_state`` and read the offloaded home's own held ``gateway.lock`` as
    a forever-``starting`` owner — every managed worker then died at ``worker.adopt`` with
    ``owner_unavailable``. Match the default root's ``profiles/<name>`` by name plus real-path
    equality (never bare name), walking up from *home* like ``named_profile_home`` so subpaths
    resolve too. Both the env-derived root and the native default home are probed: managed
    workers run with ``HERMES_HOME`` set to their own profile home, so root discovery must not
    depend on the process env. Only default roots are consulted; a classic layout never
    reaches here.
    """
    from hermes_constants import _get_platform_default_hermes_home, get_default_hermes_root
    candidates = (home, *home.parents)
    for root in dict.fromkeys((Path(get_default_hermes_root()),
                              Path(_get_platform_default_hermes_home()))):
        try:
            profiles = root / "profiles"
            for candidate in candidates:
                if candidate.name.startswith("."):
                    continue
                match = profiles / candidate.name
                if match.exists() and match.resolve() == candidate.resolve():
                    return root if root != candidate else None
        except OSError:
            continue
    return None


def implied_host_root(home: Path) -> Path | None:
    """The root whose gateway must serve named profile *home* when nothing records a verdict.

    ``hermes gateway run`` refuses a ``profiles/<name>`` home a gateway of its own unless it authored
    ``gateway.standalone: true`` (or passes ``--force``); the retired ``multiplex_profiles: false``
    no longer opts out, so only a standalone profile keeps its own daemon.
    """
    root = multiplexer_root_for(home)
    if root is None:
        return None
    from hermes_cli.gateway_multiplex_mode import host_serves_named_profile
    return None if host_serves_named_profile(root, home) is False else root


def multiplexer_serves_home(home: Path) -> Path | None:
    """The root whose (possibly stopped) multiplexer serves *home*, else None.

    ``hermes_cli.gateway.named_profile_served_by_running_multiplexer`` answers the live question;
    this is its offline twin for the moment no gateway runs, deciding from the boot policy
    (:func:`hermes_cli.gateway_multiplex_mode.host_serves_named_profile`): a standalone secondary is
    never served, an explicit ``true`` serves the rest, and an unset or retired ``false`` flag falls
    back to the ``served_profiles`` the last multiplexer recorded. No evidence keeps it unanswered.
    """
    root = multiplexer_root_for(home)
    if root is None:
        return None
    from hermes_cli.gateway_multiplex_mode import host_serves_named_profile
    verdict = host_serves_named_profile(root, home)
    if verdict is not None:
        return root if verdict else None
    from gateway.status import read_runtime_status
    from hermes_cli.profiles import normalize_profile_name
    served = (read_runtime_status(root / "gateway_state.json") or {}).get("served_profiles")
    if not isinstance(served, list):
        return None
    names = {normalize_profile_name(str(p)) for p in served if p}
    return root if normalize_profile_name(home.name) in names else None
