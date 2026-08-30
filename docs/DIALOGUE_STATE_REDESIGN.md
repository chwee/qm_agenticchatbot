# Dialogue state redesign: architecture proposal and rollout

Companion doc to the full design (with the log evidence, tables and diagrams):
https://claude.ai/code/artifact/306b36f0-df0f-446d-88a0-f5a98722f6a6

This file is the git-trackable summary for engineers working in this repo.
Successor to `MEMORY_CONTEXT_REDESIGN.md`, which diagnosed context
contamination as the primary cause of enquiry→Module B misroutes but stopped
short of a structural fix. This doc reviews the actual routing/dialogue code
(`router.py`, `module_a/b/c.py`, `orchestrator.py`) and proposes the
architecture change, not just the input-cleanup change.

## Diagnosis

**Dialogue state is read back out of the text the model just wrote.**

`module_b.py:692` decides the enrolment state machine's transition with
`reply.strip().lower().endswith(_CONFIRM_MARKER.lower())`. Twelve places
across `crews/` instruct the LLM to reproduce a sentence verbatim so that
matching still works (e.g. `_CONFIRM_MARKER`, `_CANCEL_CONFIRM_MARKER`,
`_REMINDER_DATE_ASK_TEXT` in `module_a.py`). Reword the reply and the
transition silently fails to fire — the next turn then has nothing but raw
transcript text to re-derive intent from, which is where the misroutes
documented in `MEMORY_CONTEXT_REDESIGN.md` actually come from.

Measured over the current code (2026-08-28):

| | |
|---|---|
| `re.compile` across router.py/module_a.py/module_b.py/module_c.py/orchestrator.py | 37 |
| "observed live" / "found live" comments (one per production patch) | 36 |
| places instructing the LLM to end a reply with an exact sentence, verbatim | 12 |
| fields on `conversation_state` (`active_module`, `active_flow`) — everything else (course, intake, invoice) is re-parsed from prose every turn | 2 |
| `module_a.py` line count | 1124 |

## Root cause the previous doc didn't name

`classify_free_text()` asks one question on every turn: *ENQUIRY, ENROLLMENT,
or PAYMENT?* — a **topic** classification. But the messages that actually
misroute (`joelim@test.com`, `the second one`, `08-09 Sep`, `course 2`) have
no topic — they're **answers to a question the bot just asked**. Asking
"which of three topics is this email address?" is a category error: an email
address containing the letters "pay" genuinely does look more like PAYMENT
than ENQUIRY when those are the only three choices on offer.

Every deterministic override in `router.py` — `_reminder_in_progress()`,
`_CONFIRMATION_RE` + `_last_assistant_offered_enrollment()`, `_BARE_EMAIL_RE`
(which exists purely to undo `_PAYMENT_KEYWORD_RE` misfiring on an email
address) — exists to undo that category error after the fact. `_sticky_dispatch()`
(Tier 0) is the correct instinct, already partly built, but it still runs
Tier 1's topic classifier unchanged whenever it misses, and it can only
express "same module" vs. "different module," never "this answers my open
question."

**Fix:** ask two questions in order. Is a question open, and does this
message answer it (`ANSWER`/`REVISE`/`DIGRESS`/`ABANDON`)? Only if nothing is
open does topic classification (`START`) apply at all. The ambiguous messages
in the corpus always arrive while a question is open — never cold-open — so
this reframe removes the need for most existing overrides rather than adding
a ninth.

## Target contracts

Three types carry the design (full definitions and the reminder-flow
transition table are in the artifact):

1. **`DialogueState`** — `pending: PendingQuestion` (an enum, not prose) +
   `slots: Slots` (`course_code`, `schedule_code`, `invoice_no`,
   `candidates` — what was actually offered, so "the second one" resolves by
   indexing a tuple instead of regexing the transcript) + `session_id`.
   Extends `conversation_state` with `flow_context JSONB`, `session_id`,
   `pending_question`.
2. **`Interpretation`** — structured LLM output carrying an `Act`
   (`ANSWER`/`REVISE`/`DIGRESS`/`ABANDON`/`START`), not two lines of free text.
   `DIGRESS` is the act the current architecture cannot express at all — it's
   what lets someone ask an unrelated question mid-flow and return to the open
   question afterward, instead of being trapped (147 consecutive Module
   B/registration turns recorded in the logs, longest run 9).
3. **`TurnResult`** — modules return `state: DialogueState` explicitly;
   `orchestrator._dispatch()` writes it verbatim. Deletes
   `reply.endswith(marker)` / `regex.search(reply)` as a state-transition
   mechanism entirely.

Isolation: split `dialogue/` by **flow**, not by module —
`dialogue/flows/{reminder,enrolment,cancellation,payment,registration}.py`,
each with `state.py`/`interpret.py`/`policy.py` (pure, no I/O, no LLM —
unit-testable in milliseconds, unlike today's equivalent logic which can only
be exercised via a live OpenAI-backed conversation).

## Rollout (staged, with an exit gate at day 3)

| Phase | What | Cost | Gate |
|---|---|---|---|
| 0 | Trace instrumentation — no behaviour change | 0.5 d | — |
| 1 | Land `DialogueState` types, dual-write alongside existing logic | 1 d | asserted state agrees with marker-matched state ≥95% of replayed turns |
| 2 | Cut context contamination — participant-only router window, episode segmentation | 1 d | enquiry→B misroutes drop below 8%, or stop and re-diagnose |
| 3 | Two-question router (`Act` before topic) | 1 d | all 40 logged misroutes replay clean |
| 4 | Flip the write — orchestrator writes `result.state`; delete marker matching | 1 d | — |
| 5 | Extract flows one at a time, reminder first | 3–4 d | — |
| 6 | Delete overrides one at a time, re-running evals after each | ongoing | no regression |

Constraints: don't raise the model off `gpt-4o-mini` (hides the cause without
removing it); don't migrate frameworks yet (these contracts are
framework-agnostic — `policy.py` is already LangGraph's reducer shape if that
migration happens later); don't fix the next flow bug with a 38th regex — add
a row to the transition table instead.

## Status

### Phase 0 — done (2026-08-28)

Found while starting phase 0: **Tier 0 sticky dispatch's decision has always
been logged via `log.info()`, but the query trace file
(`backend/logs/query_log_*.txt`) is written separately by `query_log.emit()`.**
The two never shared a channel, so every trace file showed an identical
`free text → Module B` line whether the decision came from the sticky
dispatcher, the LLM classifier, or the no-API-key heuristic fallback — three
different paths, indistinguishable in the artefact this whole diagnosis was
built from. `MEMORY_CONTEXT_REDESIGN.md` explicitly grepped those logs for
"Tier 0", found zero occurrences, and concluded the path might not be running
at all. It runs — it was simply never written to the file being read.

Fixed:

- `crews/router.py` — added `RouteTrace` (observational dataclass; never
  consulted for control flow). `_sticky_dispatch()` and `classify_free_text()`
  now take an optional `trace` parameter and record which tier decided, the
  pending flow at entry, the raw classifier label/confidence before any
  override, and each override that fired with its before→after. `route()`
  returns `{"module", "arg", "has_media", "trace"}` — additive; no existing
  caller reads the new key, so nothing downstream changed behaviour.
- `orchestrator.py` — `_print_routing()` takes an optional `trace` and renders
  it into the query log under the existing routing line. Wired into all four
  call sites that log a routing decision, including the two registration-gate
  branches (`registration declined, not re-prompting` and
  `profile incomplete, prompting`) — these are the exact turns
  `MEMORY_CONTEXT_REDESIGN.md`'s 120/18 counts came from, and they previously
  never went through `_dispatch()` so the trace has to be threaded to them
  explicitly.

Every render call is wrapped in `try/except` so a trace-rendering bug can
never take down a real turn.

Verified: `py_compile` clean on both files; a standalone smoke test exercised
all `RouteTrace` fields across the media/sticky/disambiguation/llm/heuristic
tiers, an override chain, and `_print_routing()` called with a broken trace
object and with `trace=None` (the registration paths) — all render/degrade
correctly. Not yet exercised against a live conversation (would need
Docker + the backend running); the next real conversation logged will be the
first with tier-level visibility.

### Phase 1 — done (2026-08-28)

Scoped to module_a.py's reminder flow only (the flow with the most patches,
and where phase 0's own example bugs live) as a reference implementation;
module_b.py/module_c.py dual-write is deferred to a follow-up pass rather
than attempted in the same change — see "Deferred" below.

Landed:

- **`app/dialogue/state.py`** — `PendingQuestion` (a plain-string enum),
  `Slots` (`course_code`, `schedule_code`, `invoice_no`, `candidates`, with
  `to_json()`/`from_json()`), `DialogueState`. `PendingQuestion` gives the
  reminder flow's three sub-stages — email-ask, which-course-ask,
  which-intake-ask — their own values for the first time; today they all
  share the one `active_flow` value `"awaiting_reminder"` and are
  disambiguated only by regexing the assistant's last reply
  (`_INTAKE_ASK_CONTEXT_RE` / `_COURSE_ASK_CONTEXT_RE` in `module_a.py`).
- **`conversation_state.flow_context JSONB`** — added in `db/schema.sql`
  *and*, separately, in `database.py`'s `_SCHEMA_MIGRATIONS` list (see
  "Found while implementing" below for why both were needed).
  `repositories.set_conversation_state()` takes an optional `flow_context`
  dict (defaults to `{}`, i.e. every existing call site is unaffected);
  `clear_conversation_state_if_owner()` resets it to `{}` alongside
  `active_module`/`active_flow`.
- **`module_a.py`** dual-writes at all four `set_conversation_state()` call
  sites in the reminder flow (the pre-agent email-ask short-circuit, the
  course-ambiguity backstop, the intake-ask backstop, and the
  still-no-email branch), plus an `_log_dialogue_state()` helper that logs
  the asserted `(pending, slots)` pair — wrapped in `try/except` so a
  logging failure can never break a turn. Nothing reads `flow_context` back
  to decide anything yet; every existing reply/behaviour is unchanged.

**Found while implementing — a correction to phase 0's own claim.** Phase 0
said (following `CLAUDE.md`) that `db/schema.sql` "auto-applies on startup
even without Docker." Live-testing phase 1 against the actual running dev
database (`qm_postgres`, up 10 hours) showed this is only true for a
**brand-new** database — `database.py: init_db()` runs the full
`schema.sql` exactly once, gated on `to_regclass('public.courses') IS NULL`.
Every *existing* database — which is every database that matters once the
project is past its first boot — is instead migrated by a second, separately
maintained list, `database.py: _SCHEMA_MIGRATIONS`. The project's own
pattern already accounts for this (e.g. `reminders.sent_at` is added in both
places), but it means every column added to `conversation_state` needs the
same statement in two files, or it silently only ever applies to installs
that don't exist yet. Caught because phase 1 was verified against the live
DB rather than compile-checked alone — the first version of this change
would have shipped a dual-write that throws on every real turn (`INSERT`
into a column the running database doesn't have).

Verified live against `qm_postgres` (not just `py_compile`): schema check
confirms `conversation_state.flow_context` exists as `jsonb NOT NULL DEFAULT
'{}'::jsonb`; a `Slots` round-trips through a real `INSERT`/`SELECT`
unchanged; a call site that doesn't pass `flow_context` still writes `{}`
(old behaviour preserved); `clear_conversation_state_if_owner()` resets it;
the module-ownership guard still refuses to touch state (or its
`flow_context`) owned by a different module. Test customer/state rows
created for this were deleted afterward — confirmed absent from both tables.

### Phase 1 follow-up — module_b.py / module_c.py (done, 2026-08-28)

Extends phase 1's coverage to the two modules deferred above, completing
phase 1 before moving to phase 2.

- **`app/dialogue/trace.py`** (new) — `log_dialogue_state()`, factored out of
  `module_a.py`'s local copy once `module_b.py`/`module_c.py` needed the
  identical helper. `module_a.py` now imports it from `app.dialogue` too —
  the same dedup this whole redesign exists to do elsewhere, applied to its
  own first artifact rather than left as a third private copy waiting to
  happen.
- **`module_b.py`** dual-writes at all three `set_conversation_state()`
  sites in `_run_agent()`'s outcome check: `CONFIRM_ENROL`
  (`course_code`/`schedule_code`, extracted from the confirmation summary —
  required to name both codes in brackets per `build_specialist_agent()`'s
  STEP 3), `CONFIRM_CANCEL` (`course_code`/`invoice_no`, same idea for the
  cancellation summary), and `INTAKE_SELECTION` (`course_code` +
  `candidates` = every SH-code the numbered list actually offered, in
  order — the field that lets a later phase resolve "the second one" by
  indexing a tuple instead of re-parsing the list out of the transcript).
  All extracted with the same regexes (`_C_CODE_IN_TEXT`, `_SH_CODE_IN_TEXT`)
  the *existing* marker-matching already runs against this same reply text —
  the dual-write adds no new parsing risk, it just keeps the result as typed
  data instead of only prose.
- **`module_c.py`** — deliberately **not** given a real `conversation_state`
  write. Module C has no flow owner at all today (a documented gap, see
  Root Cause above); adding one would be an actual routing behaviour
  change — Tier 0 sticky dispatch would start handing subsequent turns back
  to Module C when it doesn't today — which is out of scope for a
  dual-write. Instead `_run_agent()`'s non-reroute path logs what *would* be
  asserted (`PendingQuestion.PAYMENT_PROOF`, with `invoice_no` extracted via
  the existing `_INVOICE_RE`) via `log_dialogue_state()` alone — no
  `set_conversation_state()` call exists anywhere in the file, verified by
  grep as part of this phase's own test.

Verified: full backend `py_compile` clean. Live-tested against `qm_postgres`
(not just regex unit checks) — `module_b`'s three slot shapes extracted
correctly from realistic reply text (a confirmation summary, a cancellation
summary, and a two-item intake list, including candidate order and
de-duplication) and round-tripped through a real `INSERT`/`SELECT`
unchanged; `module_c.py`'s source was searched at runtime for
`repo.set_conversation_state(` and confirmed to have zero occurrences; the
shared `log_dialogue_state()` helper was called through both `module_b.log`
and `module_c.log` without raising. The original phase-1 round-trip test was
re-run afterward as a regression check and still passes unchanged. Test rows
deleted afterward — confirmed absent.

**Still open** (unchanged by this pass, not phase-1 scope): registration
continuation isn't tracked via `conversation_state` at all — it matches the
literal text of the last message sent instead
(`orchestrator._is_registration_prompt()`). The phase-1 gate ("asserted
state agrees with marker-matched state ≥95% of replayed turns") can only be
scored once real traffic has logged `dialogue-state (phase 1, observational)`
lines across all three modules to compare against.

### Phase 2 — done (2026-08-28)

Scoped exactly as the rollout table above: participant-only router window +
episode segmentation. The original design artifact's phase 2 also mentioned
a "conditional call-to-action" (stop offering to enrol on every course
reply) as part of cutting contamination — deliberately **not** done here.
Cutting the router's own window to participant-only removes the classifier's
exposure to that bait regardless of whether module_a.py still sends it, so
the two are separable; the CTA change is a UX/frequency concern (not
re-annoying a participant with the same offer), not something this phase's
gate depends on. Left for a follow-up if the phase-2 gate isn't met once
scored against real traffic.

- **Episode segmentation** — `chat_memory.session_id TEXT` (new column, both
  `schema.sql` and `database.py: _SCHEMA_MIGRATIONS`, per phase 1's lesson)
  plus a matching index. `repositories.current_session_id(whatsapp_id)`
  reuses the id of the participant's last message (either role) if it was
  within 60 minutes, otherwise mints a fresh one; `add_memory()` stamps
  every row with it. `recent_memory()` gained an optional `session_id`
  filter — every existing caller (module_a/b/c, the two orchestrator.py
  call sites) omits it and is completely unaffected; only
  `router._recent_history()` passes it now.
- **Participant-only classifier window** — `router._recent_history()` still
  returns the full mixed-role history (now episode-scoped), unchanged for
  `_last_assistant_offered_enrollment()` and `_reminder_in_progress()`,
  which genuinely need assistant turns to check. Only
  `classify_free_text()`'s own LLM prompt was narrowed: `convo` is built
  from `participant_turns = [h["content"] for h in history if
  h.get("role") == "user"]`, the last 3, prefixed `"participant: "` — no
  assistant text reaches the classifier's prompt at all. The task
  description's "if the assistant's last message offered intakes..."
  instruction (now unsatisfiable — there's nothing to check it against) was
  replaced with an explicit instruction to report LOW confidence on an
  ambiguous bare reply rather than guess, since the existing deterministic
  downgrade (`_CONFIRMATION_RE` + `_last_assistant_offered_enrollment()`,
  reading the untouched full `history`) already resolves those correctly.

**Why this is safe, not just smaller**: the two changes are independent by
construction. `_recent_history()`'s return value is unchanged for every
consumer except the one line that builds `convo` — the deterministic
overrides that need assistant text still get it, from the same variable,
untouched. Episode segmentation only removes rows that are already stale by
any reasonable definition (60+ minutes idle); nothing that was reachable
before is newly unreachable within an active conversation.

Verified live against `qm_postgres`: `chat_memory.session_id` exists;
same-turn user/assistant writes share one session_id; a message 2 hours
stale does not extend the episode (a fresh id is minted); `recent_memory()`
with `session_id` correctly excludes the stale episode while the no-argument
call path (every existing caller) is unaffected;
`router._recent_history()` returns correctly-scoped mixed-role history.
`classify_free_text()` was exercised with `_base.kickoff_agent` monkeypatched
(so this cost zero API calls) against the exact conversational shape from
this doc's own "cleanest pair in the corpus" example — a course-list reply
followed by "Ya, how about course 2" — and the captured prompt contained
only `participant: Hello I want to check courses` /
`participant: Ya, how about course 2`; the assistant's course-list reply
text was confirmed absent from the prompt entirely. Phase 0 and phase 1's
own tests were re-run afterward as regressions and still pass unchanged.

**Measured for real — 2026-08-28, see phase 2.1 below.** The gate check
below required a second iteration once real replay evidence showed this
phase alone (assistant text fully stripped, nothing else) wasn't sufficient
for short, low-context messages.

### Phase 2.1 — real gate measurement + a second fix (2026-08-28)

The reply "the next real conversations logged are what scores it" above
undersold what was actually possible: the 5 historical log files already
constitute a real corpus that can be *replayed* through the current code
without waiting for new live traffic. Built `backend/evals/replay_router_misroutes.py`
to do exactly that, permanently, since ad-hoc scratch scripts don't survive
to the next phase.

**Reproducing "the 40" honestly.** `MEMORY_CONTEXT_REDESIGN.md`'s "40
enquiry-shaped messages...routed to Module B" was computed by a process not
preserved in this repo — attempts to exactly reproduce it landed anywhere
from 9 to 185 candidates depending on how loosely "enquiry-shaped" was
defined. Rather than chase an unreproducible number, `replay_router_misroutes.py`
uses a **code-consistent** definition instead: a message with no explicit
enrollment/payment signal of its own — using router.py's own
`_has_explicit_enrollment_signal()`/`_looks_like_payment_intent()`, not a
separately-invented heuristic — that isn't registration-field data, a bare
confirmation, a bare intake-position reply, an explicit decline, or a
first-person status query (router.py's own classifier backstory already
documents "what did I register for" as correctly ENROLLMENT) — yet still
routed to Module B historically. This lands on **9** well-validated cases
across 6 conversations, spanning all 5 log files. Smaller than "40," but
every one of the 9 is a genuine, inspectable misroute, not a guess.

**First replay (phase 2 as shipped, no other changes): 5/9 fixed (56%).**
Real chat history seeded from the actual logs, real GPT-4o-mini calls, no
mocking. The 4 failures shared a shape: a short "how about course N"
message with only 1-2 prior participant turns before it — including case 9,
literally Joe Lim's own conversation from earlier this session. Diagnosis:
phase 2 stripped *all* assistant text to kill contamination, but that also
discarded legitimate grounding — "Ya, how about course 2" in a complete
vacuum, with no signal that the assistant just listed a catalogue,
plausibly reads as enrollment intent even to a careful reader. This is
exactly what the original design artifact's Layer 2 sketch anticipated
(`assistant_last_did: listed_catalogue` — a machine-written line, not zero
signal) and phase 2's actual implementation had simplified away.

**Fix**: `router.py: _assistant_last_action(history)` — a fixed,
five-item-vocabulary label (`listed_courses` / `showed_intake_options` /
`asked_to_confirm_enrolment_or_cancellation` / `asked_which_intake_for_reminder`
/ `discussed_payment_or_invoice`, falling back to `replied` or `none`)
matched against the last assistant turn via a small set of precise
patterns, prepended to the classifier's window as one line
(`assistant_last_action: listed_courses`) ahead of the participant-only
turns. This is deliberately **not** phase 2 reversed: phase 2's
contamination problem was dense, unpredictable, dozens-of-tokens-long free
prose bleeding "enrol"/"payment"/"invoice"/"intake" into the classifier at
high, unbounded token mass; this is one label from a closed set, with no
course names, fees, codes, or dates in it at all — bounded, predictable,
low-risk by construction, and directly informed by the actual failure
pattern instead of guessed.

**Second replay (phase 2.1 applied): 9/9 fixed (100%)**, including case 9
(Joe Lim's conversation) now correctly resolving to ENQUIRY. Re-run twice
independently — 7-8 of 9 landed on a confident ENQUIRY and 1-2 landed on
the DISAMBIGUATE menu rather than a confident read (LLM classification
carries some run-to-run variance at temperature 0.2); zero landed on
Module B either time. All prior phases' tests (0, 1, 1-followup, 2, 3) were
re-run as regressions after this change and still pass unchanged. Full
backend compile clean.

**Honest scope of this result**: 9 cases is a real but small, single-shape
sample (all "bare course reference following a catalogue listing"). It is
not the same as the original "40," and phase 1's gate (state-agreement
≥95%) remains genuinely unmeasurable retroactively — the dual-write logging
didn't exist during the period these logs cover. `replay_router_misroutes.py`
is permanent and re-runnable, though, so as more misroute shapes are found
(or more history accumulates), re-running it costs a few cents and a
minute, not a rebuild.

### Phase 3 — done (2026-08-28)

Scoped more narrowly than "two-question router" originally suggested, once
the actual architecture was re-examined — worth recording why.

**Re-diagnosis before implementing.** Tier 0 sticky dispatch already sends a
turn straight to the pending module without any classification at all
whenever nothing overrides it — so the "does this answer the open question"
interpretation the design doc's `Act` enum was meant to formalize is, for
the sticky case, already happening deterministically (no LLM) via each
module's own directive functions. The actual gap was narrower: the ONE
override check Tier 0 has (`_explicit_module_signal()`) treated **any**
course/schedule code in the message as an unconditional signal to leave the
pending module — including the participant simply repeating back the exact
course already under discussion. A bare "C2601" mid-reminder for C2601 was
being forced out to Tier 1's topic classifier every time, even though
nothing about the turn had changed. That's the concrete case fixed here;
it's the router-level, low-risk slice of the `Act` idea (`ANSWER` vs a real
signal), not the full `Interpretation`/`DIGRESS` pipeline the original
design sketched — that fuller version needs real free-text digression
detection (e.g. "what's SkillsFuture eligibility" mid-reminder), which is
higher-risk to get right without misfiring on meta-questions like "which
course are you referring to" (fixed earlier this session a different way —
see module_a.py's repeat-detection), and is deferred rather than rushed.

Also **not** phase 3: the "147 consecutive B/registration runs" statistic
from the diagnosis is dominated by the registration-continuation flow
(`orchestrator.py`'s `_awaiting_registration_reply()`), which doesn't go
through `router.py`'s Tier 0/Tier 1 at all — a separate mechanism, and a
separate fix, tracked under "still open" below rather than folded in here.

- **`router.py`**: `_restates_pending_course(body, state)` — true only when
  every course/schedule code in `body` is already part of the pending
  question's `flow_context` (its `course_code`, `schedule_code`, or
  `candidates` — phase 1's first real read for a routing decision, not just
  observational logging). `_explicit_module_signal()` now takes the pending
  `state` and only counts a code as a "B" signal when it's NOT a pure
  restatement; an enrollment keyword ("enrol", "sign up"...) is untouched
  and always signals "B" regardless — restating a code and stating fresh
  intent are different things. Falls back to the old unconditional "any
  code is a signal" behaviour whenever no `flow_context` exists to compare
  against (a flow from before phase 1, or one still un-migrated) — this
  cannot regress a case phase 1 doesn't yet cover.

Verified live against `qm_postgres`, via `router._sticky_dispatch()`
end-to-end (not just the pure function in isolation): a pending Module A
reminder for C2601 now stays sticky when the participant repeats "C2601"
(previously forced out to Tier 1 every time); a genuinely different course
mentioned in the same state still escapes, unchanged; a flow with no
`flow_context` recorded keeps the old unconditional behaviour exactly;
`candidates` (not just `course_code`/`schedule_code`) are honoured the same
way, checked against both a listed and an unlisted SH-code. Enrollment
keywords and payment intent were confirmed unaffected. All prior phases'
tests (0, 1, 1-followup, 2) were re-run afterward as regressions — all still
pass unchanged. Full backend compile clean.

**Not yet measured**: the phase-3 gate ("all 40 logged misroutes replay
clean") — this narrow fix targets a real but different failure mode than
most of the 40 logged misroutes, which phase 2 already addressed. Scoring
against the actual 40 needs the eval scenarios from "Tests to add" (below)
built out, not yet done.

### Phase 4 — done (2026-08-28)

Flips the write: modules now return a `TurnResult` (reply + the pending
question/slots they decided) instead of each calling
`repo.set_conversation_state()`/`clear_conversation_state_if_owner()`
directly, at up to 7 different points depending on which branch a turn took
(module_a.py's reminder flow) or 5 (module_b.py's enrolment/cancellation
flow). `orchestrator.py: _dispatch()` is now the single, auditable place
`conversation_state` is actually written from, via a new
`_apply_turn_result()`.

**Deliberately not a behaviour change.** The DECISION logic — which marker
text or regex pattern means what — is byte-for-byte the same as before this
phase; only *who calls the DB write* moved. Every write site was
transformed mechanically: a `repo.set_conversation_state(...)` call became
capturing `(pending, slots)` into local variables instead, and the
function's `return reply` became `return TurnResult(reply=reply, ...,
pending=pending_result, slots=slots_result)`. `PendingQuestion` values that
map onto the SAME legacy `active_flow` string (module_a's `WHICH_COURSE`/
`WHICH_INTAKE`/`EMAIL_FOR_REMINDER` all write `"awaiting_reminder"`, exactly
as before) go through a new `ACTIVE_FLOW_FOR_PENDING` lookup in
`dialogue/state.py`, so Tier 0 sticky dispatch and each module's own
regex-based context-window detection keep reading the identical strings
they always have — migrating those readers onto `PendingQuestion` directly
is later work, not this phase.

- **`dialogue/state.py`** — added `TurnResult` (`reply`, `module`, `pending`,
  `slots`) and `ACTIVE_FLOW_FOR_PENDING` (the `PendingQuestion` →
  legacy-string lookup).
- **`module_a.py`**: `_run_agent()` and `run()` now return `TurnResult`. All
  7 of its write sites relocated; the fabrication-check in `run()` (which
  can replace the reply text if module A's own reply hallucinated an
  enrollment) carries `result.pending`/`result.slots` through unchanged
  either way — only the reply text is ever swapped there, matching old
  behaviour exactly (the state decision always finished inside
  `_run_agent()`, before that check ever ran).
- **`module_b.py`**: same transform, 5 write sites (3 inside `_run_agent()`,
  2 short-circuit clears in `run()` itself for the quick-enrol path and the
  no-API-key fallback).
- **`module_c.py`**: conforms to the same `TurnResult` contract but
  deliberately keeps `pending=NONE` on every path — unchanged from before
  this phase. Module C has never called `set_conversation_state()` at all
  (see "still open" below); giving it real ownership now would be an actual
  routing behaviour change (Tier 0 would start handing it subsequent
  turns), not a mechanical relocation, so it's left for a separate,
  explicit decision.
- **`orchestrator.py`**: `_dispatch()` calls each module's `run()`, then
  `_apply_turn_result()` before returning — `pending == NONE` clears
  (ownership-guarded, as before), anything else sets via
  `ACTIVE_FLOW_FOR_PENDING`. Wired into both the normal dispatch path and
  the `RerouteRequested` (Module C → A/B) redirect path.

**Known, accepted side effect**: `backend/evals/debug_module_b.py` (an
ad-hoc, pre-existing debug script, not part of any documented or maintained
workflow) calls `module_b._run_agent()` directly and treats the result as a
string for printing — it will now print a `TurnResult` repr instead. Not
fixed, since updating every one-off debug script for a refactor of this
shape is unbounded scope for near-zero value; noted rather than silently
left for someone to trip over.

Verified live against `qm_postgres`:

- `_apply_turn_result()` tested directly for every `PendingQuestion` value
  either module uses (`WHICH_INTAKE`, `CONFIRM_ENROL`, `CONFIRM_CANCEL`,
  `INTAKE_SELECTION`, and the `NONE` → clear case for both modules and for
  Module C) — each produces the exact `active_module`/`active_flow`/
  `flow_context` row the old direct-write code would have.
  Cross-module ownership guard re-confirmed: a Module B result clearing
  state does not touch a flow owned by Module A.
- Two full end-to-end runs through `handle_inbound()` (real LLM calls, no
  mocking): a plain enquiry exchange (module A, no pending flow — state
  correctly stays clear) and a full reminder sequence — course question →
  decline → agree-with-email — which lands `conversation_state` in
  `module=A, active_flow=awaiting_reminder` with `flow_context.course_code`
  populated, proving the `TurnResult` plumbing carries real data from a
  module's decision all the way to the DB through the new single write
  point, not just in isolated unit checks.
- Observed and *not* chased down as part of this phase: that reminder run
  resolved to `PendingQuestion.EMAIL_FOR_REMINDER` rather than
  `WHICH_INTAKE` even though the course has two intakes and the email had
  just been given — a pre-existing property of the unchanged decision logic
  (a timing question about when the agent's own `Save Lead Data` tool call
  becomes visible to the post-agent DB read), not something this phase's
  write-relocation could have introduced, since the exact same branch
  structure decided it before and after. Worth a closer look in a future
  session, separate from this refactor.
- All prior phases' tests (0, 1, 1-followup, 2, 3) re-run as regressions —
  all still pass, unchanged. Full backend compile clean.

### Phases 5–6 — not started
