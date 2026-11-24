"""A daemon that has stopped working must say so.

2026-11-18 to 11-20: 1249 ticks in a row died on `Page.goto` timeouts. Two
days, nothing read, drafted or sent, and nothing told the user. The page is
what fails, so the alarm goes to the desktop; the self-chat gets the account
once the page loads again.
"""

from wa_session import tick


def _fail(config, n, notices):
    for i in range(n):
        result = {"at": f"2026-11-24T05:{i:02d}:00+00:00", "actions": [],
                  "error": "TimeoutError: Page.goto: Timeout 30000ms exceeded.\nCall log:"}
        tick._record_tick_error(config, result)
    return result


def test_a_short_burst_stays_quiet(config, monkeypatch):
    notices = []
    monkeypatch.setattr(tick, "notify_desktop",
                        lambda *a, **k: notices.append(a) or True)
    _fail(config, tick.TICK_ERROR_NOTIFY_AFTER - 1, notices)
    assert notices == []


def test_half_an_hour_of_failure_reaches_the_desktop(config, monkeypatch):
    notices = []
    monkeypatch.setattr(tick, "notify_desktop",
                        lambda *a, **k: notices.append(a) or True)
    result = _fail(config, tick.TICK_ERROR_NOTIFY_AFTER, notices)
    assert len(notices) == 1
    message = notices[0][3]
    assert f"{tick.TICK_ERROR_NOTIFY_AFTER} ticks in a row" in message
    assert "Page.goto" in message and "Call log" not in message
    assert {"desktop_notice": "tick_errors",
            "n_errors": tick.TICK_ERROR_NOTIFY_AFTER} in result["actions"]


def test_recovery_is_announced_once_and_resets(config, monkeypatch):
    monkeypatch.setattr(tick, "notify_desktop", lambda *a, **k: True)
    _fail(config, tick.TICK_ERROR_NOTIFY_AFTER, [])
    notes = []
    monkeypatch.setattr(tick, "post_note",
                        lambda page, text, **kw: notes.append(text) or "n1")
    result = {"actions": []}
    tick._announce_recovery(object(), config, result)
    assert len(notes) == 1 and "working again" in notes[0]
    assert {"recovered_after": tick.TICK_ERROR_NOTIFY_AFTER} in result["actions"]
    tick._announce_recovery(object(), config, {"actions": []})
    assert len(notes) == 1                       # once
    assert tick._load_tick_errors(config) == {}


def test_a_burst_that_never_alerted_resets_silently(config, monkeypatch):
    monkeypatch.setattr(tick, "notify_desktop", lambda *a, **k: True)
    _fail(config, 3, [])
    notes = []
    monkeypatch.setattr(tick, "post_note",
                        lambda page, text, **kw: notes.append(text) or "n1")
    tick._announce_recovery(object(), config, {"actions": []})
    assert notes == [] and tick._load_tick_errors(config) == {}


def test_a_recovery_note_that_fails_is_retried(config, monkeypatch):
    monkeypatch.setattr(tick, "notify_desktop", lambda *a, **k: True)
    _fail(config, tick.TICK_ERROR_NOTIFY_AFTER, [])

    def broken(*a, **k):
        raise RuntimeError("never acknowledged")
    monkeypatch.setattr(tick, "post_note", broken)
    tick._announce_recovery(object(), config, {"actions": []})
    assert tick._load_tick_errors(config).get("notified") is True


def test_the_alarm_never_breaks_a_tick(config, monkeypatch):
    def broken(*a, **k):
        raise OSError("no notification centre")
    monkeypatch.setattr(tick, "notify_desktop", broken)
    _fail(config, tick.TICK_ERROR_NOTIFY_AFTER + 1, [])     # must not raise


def test_a_failing_tick_is_counted(config, monkeypatch):
    """Wired into run_tick, not just defined."""
    import wa_session.tick as t

    def boom(config, result):
        raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")
    monkeypatch.setattr(t, "_browser_phase", boom)
    out = t.run_tick(config)
    assert "TimeoutError" in out["error"]
    assert t._load_tick_errors(config)["count"] == 1
