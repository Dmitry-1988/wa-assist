"""No internet: skip the browser, keep counting, keep alerting.

The long outages (1249 ticks on 2026-11-18 to 11-20, 156 on 11-24) were this
Mac without internet. Each tick launched Chromium and waited 30s for a
timeout. Now a plain HTTPS request decides first.
"""

import urllib.error

import wa_session.tick as tick
from wa_session.tick import _offline_reason as real_offline_reason


class Answer:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_any_http_answer_counts_as_online(monkeypatch):
    monkeypatch.setattr(tick.urllib.request, "urlopen",
                        lambda req, timeout: Answer(302))
    assert real_offline_reason() == ""


def test_an_http_error_still_means_the_network_works(monkeypatch):
    def forbidden(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "no", {}, None)
    monkeypatch.setattr(tick.urllib.request, "urlopen", forbidden)
    assert real_offline_reason() == ""


def test_no_route_is_offline(monkeypatch):
    def down(req, timeout):
        raise urllib.error.URLError("nodename nor servname provided")
    monkeypatch.setattr(tick.urllib.request, "urlopen", down)
    assert "URLError" in real_offline_reason()


def test_an_offline_tick_never_starts_the_browser(config, monkeypatch):
    monkeypatch.setattr(tick, "_offline_reason", lambda: "URLError: no route")

    def browser(*a, **k):
        raise AssertionError("must not launch Chromium while offline")
    monkeypatch.setattr(tick, "_browser_phase", browser)
    out = tick.run_tick(config)
    assert out["offline"] == "URLError: no route"
    assert "error" not in out
    assert tick._load_tick_errors(config)["count"] == 1


def test_half_an_hour_offline_still_raises_the_alarm(config, monkeypatch):
    monkeypatch.setattr(tick, "_offline_reason", lambda: "URLError: no route")
    notices = []
    monkeypatch.setattr(tick, "notify_desktop",
                        lambda *a, **k: notices.append(a[3]) or True)
    for _ in range(tick.TICK_ERROR_NOTIFY_AFTER):
        tick.run_tick(config)
    assert len(notices) == 1
    assert "unreachable from this Mac" in notices[0]
    assert "offline" in notices[0]
