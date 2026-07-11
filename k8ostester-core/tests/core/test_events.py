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
