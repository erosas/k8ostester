"""The CNPG control actions — the preconditions that make single-use vs multi-use
fall out of discovered state. Pure, no cluster."""
from k8ostester_kernel.control import capabilities
from k8ostester_pg.control import CNPG_ACTIONS


def snap(**over):
    base = dict(ready=True, primary="pg-1", replicas=["pg-2", "pg-3"],
                zones=["a", "b", "c"], version="16.4", target="16.6",
                upgrading=False, backup_configured=True, backups_completed=1,
                pitr_window=True, blue_green=True, fault_in_flight=False)
    return {**base, **over}


def caps(state):
    return {c["id"]: c["enabled"] for c in capabilities(CNPG_ACTIONS, state)}


def test_healthy_cluster_enables_the_expected_actions():
    c = caps(snap())
    assert c["backup"] and c["restore"] and c["rotate"] and c["upgrade"]
    assert c["kill-pod"] and c["partition-pod"]   # generic per-pod faults


def test_upgrade_available_when_healthy_target_chosen_at_press_time():
    # no --target gate anymore — the modal picks the image; only rolling/busy block it
    assert caps(snap())["upgrade"] is True
    assert caps(snap(target=""))["upgrade"] is True             # no flag needed
    assert caps(snap(upgrading=True))["upgrade"] is False       # not while rolling
    assert caps(snap(busy=True))["upgrade"] is False


def test_rotate_and_backup_stay_multi_use():
    for _ in range(3):
        assert caps(snap())["rotate"] is True
        assert caps(snap())["backup"] is True


def test_restore_needs_a_backup_and_window():
    assert caps(snap(backups_completed=0))["restore"] is False
    assert caps(snap(pitr_window=False))["restore"] is False


def test_faults_gated_by_the_interlock():
    assert caps(snap(fault_in_flight=True))["kill-pod"] is False        # interlock
    assert caps(snap(fault_in_flight=True))["partition-pod"] is False
    assert caps(snap(primary="", replicas=[]))["kill-pod"] is False     # no target


def test_rotate_needs_blue_green_roles():
    assert caps(snap(blue_green=False))["rotate"] is False
