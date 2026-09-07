# Sketch: running the drafting and summarising on something other than Claude

Not implemented. This is the shape it would take, written down while the
reasoning is fresh, so that whoever does it does not have to re-derive which
parts are load-bearing.

---

## Why this is even possible

Because the daemon already treats the model's output as hostile data.

`pipeline.parse_submission` rejects any answer containing `chat`, `recipient`,
`to`, `send`, `live` or `draft_id` — routing is the daemon's, and an attempt to
supply it is a hard stop rather than something to sanitise. `extract_answer`
does nothing but pull the outermost `{...}` out of the final message. The
recipient comes from the queue item the daemon wrote. Sending is a click the
daemon performs.

So the model is a text generator with no standing, and **swapping it cannot
weaken the security boundary**. That is a property worth not losing: everything
below keeps validation on the daemon's side of the line.

What swapping it *does* change is quality, cost, and who sees the messages.

---

## Where the seam goes

Both runs have the same shape today:

```
build the prompt        ← stays in drafter.py (voice, rules, house style)
run a model with tools  ← THE BACKEND
prove the tools were live
take the final message
extract_answer          ← stays
parse_submission        ← stays: this is the boundary
write_submission        ← stays
```

Only the middle two lines move. `run_drafter` and `run_summarizer` keep their
signatures and their return shape (`{queue_id, ok, ...}`), so `tick.py` and the
retry/stall logic are untouched.

---

## The protocol

```python
@dataclass(frozen=True)
class Run:
    ok: bool
    text: str = ""        # the run's FINAL message, nothing else
    error: str = ""       # why not, for the log
    health: str = ""      # what the tool handshake reported


class Backend(Protocol):
    name: str

    def run(self, prompt: str, *, tools: ToolSet,
            timeout_s: int, cwd: Path) -> Run: ...
```

`ToolSet` is one of exactly two values today — `NONE` for the summariser and
`WORKSPACE_READONLY` for the drafter. It is deliberately not a free-form list:
the set of capabilities this agent may ever hand a model is a decision that
belongs in the code, not in configuration a future caller can widen.

Selection is per-run, because the two jobs have different requirements:

```
WA_DRAFTER_BACKEND=claude-cli      # default
WA_SUMMARISER_BACKEND=claude-cli   # the one worth moving first
```

---

## What a backend must guarantee

Three rules. The first is the one that is easy to get wrong and expensive to
get wrong.

### 1. Prove the tools are live, or return `ok: False`

A drafting run that cannot reach mail and calendar does not produce an error.
It produces a confident, fluent, completely baseless answer, and exits 0.
`mcp_health` exists because of that: it reads the `init` event that
`--output-format stream-json --verbose` emits *before any tokens are spent*,
checks the server is `connected`, and checks it is actually offering
`get_events` and `search_gmail_messages` — a server that is up but not serving
those cannot answer the questions this agent exists for, and is treated exactly
like one that is down.

Any new backend needs its own version of that, and must fail closed. For an API
backend driving its own tool loop this is easier, not harder: call the tool
once, or verify the connection, before trusting anything the model says.

`ok: False` keeps the queue item for a retry. After `CONTEXT_STALL_ATTEMPTS`
the self-chat says so. None of that changes.

### 2. Grant capability positively

`DISALLOWED_TOOLS` exists only because the Claude CLI ships a large built-in
tool surface — `Bash`, `Read`, `Write`, `Edit`, `Agent` — that must be denied
explicitly, since an allowlist that merely omits a dangerous tool is not a
boundary. That was the disclosed privilege-escalation finding: `Write` reached
`src/wa_session/`, the package the daemon imports and executes.

An API backend starts with **no** tools and gains only what you implement. That
is strictly the better position, and a backend written that way should not
grow a deny-list — it should simply never define a tool that touches the
filesystem or WhatsApp.

### 3. Return the final message and nothing else

`extract_answer` tolerates a stray code fence or a sentence either side, but
the backend should not be pasting transcripts or tool output into `text`. One
final message; the daemon does the rest.

---

## The two jobs are not equally portable

### Summariser — portable almost for free

`summary_allowed_tools()` returns `[]`. Zero tools, prompt in, JSON out. A
local model via Ollama or llama.cpp needs an adapter of perhaps thirty lines.

This is the one worth doing first, and not for cost: **group chatter is written
by people who never agreed to any of this.** Running the digest locally is the
only change here that would genuinely alter what leaves the machine.

The risk is quality, in the exact place it has already caused harm. The digest
rules are demanding — Hebrew to English without mixing them mid-sentence, under
120 words, three bullets a chat, and *report, do not infer*. That last rule
exists because a digest once turned "Thursday and Friday are my days off" into
"no kindergarten those days", which was false and nearly kept a child home.
A smaller model is worse at precisely that discipline.

Gate it on evidence, not hope: `test_digest_language.py` and the inference
rules run against the candidate model, then a week of digests read by a human
before it becomes the default.

### Drafter — a real project

It needs Gmail and Calendar tool-calling, which today is MCP enforced by the
Claude CLI's permission system. On a raw API you would write the tool loop
yourself: two read-only tools, a turn limit, and the health probe above. Doable,
and arguably safer, but it is a build rather than an adapter.

It also has to hold a voice — one or two sentences, answer first, no
provenance in the body, and **no commitments**: no promises, no accepting
plans, no agreeing to anything, because a free calendar is not consent to fill
it. A model that drifts there produces drafts you reject, which costs the
scarce resource, which is your attention.

---

## One thing a local model does not buy

Gmail and Calendar are read through Google's APIs either way. Running the model
on your own machine changes which *model vendor* sees your message text; it
does not make your mail local. Any claim in `DISCLAIMER.md` should be adjusted
precisely and not generously:

- summariser moved locally → group messages no longer leave the machine, true
- drafter moved locally → the message text goes to a different vendor, or none;
  your mail and calendar still go to Google

---

## Sequencing

1. **Not while the project is unstable.** An abstraction laid over code that is
   still producing correctness bugs gives you bugs in the seam between them.
   Wait for a quiet week.
2. Extract `Backend` with `ClaudeCLIBackend` as the only implementation, and
   change nothing else. If the tests do not notice, the seam is in the right
   place.
3. Add a toolless backend and point the **summariser** at it, behind an env
   var, with Claude still the default.
4. Read digests for a week. Compare against the rules that already have tests.
5. Only then consider the drafter, and only with the tool loop and health probe
   written first.

---

## Open questions

- **Turn limits.** `--max-turns 16` for drafting, `10` for summarising. A local
  model with weaker tool-calling may loop; a backend needs its own ceiling and
  must treat hitting it as `ok: False`, not as a partial answer.
- **Timeouts.** 300s drafting, 420s summarising, enforced by killing the child.
  An in-process API loop needs its own hard stop, and the profile lock is
  released during this phase — a backend that hangs stalls the queue item, not
  the browser.
- **Prompt portability.** The prompts are written for one model and lean on it
  following long instructions. Expect to rewrite them per backend rather than
  share one string, and to keep the tests as the contract instead.
- **Cost accounting.** Nothing tracks spend today. If backends differ in price
  this becomes worth logging per run.
