# Sticky Router: stateful routing redesign

Companion doc to the full design (with diagrams): see the shared artifact link in chat/Slack. This file is the git-trackable summary for engineers working in this repo.

## Problem

Every inbound WhatsApp message is classified from scratch by `router.classify_free_text()` (called unconditionally from `orchestrator.handle_inbound()`), using only the last 6 chat-memory turns as context. There is no persisted "we are already mid-flow" signal anywhere. This is the direct cause of the reported ambiguous/inaccurate routing.

Confirmed in the current code (WA_CrewAI/backend):

- **`app/crews/router.py:181-186`** — the 3-label classifier output is parsed with `if "ENROLL" in out: B / elif "PAY" in out: C / else: A`. A malformed/unparseable LLM response is indistinguishable from a confident ENQUIRY classification — both silently become Module A. No confidence score, no "unsure" bucket, no distinguishing log line.
- **Three incompatible, private continuation schemes**, none shared with the router:
  - Module A: scans recent assistant turns for the literal word `"remind"` (`module_a.py` ~L102-152).
  - Module B: requires the last assistant message to end exactly with `"Shall I go ahead and enrol you?"` (`module_b.py` L136, L214-239).
  - Module C: re-parses a specific numbered-list text shape from a past reply (`module_c.py` L72-95).
- **`app/crews/router.py:187-243`** — two hand-written regex overrides exist purely because specific misroutes were observed live and patched after the fact. This is whack-a-mole: any new flow (e.g. cancellation) will reproduce the same failure class until it's observed and patched again.
- **No structured handoff.** `orchestrator._dispatch()` treats a module's return value as final. Module C is built with zero CrewAI coworkers — if the router mis-sends a message to it, it has no delegation escape hatch at all.
- **No multi-intent handling.** The router forces exactly one label per message; a compound message ("what's my schedule, and can I cancel it") gets fully resolved under one guessed intent with no detection that part of it went unaddressed.

Also confirmed missing (referenced in the original design but not implemented):

- **Cancellation has no conversational path.** `services/payments.request_credit_note()` / `approve_credit_note()` are complete but wired only to staff admin endpoints (`dbadmin.py`). No tool in `MODULE_B_TOOLS` exposes cancellation to a participant.
- **Reminder dispatch never fires.** Module A writes fully-formed rows to the `reminders` table (via `leads.py:save_lead()` → `repo.create_reminder()`), but `scheduler.py`'s two cron jobs (`run_followups`, `run_nightly_report`) never read that table. Only a generic 24h/72h/7-day nurture drip runs.

## Design: two-tier routing

**Tier 0 — deterministic state check (no LLM).** Before calling the classifier, check a new `conversation_state` row for this contact. If an active, unexpired flow exists, dispatch straight to its owning module. This covers essentially all in-flow turns (confirmations, slot answers, receipt photos) without touching the model.

**Tier 1 — confidence-gated LLM classification.** Only runs when the contact is at rest (no active flow). The classifier returns structured `{intent, confidence}`, not a bare label. Below a confidence threshold, send a disambiguation menu ("1) Course info 2) Enrol/change 3) Cancel 4) Payment") instead of guessing — never let a malformed or low-confidence response silently resolve to Module A.

### `conversation_state` schema

| column | purpose |
|---|---|
| `whatsapp_id` | key, one row per contact |
| `active_module` | `A` / `B` / `C` / `null` |
| `active_flow` | `awaiting_enroll_confirm`, `awaiting_cancel_reason`, `awaiting_reminder_date`, `awaiting_payment_proof`, ... |
| `flow_context` | JSON — slots collected so far |
| `expires_at` | stale flows auto-release to idle rather than trapping the next unrelated message |

This replaces the three private string-marker schemes with one source of truth that both the router and the owning module read/write.

### Reroute contract

Standardize module return values so a module can hand a wrong turn back instead of answering outside its lane:

```json
{ "reroute": true, "reason": "not a payment question", "candidate": "A" }
```

`orchestrator._dispatch()` treats this as non-terminal: update state, forward to `candidate` (or fall through to Tier-1 classification with the rejection as a hint). Cap at one hop to prevent ping-pong. This specifically fixes Module C's missing escape hatch.

## Filling the three gaps

1. **Cancellation in Module B** — add `request_cancellation_tool`, wired to the existing `services/payments.request_credit_note()`. Collect reason, confirm once, write a `pending` row, notify ops. Approval stays a human step in the admin portal.
2. **Reminder dispatcher** — add `run_reminder_dispatch` next to `run_followups`/`run_nightly_report` in `scheduler.py`: query due, unsent rows in `reminders`, send, mark sent. No LLM involved.
3. **Credit-note fan-out, decoupled** — pull the PDF-generate → email-customer → notify-ops → release-seat sequence out of the synchronous admin approval handler into its own worker triggered by the status change, so it's not coupled to the HTTP request/response cycle.

Both background pieces stay outside the conversational router entirely — they're triggered by time and DB state, not by chat, so they're never part of the router's decision space.

## One more dedup

`module_a.py` (~L697-708) and `module_b.py` (~L492-510) both restate the same "how to present shared tool output" instructions near-verbatim. Move it into one constant in `crews/_base.py` that both import, so a future prompt fix can't be applied to one module and forgotten in the other.

## Rollout (each phase independently shippable)

1. Add `conversation_state`, write-only — log what Tier 0 *would* decide next to what the old string-marker logic actually decides, before changing routing behavior.
2. Turn on Tier 0 sticky dispatch once the two agree closely enough. This is the change that fixes the reported symptom.
3. Confidence-gate Tier 1; retire the two regex overrides in `router.py:187-243` once the state store makes them unnecessary.
4. Add the reroute contract across all three modules, including Module C's missing escape hatch.
5. Build cancellation, reminder dispatch, and credit-note fan-out on top of a router that's already trustworthy.

## Test scenarios to add under `evals/scenarios/`

- Bare confirmation reply mid-enrolment with no other context → must stay in Module B without reclassification.
- Email reply continuing a reminder flow → must stay in Module A, not fall through to Module C on keyword match.
- Compound message ("what's my schedule, and can I cancel it") → both clauses addressed via reroute, or the response makes clear only one half was handled.
- Stray non-payment message sent to Module C → reroutes to Module A instead of a generic non-answer.
- Genuinely ambiguous cold-open message → disambiguation reply, never a low-confidence guess dressed as confident.
