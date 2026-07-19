"""Server smoke tests — the SPA and the Console wiring. No real cluster."""
from unittest.mock import patch

from k8ostester_pg import server


def test_spa_is_a_self_contained_page_with_the_hooks():
    html = server.SPA
    assert html.strip().startswith("<!doctype")
    assert "/api/stream" in html and "/api/action" in html   # the two endpoints
    assert 'data-tab="ops"' in html and 'data-tab="chaos"' in html
    assert "EventSource" in html


@patch("k8ostester_pg.server.config.list_kube_config_contexts", return_value=([], None))
@patch("k8ostester_pg.server.ClusterClient")
@patch("k8ostester_pg.server.execute.execute", return_value="killed primary pg-1")
@patch("k8ostester_pg.server.discover.snapshot", return_value={"primary": "pg-1"})
def test_console_act_discovers_then_executes(snap, ex, _cc, _ctx):
    c = server.Console(context="ctx", namespace="ns", cluster="orders", start=False)
    msg = c.act("kill-pod")
    assert msg == "killed primary pg-1"
    snap.assert_called_once()                       # fresh discovery before acting
    ex.assert_called_once()
    assert ex.call_args.args[2] == "kill-pod"   # dispatched the right action
    assert ex.call_args.kwargs["name"] == "orders"  # against the selected cluster


@patch("k8ostester_pg.server.config.list_kube_config_contexts", return_value=([], None))
@patch("k8ostester_pg.server.ClusterClient")
@patch("k8ostester_pg.server.discover.snapshot", return_value={"primary": "pg-1"})
def test_console_state_serves_the_cached_snapshot(snap, _cc, _ctx):
    c = server.Console(context="ctx", namespace="ns", cluster="orders", start=False)
    c.refresh()                                     # the timer would do this
    st = c.state()
    assert st["snapshot"]["primary"] == "pg-1"
    assert any(cap["id"] == "kill-pod" for cap in st["capabilities"])
    snap.assert_called_once()                       # discovery runs once, not per read
    c.state()
    c.state()
    snap.assert_called_once()                       # extra reads don't re-discover


@patch("k8ostester_pg.server.config.list_kube_config_contexts", return_value=([], None))
@patch("k8ostester_pg.server.ClusterClient")
def test_console_is_unselected_until_a_cluster_is_chosen(_cc, _ctx):
    c = server.Console(start=False)                 # no launch target
    assert c.state() == {"unselected": True}
    assert c.contexts_info()["selected"] is None
