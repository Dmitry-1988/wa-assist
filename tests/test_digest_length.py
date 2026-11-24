"""A digest over the limit gets one shortening rerun, and is never lost.

The prompt said "aim for under 120 words" and five groups produced 186 on
2026-11-22: the model reads a target as a suggestion. The daemon counts.
"""

import json

from wa_session import drafter
from wa_session.pipeline import QueueItem


def _reply(words: int) -> str:
    body = " ".join(["word"] * words)
    return json.dumps({"result": json.dumps(
        {"queue_id": "sum-1", "body": body, "sources": []})})


def _wire(monkeypatch, *lengths):
    prompts = []
    replies = iter(lengths)

    def fake_run(cmd, **kwargs):
        prompts.append(cmd[2])
        n = next(replies)
        if n is None:
            return drafter.subprocess.CompletedProcess(cmd, 1, "", "boom")
        return drafter.subprocess.CompletedProcess(cmd, 0, _reply(n), "")

    monkeypatch.setattr(drafter, "claude_binary", lambda: "claude")
    monkeypatch.setattr(drafter.subprocess, "run", fake_run)
    written = {}
    monkeypatch.setattr(drafter, "write_submission",
                        lambda config, qid, sub: written.setdefault("body", sub.body))
    return prompts, written


ITEM = QueueItem(queue_id="sum-1", chat="__summary__")


def test_a_short_digest_runs_once(config, monkeypatch):
    prompts, written = _wire(monkeypatch, 90)
    out = drafter.run_summarizer(config, ITEM)
    assert len(prompts) == 1 and out["digest_words"] == 90
    assert len(written["body"].split()) == 90


def test_a_long_digest_is_rewritten_shorter(config, monkeypatch):
    prompts, written = _wire(monkeypatch, 186, 110)
    out = drafter.run_summarizer(config, ITEM)
    assert len(prompts) == 2
    assert "186 WORDS" in prompts[1]
    assert out["shortened_from"] == 186 and out["digest_words"] == 110
    assert len(written["body"].split()) == 110


def test_a_failed_rewrite_still_posts_the_long_digest(config, monkeypatch):
    prompts, written = _wire(monkeypatch, 186, None)
    out = drafter.run_summarizer(config, ITEM)
    assert out["ok"] is True and "shorten_failed" in out
    assert len(written["body"].split()) == 186


def test_the_prompt_ceiling_matches_the_enforced_one():
    prompt = drafter.build_summary_prompt([], "sum-1")
    assert f"never exceed\n{drafter.DIGEST_WORD_LIMIT}" in prompt
