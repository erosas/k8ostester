"""Unit tests for the capability model — pure, no cluster."""
from k8ostester_kernel.control import Action, capabilities, is_enabled

ACTIONS = [
    Action("rotate", "Rotate", "ops", lambda s: s["ready"]),
    Action("upgrade", "Upgrade", "ops",
           lambda s: s["ready"] and s["version"] != s["target"] and not s["upgrading"]),
    Action("kill", "Kill primary", "chaos",
           lambda s: bool(s["primary"]) and not s["fault_in_flight"], destructive=True),
]


def state(**over):
    base = dict(ready=True, version="16.4", target="16.6", upgrading=False,
                primary="pg-1", fault_in_flight=False)
    return {**base, **over}


def test_enabled_map_is_a_pure_function_of_state():
    caps = {c["id"]: c for c in capabilities(ACTIONS, state())}
    assert caps["rotate"]["enabled"] is True
    assert caps["upgrade"]["enabled"] is True
    assert caps["kill"]["enabled"] is True
    assert caps["kill"]["destructive"] is True and caps["kill"]["tab"] == "chaos"


def test_upgrade_self_disables_when_already_at_target():
    # no used-flag: it disables because version == target, not because we clicked it
    caps = {c["id"]: c for c in capabilities(ACTIONS, state(version="16.6"))}
    assert caps["upgrade"]["enabled"] is False
    assert caps["rotate"]["enabled"] is True         # rotate stays multi-use


def test_interlock_disables_conflicting_action_in_flight():
    caps = {c["id"]: c for c in capabilities(ACTIONS, state(fault_in_flight=True))}
    assert caps["kill"]["enabled"] is False


def test_server_side_gate_matches_the_map():
    assert is_enabled(ACTIONS, "upgrade", state()) is True
    assert is_enabled(ACTIONS, "upgrade", state(upgrading=True)) is False
    assert is_enabled(ACTIONS, "does-not-exist", state()) is False
