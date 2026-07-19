"""Server smoke tests — the SPA and the Console wiring. No real cluster."""
from unittest.mock import patch

from k8ostester_pg import server


def test_spa_is_a_self_contained_page_with_the_hooks():
    html = server.SPA
    assert html.strip().startswith("<!doctype")
    assert "/api/stream" in html and "/api/action" in html   # the two endpoints
    assert 'data-tab="ops"' in html and 'data-tab="chaos"' in html
    assert "EventSource" in html


@patch("k8ostester_pg.server.ClusterClient")
@patch("k8ostester_pg.server.execute.execute", return_value="killed primary pg-1")
@patch("k8ostester_pg.server.discover.snapshot", return_value={"primary": "pg-1"})
def test_console_act_discovers_then_executes(snap, ex, _cc):
    c = server.Console("ctx", "ns", "")
    msg = c.act("kill-primary")
    assert msg == "killed primary pg-1"
    snap.assert_called_once()                       # fresh discovery before acting
    ex.assert_called_once()
    assert ex.call_args.args[2] == "kill-primary"   # dispatched the right action


@patch("k8ostester_pg.server.ClusterClient")
@patch("k8ostester_pg.server.discover.snapshot", return_value={"primary": "pg-1"})
def test_console_state_bundles_snapshot_and_capabilities(snap, _cc):
    c = server.Console("ctx", "ns", "")
    st = c.state()
    assert st["snapshot"] == {"primary": "pg-1"}
    assert any(cap["id"] == "kill-primary" for cap in st["capabilities"])
