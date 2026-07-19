"""Server smoke tests — the SPA and the Console wiring. No real cluster."""
from unittest.mock import MagicMock, patch

from k8ostester_pg import server


def test_spa_is_a_self_contained_page_with_the_hooks():
    html = server.SPA
    assert html.strip().startswith("<!doctype")
    assert "/api/stream" in html and "/api/action" in html   # the two endpoints
    assert 'data-tab="operate"' in html and 'data-tab="builder"' in html
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
def test_console_deploy_applies_each_manifest_doc(_cc, _ctx):
    c = server.Console(namespace="ns", cluster="pg", start=False)
    c._sel = {"context": None, "namespace": "lab", "name": "pg"}
    res = MagicMock()
    res.namespaced = True
    with patch("kubernetes.dynamic.DynamicClient") as DC:
        DC.return_value.resources.get.return_value = res
        out = c.deploy({"name": "demo", "backups": False, "pooler": False, "schedule": False})
    assert out["namespace"] == "lab" and not out["failed"]
    assert any("Cluster/demo" in x for x in out["created"])
    # applied into the target namespace via the dynamic client
    assert res.create.call_args.kwargs["namespace"] == "lab"


@patch("k8ostester_pg.server.config.list_kube_config_contexts", return_value=([], None))
@patch("k8ostester_pg.server.ClusterClient")
def test_console_is_unselected_until_a_cluster_is_chosen(_cc, _ctx):
    c = server.Console(start=False)                 # no launch target
    assert c.state() == {"unselected": True}
    assert c.contexts_info()["selected"] is None
