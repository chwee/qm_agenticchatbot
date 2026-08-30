# Router context contamination: diagnosis and memory redesign

Companion doc to the full design (with the log evidence and diagrams): see the shared
artifact link in chat. This file is the git-trackable summary for engineers working in
this repo. Successor to `ROUTER_REDESIGN.md` — that doc's Tier 0 / Tier 1 design is now
partly built; this one explains why misclassification persists anyway.

## Symptom

After many turns in one WhatsApp conversation, the Intent Router starts sending
enquiry-shaped messages to Module B (enrollment), tripping the registration gate on
participants who only asked a question.

## What the traces show

Parsed from `backend/logs/query_log_2026-08-{22,23,24,25,28}.txt` — 15,590 lines,
1,177 turns, 165 distinct `whatsapp_id`s, longest thread 281 turns.

- Module A 783 / Module B 313 / Module C 42 / in-flight registration 30.
- **40 enquiry-shaped messages (question words, no enrol/register verb) routed to Module B.**
- 120 `profile incomplete, prompting`; 18 `registration declined, not re-prompting`.
- 5 `Module C rerouted -> Module B`, all on turn 1, all after a wasted `My Enrollments()` call.
- 4 disambiguation menus in 1,177 turns — all on cold-open gibberish, never mid-flow.
- Zero occurrences of `Tier 0` / `sticky dispatch` anywhere in the logs: that path is
  uninstrumented, so we cannot tell whether it runs.

**The misroutes do not cluster late in long conversations.** Inside the 281-turn thread the
Module B share per 40-turn block is flat (12, 13, 12, 9, 10, 8, 18). Length is not the driver.

**What does predict a misroute is the preceding assistant reply:**

| preceding assistant reply | enquiry-shaped msg -> Module B |
|---|---|
| contained enrol-bait ("which intake would you like", "ready to enroll?") | 31/160 = **19%** |
| no enrol-bait | 8/103 = **8%** |

2.4x lift. 78% of all enquiry->B misroutes follow a bait-carrying reply, and 12 of the 40
follow a turn the router had *just* correctly classified as Module A.

Cleanest pair in the corpus (`153811586920512@lid`, 2026-08-28, one turn apart):

```
"Ya, how about course 2"                     -> Module B (registration prompt)
"no. I just want to know about the course 2" -> Module A (correct)
```

Same intent. The second message only differs by carrying enough of its own signal to
outweigh the bait sitting above it in the window.

Three other confirmed failure shapes:

- `"How about the other one you mentioned"` -> Module B -> *"'MENTIONED' doesn't look like a
  valid NRIC"*. Misroute, then the registration gate parses the participant's words as profile data.
- Bot asks for NRIC; participant sends `SI785000W` (letter I for digit 1) -> classified as a
  **decline**. Their protest *"I had provide the NRIC"* -> classified as a second decline; the
  flow stops re-prompting. Four separate participants hit this.
- `"I want to cancel my enrollment"` -> *"No worries - no rush! Feel free to keep asking about
  our courses and fees..."* — cancellation collapses into decline.

## Root causes

### 1. The classifier's evidence is mostly the classifier's own output — PRIMARY

`orchestrator._persist_memory()` (L710-712) writes both the user text and the **full assistant
reply** to `chat_memory`. `router._recent_history()` (L126-136) reads back 6 turns: roughly
3 short user messages and 3 long tool-derived assistant replies. By token mass the router's
window is overwhelmingly the bot talking to itself, and that text is dense with *enrol*,
*payment*, *invoice*, *intake* — the exact tokens the 3-label prompt keys on.

This is the distractor effect from Chroma's context-rot research: topically-related but
irrelevant content degrades performance far more than unrelated filler, at any length.

### 2. Dialogue state is re-derived from prose every turn — PRIMARY

`conversation_state` (schema.sql L124-130) stores only `active_module` and `active_flow`.
The `flow_context JSONB` slot bag `ROUTER_REDESIGN.md` specified was **never built**, so which
course / which intake / which invoice still live only as English in the transcript. Every
module re-parses them each turn: `_recent_courses_text()`, `_resolve_course_choice()`,
`_course_switch_note()`, `_stale_context_directive()`. A non-deterministic re-derivation of
state the system already knew is why identical intent resolves differently one turn apart.

### 3. Flow transitions are decided by regexing the agent's own free text — HIGH

- `module_b.py` L690-710: `reply.strip().lower().endswith(_CONFIRM_MARKER.lower())`,
  `_INTAKE_LIST_RE.search(reply)`.
- `module_c.py` L282: the reroute is parsed out of model output.
- `module_c.py` never calls `set_conversation_state` at all — "awaiting payment proof" has no owner.

The state machine's transitions depend on the model reproducing an exact sentence. Reword the
reply and the state silently fails to advance.

### 4. Sticky dispatch has no exit for an enquiry — HIGH

`_explicit_module_signal()` (router.py L88-98) can only return `"B"` or `"C"`. There is no
explicit *enquiry* signal, so a plain question can never break a latched Module B flow; the
only escape is the 30-minute `expires_at`. The logs hold 147 consecutive B/REG runs, 25 of
length >= 4, longest 9. Failure shapes 2, 3 and 4 above all happened inside such a run.

### 5. No episode boundary; self-reported confidence never fires — MEDIUM

`chat_memory` (schema.sql L238-245) is one unbounded append-only list per `whatsapp_id` with
no `session_id`. `recent_memory()`'s `LIMIT n ORDER BY created_at DESC` silently spans days,
so today's message can be routed on Saturday's reply. Separately, `gpt-4o-mini` at
`temperature=0.2` self-reports `CONFIDENCE: LOW` only 4 times in 1,177 turns — it is not
calibrated, and never fires on the ambiguous mid-flow turns where the menu would help.

### Why the overrides can't converge

`_CONFIRMATION_RE`, `_BARE_EMAIL_RE`, `_reminder_in_progress()`,
`_last_assistant_offered_enrollment()` — each added after a live misroute, each inspecting the
same raw transcript, each needing not to break the previous four. router.py's own comments
document ~8 rounds of this. A ninth won't converge while the input stays contaminated.

## Design: three memory layers

### Layer 1 — Dialogue state (authoritative, deterministic, never LLM-written)

Extend `conversation_state`:

```sql
ALTER TABLE conversation_state ADD COLUMN IF NOT EXISTS flow_context   JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE conversation_state ADD COLUMN IF NOT EXISTS session_id     TEXT;
ALTER TABLE conversation_state ADD COLUMN IF NOT EXISTS turn_count     INT NOT NULL DEFAULT 0;
ALTER TABLE conversation_state ADD COLUMN IF NOT EXISTS sticky_turns   INT NOT NULL DEFAULT 0;
ALTER TABLE chat_memory        ADD COLUMN IF NOT EXISTS session_id     TEXT;
CREATE INDEX IF NOT EXISTS idx_chat_memory_session ON chat_memory(whatsapp_id, session_id, created_at);
```

`flow_context` holds slots — `course_code`, `schedule_code`, `invoice_no`,
`pending_question` (an enum, never prose). Modules **assert** state by returning it; the
orchestrator writes it. Nothing regexes generated text to decide a transition.

### Layer 2 — Working context (what the model actually sees, rendered per turn, never stored)

Stop pasting the transcript. Render a compact state block plus **participant turns only**.
The assistant's contribution is reduced to one machine-written line naming the open question:

```
registered          no
open_question       none
course_in_focus     none
enrollment          none
assistant_last_did  listed_catalogue

participant: Hi what courses u provide
participant: what course 2
```

### Layer 3 — Long-term facts (retrieved, not scrolled)

Anything older than the current episode is looked up, not replayed. `customers`,
`enrollments` and `payments` are already the authoritative long-term memory; the transcript
is a lossy duplicate. Fetch just-in-time through `repositories.py`.

## The two code changes that carry most of the fix

`backend/app/crews/router.py`:

```python
# before -- the assistant's paragraphs become classifier evidence
# history = repo.recent_memory(wid, limit=6)
# convo   = "\n".join(f"{h['role']}: {h['content']}" for h in history)

# after -- participant turns only, current episode only, plus asserted state
state = repo.get_conversation_state(wid) or {}
turns = repo.recent_memory(wid, limit=3, roles=("user",),
                           session_id=state.get("session_id"))
convo = "\n".join(f"participant: {t['content']}" for t in turns)
ctx   = render_state_block(state, context.customer())   # ~12 lines of f-string
```

`backend/app/crews/_base.py`:

```python
@dataclass
class ModuleResult:
    reply:     str
    next_flow: str | None    # 'awaiting_intake_selection' | None
    slots:     dict          # {'course_code': 'C2601', 'schedule_code': 'SH2604'}
    reroute:   str | None    # 'A' | 'B' | 'C'
```

`orchestrator._dispatch()` writes `conversation_state` from this — never from
`reply.endswith(_CONFIRM_MARKER)` or `_INTAKE_LIST_RE.search(reply)`.

## Change list, ranked by leverage

| change | touches | effort | removes |
|---|---|---|---|
| Participant-only router window | router.py, repositories.py | 0.5 d | cause 1 |
| Episode segmentation (`session_id`, 60-min idle) | schema.sql, repositories.py | 0.5 d | cause 5a |
| Conditional call-to-action (offer to enrol once per episode) | module_a.py | 0.5 d | the bait itself |
| `flow_context` slot bag | schema.sql, module_a/b/c | 2 d | cause 2 |
| `ModuleResult` return contract | _base.py, orchestrator.py, module_a/b/c | 2 d | cause 3 |
| Sticky-flow exit + never read NRIC/email-shaped msg as decline | router.py, orchestrator.py | 1 d | cause 4 |
| Structured classifier output (JSON schema response format) | router.py, llm.py | 0.5 d | silent A fallback |
| Pre-emptive Module C guard (check `latest_enrollment()` first) | module_c.py | 0.5 d | 5 cold reroutes |
| Structured routing trace per turn | query_log.py, router.py | 0.5 d | blind debugging |

## Framework survey

The popular "agent memory" tools solve a different failure than the one measured here.

| option | what it gives | fits? |
|---|---|---|
| **Keep CrewAI, add explicit state** (this doc) | the state machine we're hand-rolling, made first-class; CrewAI stays as the tool-calling execution layer | **recommended**, ~1-2 weeks |
| CrewAI unified `Memory` (recency/semantic/importance weights, consolidation) | better *retrieval* ranking; useful later for cross-conversation recall | wrong layer for this bug |
| **Rasa CALM** (flows, dialogue stack, slots; LLM emits commands, code decides) | closest match to the actual problem — digression, correction and topic-switch are first-class patterns, not regex patches. "no, I just want to know about course 2" is a *correction* command, not a re-classification | evaluate for v2; means rewriting modules as flows |
| **LangGraph** (typed state, checkpointer, `interrupt()`) | makes Layer 1 native; thread-scoped short-term memory with trim/summarise + a store for long-term | viable, real migration |
| **OpenAI Agents SDK** (handoffs + input filters) | A/B/C map onto handoffs; the input filter *is* "control what the receiving agent sees" — change 1 as a framework primitive | viable, lighter migration |
| Mem0 / Letta (MemGPT) / Zep | extract-consolidate long-term personalisation memory | **not this bug** — none would have prevented any incident above |

The principle every serious option shares: **the model interprets, the code decides.** That is
already half-present here — `/command` paths and the money path are deliberately deterministic,
and `context.py` keeps identity out of the token stream on purpose. Routing state is the
remaining place where the model is asked to decide something the database already knows.

## Rollout

1. **Instrument first, change nothing.** Structured routing trace + the 40 logged misroutes as
   eval scenarios. *Gate: baseline reproduces at ~15% on the replayed corpus.*
2. **Cut the contamination.** Participant-only window, episode segmentation, conditional CTA.
   *Gate: enquiry->B misroutes drop below 8%. If not, the diagnosis is wrong — stop and re-measure.*
3. **Make state authoritative.** `flow_context` + `ModuleResult`. Run write-only first: log what
   the asserted state *would* be next to what marker-matching actually decides.
   *Gate: they agree on >=95% of replayed turns.*
4. **Delete the overrides,** one at a time, re-running the suite after each.
   *Gate: no regression. An override that can't be removed is telling you something.*
5. **Fix the trapped flows.** Sticky-flow exit, decline-detection guard, pre-emptive Module C.
   *Gate: the four "registration declined" traps replay clean.*

## Tests to add under `evals/scenarios/`

`ROUTER_REDESIGN.md` listed five scenarios that were never added — `evals/scenarios/` still
holds only the original four. Add those, plus these replayed from the logs:

- Catalogue question immediately after a Module A reply ending in enrol-bait -> must stay Module A.
- `"How about the other one you mentioned"` after a course list -> Module A, never a registration prompt.
- A valid-shaped NRIC or email arriving mid-registration -> never classified as a decline.
- A mistyped NRIC mid-registration -> near-miss hint, never a decline.
- `"I want to cancel my enrollment"` -> cancellation path, never the decline reply.
- Four consecutive enquiry turns while a Module B flow is latched -> must break out by turn 3.
- `"how to pay the course"` from a participant with no enrollment -> resolved without a Module C round-trip.

## One thing not to do

Do not fix this by raising the model off `gpt-4o-mini`. It would probably reduce the misroute
rate and would hide the cause without removing it: the contaminated window stays, the regex
layer stays, and the next flow added reproduces the same failure class at a higher price per
turn. Fix the input first, then decide whether the model is still the constraint.
