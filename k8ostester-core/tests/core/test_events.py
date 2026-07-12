from k8ostester.core.events import EventLog


def test_event_log(tmp_path):
    log_path = tmp_path / "events.jsonl"
    events = []
    def on_event(e): events.append(e)
    
    logger = EventLog(log_path, on_event=on_event)
    logger.emit("test.type", "test message", key="value")
    logger.close()
    
    read_events = EventLog.read(log_path)
    assert len(read_events) == 1
    assert read_events[0]["type"] == "test.type"
    assert read_events[0]["data"]["key"] == "value"
    assert len(events) == 1

def test_event_log_survives_broken_callback(tmp_path):
    """A display callback that raises (e.g. a TUI that already shut down)
    must never break emit — teardown depends on it (regression: 'App is not
    running' aborted a run's teardown and leaked the namespace)."""
    def broken(_event):
        raise RuntimeError("App is not running")

    logger = EventLog(tmp_path / "events.jsonl", on_event=broken)
    event = logger.emit("teardown.start", "deleting namespace")  # must not raise
    logger.close()

    assert event["type"] == "teardown.start"
    assert EventLog.read(tmp_path / "events.jsonl")[0]["type"] == "teardown.start"
