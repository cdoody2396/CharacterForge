import json

from app.audit import AuditLog


def read_events(audit: AuditLog) -> list[dict]:
    path = audit.path_for_today()
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_log_writes_jsonl(audit):
    audit.log("filter_block", layer=1, category="minors", context="prompt")
    audit.log("app_start", version="0.1.0")
    events = read_events(audit)
    assert len(events) == 2
    assert events[0]["kind"] == "filter_block"
    assert events[0]["category"] == "minors"
    assert events[1]["kind"] == "app_start"
    assert all("ts" in e for e in events)


def test_disabled_writes_nothing(tmp_path):
    audit = AuditLog(tmp_path / "logs", enabled=False)
    audit.log("app_start")
    assert read_events(audit) == []


def test_toggle_at_runtime(audit):
    audit.log("one")
    audit.enabled = False
    audit.log("two")
    audit.enabled = True
    audit.log("three")
    kinds = [e["kind"] for e in read_events(audit)]
    assert kinds == ["one", "three"]


def test_log_survives_unwritable_dir(tmp_path):
    # Point at a path that cannot be a directory (a file stands in the way).
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    audit = AuditLog(blocker / "logs", enabled=True)
    audit.log("event")  # must not raise
