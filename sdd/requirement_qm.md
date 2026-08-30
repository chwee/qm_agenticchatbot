# Requirements Document — Q&M AI-Powered Enquiry & Enrollment System

## Document Information

- **Feature Name**: Q&M AI-Powered Enquiry & Enrollment System (WhatsApp Agentic Backend)
- **Version**: 1.31
- **Date**: 2026-08-30
- **Author**: Q&M / WA_CrewAI project team
- **Stakeholders**: Q&M Dental Group training operations, Q&M accounts/finance staff, prospective and enrolled course participants, system administrators

## Version History

### v1.31 — 2026-08-30
Traced via the live query log across multiple days (`query_log_2026-08-25.txt`, `query_log_2026-08-28.txt`): a short, topic-less reply continuing a course enquiry — "Ya, how about course 2", "what course 2", "no. How about course 2" — was repeatedly misclassified ENROLLMENT and forced into Module B's registration-completeness prompt, even though the participant was asking a question, not stating enrollment intent (each case, the participant then had to correct the bot: "no. I just want to know about the course 2"). Root-caused to the Router Agent's Tier 1 classifier context: raw assistant reply text — which could include a stale turn from an earlier, unrelated conversation, since the window was not bounded to any conversation session — was fed into the classification prompt wholesale, and a message with no topic of its own ("how about course 2" only makes sense as a continuation of whatever the assistant just said) had nothing reliable to anchor it against.

Fixed per `docs/MEMORY_CONTEXT_REDESIGN.md`/`docs/DIALOGUE_STATE_REDESIGN.md` Phases 2–2.1: the classifier prompt now sees only the participant's own last 3 messages, plus one fixed-vocabulary label of the assistant's most recent action (e.g. `listed_courses`, `asked_which_intake_for_reminder`) — never raw assistant text — bounded to the participant's current conversation session (a new session begins after 60 minutes of inactivity), so an earlier, unrelated conversation is never read as still-relevant. Verified against 9 historical misroute cases found this way, replayed live through the current router (`backend/evals/replay_router_misroutes.py`, a new permanent regression harness reclassifying real logged conversations): the participant-only window alone fixed 5/9; the remaining 4 shared a shape (a short "how about course N" with only 1–2 prior participant turns) that needed the assistant-action label restored, in this narrower structured form, before all 9 passed.

A second, narrower gap found in the same area (Phase 3): a participant simply repeating the course code already under discussion mid-flow (e.g. restating "C2601" while a reminder for that same course was still pending) was treated as an explicit signal to switch modules, forcing the turn out of Tier 0 sticky dispatch into the classifier unnecessarily. Fixed by checking the pending flow's own recorded course/schedule (`conversation_state.flow_context`, dual-written since v1.28 but never read back until now) before treating a code as a genuine switch signal.

Separately, an unrelated bug found while reviewing the same code path: an incidental course-schedule lookup made mid-reminder (e.g. the participant idly asking about a different course's dates while still finishing a reminder for the first one) was silently overwriting `customers.preferred_course` with the wrong course, corrupting that reminder's own "which intake" question afterward.

- **Requirement 3 AC3 (corrected)**: free-text classification SHALL use only the participant's own last 3 messages plus a single fixed-vocabulary label of the assistant's most recent action (never the assistant's raw reply text) as context, bounded to the participant's current conversation session — a new session begins after 60 minutes of inactivity — so an earlier, unrelated conversation is never read as still-relevant. (Previously documented as "the participant's last 6 chat turns"; that 6-turn, mixed-role fetch still exists but now only feeds the deterministic overrides in AC5/AC7/AC8, not the classifier prompt itself.)
- **Requirement 3 AC11 (strengthened)**: a course or schedule code SHALL NOT by itself count as an explicit signal for a different module when it merely restates the course/schedule already recorded as part of the pending flow itself — only a code naming a course genuinely different from the one already pending signals a switch.
- **Requirement 4 AC2 (strengthened)**: WHILE a reminder (AC9–AC15) is still pending for a specific course THEN an incidental course-information lookup for a different course arising in the same conversation SHALL NOT overwrite the course already recorded as that pending reminder's subject.
- See `docs/MEMORY_CONTEXT_REDESIGN.md` and `docs/DIALOGUE_STATE_REDESIGN.md` for the full implementation record (Phases 0–4 complete; Phases 5–6 — full digression/off-topic handling during a registration prompt, and retiring the remaining keyword-override routing paths now that `conversation_state` exists — not yet started, tracked in those documents rather than `task_qm.md`).

### v1.30 — 2026-08-25
Found during a broader stress-test of v1.29's fix (not the originally reported bug itself): asking about two courses by catalogue position, then "remind me about one of those too," saved a reminder with `preferred_course` literally set to `"Course 1"`/`"Course 4"` — the raw text, never resolved to a real course name. That combination can never match a real course again, silently breaking the reminder-dispatch join (`repositories.reminders_due_for_dispatch()`, Requirement 10 AC5) for that reminder forever. Root cause: `services/courses.py: resolve_course_arg()` only recognised a bare digit ("1", "4") as a catalogue-position reference — "course 1"/"course 4" (a word prefix) fell through unresolved — and `services/leads.py: save_lead()`, unlike every other place in the codebase that accepts a course reference, never validated `preferred_course` against the actual catalogue before persisting it.

- **Requirement 4 AC2 (strengthened)**: WHEN a participant's `preferred_course` is captured THEN the System SHALL resolve it against the actual course catalogue (by C-code, catalogue position — including a "course N" phrasing, not only a bare number — or name) before persisting it; IF it does not resolve to a real, currently-offered course THEN the System SHALL NOT persist that field, rather than saving unresolved or invalid text that can never be matched back to a real course later.
- See `task_qm.md` v1.30 for the implementation record.

### v1.29 — 2026-08-25
Reported: after a reminder completed successfully, a follow-up enrolment attempt ("I want to attend Infection Control for Dental Clinics", then "Chen Wei, S1785000A") stayed misrouted to Module A, and — far more seriously — the reply ignored the enrolment attempt entirely and re-asked "which course would you like this reminder for?", having silently reverted and **deleted the already-saved reminder** as a side effect. Traced via the live query log and a direct database check (confirmed `customers.preferred_course`/`course_date` blank and the `reminders` row gone). Root-caused to two independent, pre-existing gaps — neither part of the v1.26–v1.28 router redesign, which only ever recorded what these mechanisms concluded:

1. `_ENROLLMENT_KEYWORD_RE` (Requirement 3 AC5) didn't recognise "attend" as enrolment intent, so a message stating it had no explicit signal to override the (also pre-existing) reminder-in-progress override, which forced it back to Module A.
2. Module A's reminder-ambiguity backstop (Requirement 4 AC15) decides whether a reminder is "still ambiguous" purely from a 2-turn text window that can still contain a stale "remind" mention long after a reminder actually completed — it never checked whether the course+date it was about to revert had already been genuinely saved. A later, unrelated message that named no course was read as "still resolving an ambiguous reminder" and triggered the revert-and-delete path meant for a reminder that's genuinely still in progress.

- **Requirement 3 AC5 (strengthened)**: the explicit-enrollment-signal keyword set SHALL also recognise "attend" (and its inflections), and SHALL recognise any inflection of "enrol"/"enroll" (previously only the bare verb forms matched, e.g. "enrolling"/"enrolled" did not).
- **Requirement 4 AC15 (strengthened)**: the reminder-ambiguity backstop SHALL NOT revert or delete a participant's course/date or an already-persisted reminder record on the basis of a "still pending" signal alone — it SHALL first confirm, against the actual persisted `reminders` record, that the course+date it is about to revert was not already genuinely saved before the current turn began. A reminder already on record SHALL be left untouched by this backstop regardless of what the recent conversation text otherwise suggests.
- See `task_qm.md` v1.29 for the implementation record.

### v1.28 — 2026-08-23
Explicit request to implement the remaining phases of `ROUTER_REDESIGN.md` (Phases 3–5; Phase 1+2 already implemented as v1.26/v1.27's `conversation_state`/Tier 0 sticky routing). Scoped in planning, with the user's explicit sign-off on two deviations from the doc's literal text: (1) the two existing deterministic routing overrides (Requirement 3 AC5, AC7) are kept rather than retired — Task 45's audit found they still do necessary work on a flow's first turn, before any sticky state exists; (2) the reroute contract is scoped to Module C only (the concrete gap the doc names — zero delegation coworkers) rather than all three modules, since Module A/B already have an equivalent via existing agent-to-agent delegation (Requirement 12).

- **Requirement 3 AC13 (new)**: the Router Agent's classification SHALL report a confidence level alongside its label. WHEN confidence is low AND no deterministic signal (an explicit enrollment/payment signal, a pending confirmation, or a reminder/enrollment flow already in progress) resolves the message THEN the System SHALL present a short disambiguation menu instead of guessing, and SHALL dispatch the participant's next reply against that menu (a number or a keyword) rather than reclassifying it from scratch.
- **Requirement 3 AC14 (new)**: WHEN Module C (payment) determines the participant's message is not actually about payment THEN the System SHALL redirect it to the correct module (course enquiry or enrollment) and produce a real answer, rather than Module C giving a generic non-answer — capped at one redirect per message.
- **Requirement 10 AC5 (new)**: WHEN a saved reminder's course intake date is within the configured lead time (3 days) AND the reminder has not already been sent THEN the System SHALL email the participant a reminder once daily at the configured hour, and SHALL NOT send the same reminder more than once.
- **Requirement 11 AC5 (new)**: a participant SHALL be able to request cancellation of their own enrollment conversationally (WhatsApp free text), resolving which enrollment from an invoice number they state or their sole/latest active enrollment, with the same explicit-confirmation-before-acting standard already required for enrollment itself (Requirement 5 AC2–AC4).
- **Requirement 11 AC3 (strengthened)**: credit note PDF generation and participant email delivery SHALL NOT block the staff approval action's own response — the approval SHALL complete and confirm immediately, with the PDF/email delivered by a short-interval background dispatch (not coupled to the HTTP request/response cycle).
- See `task_qm.md` v1.28 for the full implementation record.

### v1.27 — 2026-08-23
Follow-up edge-case audit of v1.26's sticky routing, requested directly: verify Module A/B routing stays correct, verify a fresh payment query (no image) reaches Module C, and check `ROUTER_REDESIGN.md` for other suggested improvements. Found one genuine bug distinct from Tier 0: `_reminder_in_progress()`'s override (Requirement 3 AC7, pre-dating v1.26) forces ENQUIRY whenever a reminder is in play, exempting only an explicit enrollment signal — never a payment one. A payment claim arriving mid-reminder ("I already paid for my other course, can you check") was forced back to Module A instead of Module C, in both `_heuristic()` and the post-LLM cross-check in `classify_free_text()`.

The naive fix (mirror the enrollment exemption exactly) was checked against `ROUTER_REDESIGN.md`'s own explicitly flagged risk first — "email reply continuing a reminder flow must stay in Module A, not fall through to Module C on keyword match" — and confirmed live: a bare email reply like `pay@mycompany.com` (answering the reminder's own email-ask, Requirement 4 AC10-AC11) contains "pay" as a whole word and would wrongly flip to Module C under a blanket exemption. Resolved with a bare-email guard (`_BARE_EMAIL_RE`): a message that's entirely just an email address never counts as payment intent, regardless of what its address happens to contain; a payment claim combined with other text is unaffected.

- **Requirement 3 AC7 (strengthened)**: the reminder-in-progress override SHALL also yield when the participant's message states genuine payment intent (a payment keyword not confined to being part of an address the message is otherwise entirely just relaying) — mirroring the existing exemption for an explicit enrollment signal. A message that is nothing but an email address SHALL NOT be treated as stating payment intent on that basis alone, regardless of what substring its address contains, so a reminder flow's own email-ask is not disrupted by this exemption.
- See `task_qm.md` v1.27 for the implementation record, including the full edge-case battery run and the deferred `ROUTER_REDESIGN.md` items (Phases 3-5) not in scope for this pass.

### v1.26 — 2026-08-23
Reported: a fully registered participant ("Chen Wei") who had just been shown Module B's numbered intake list for a course replied "intake 1" — an unambiguous selection — but it was routed to Module A instead (Module A has no enrollment tool, so it delegated to Module B, received back a pending confirmation question, and composed a fabricated "a colleague will handle the enrollment for you" reply rather than actually enrolling or relaying the question). Root-caused to `_reminder_in_progress()`'s 2-turn assistant-window check (Requirement 3 AC7, v1.18) picking up the word "remind" from an unrelated, already-COMPLETED reminder confirmation two turns earlier, and treating that stale mention as if a reminder exchange were still open.

This is the latest of many rounds of point-patches to the same underlying gap (Requirement 3 AC5, AC7, AC8, and their v1.16/v1.18/v1.19/v1.20/v1.22 revisions): the Router has never had any persisted signal for "a specific module is already mid-flow with this participant" — every turn, it re-derives that purely by pattern-matching raw recent chat text, and every fix so far has been another keyword/window adjustment that closes one observed failure shape while remaining exposed to the next. Module A (reminder stage) and Module B (enrollment confirmation) each also maintain their own private, incompatible scheme for recognising "my own flow is still pending" — neither is visible to the Router or to each other.

Adopted the user's proposed structural fix instead of a further point-patch: the Router gains a persisted, explicit "which module/flow is this participant mid-conversation with" signal, written by whichever module leaves a question open and read deterministically before any text-based classification runs. Scoped to fixing the routing mechanism itself (this AC set); the existing keyword-based classification (AC1–AC9) is left in place as the fallback for participants not currently mid-flow, and a fuller redesign (confidence-gated classification, a reroute contract between modules, a conversational cancellation flow, automated reminder dispatch) was discussed but deliberately deferred, not part of this revision.

- **Requirement 3 AC10 (new)**: WHEN a module composes a reply that leaves a question specific to its own flow unresolved (e.g. Module A's reminder exchange still missing an email, course, or intake per Requirement 4 AC9–AC15; Module B's enrollment still awaiting an intake selection, or awaiting the participant's explicit confirmation per Requirement 5 AC2) THEN the System SHALL record which module and which flow is pending for that participant, so their very next reply can be dispatched back to the correct module without depending on the message text alone to re-establish that context.
- **Requirement 3 AC11 (new)**: WHEN a participant's new message arrives AND a pending flow (AC10) is on record for them AND it has not expired (30 minutes of inactivity since it was last set) THEN the System SHALL dispatch that message directly to the module owning the pending flow, without invoking the Router Agent or the keyword heuristic — UNLESS the message itself carries an explicit, unambiguous signal for a different module (an enrollment keyword/course code per AC5, or a payment keyword), in which case the message SHALL instead be classified normally per AC3–AC9.
- **Requirement 3 AC12 (new)**: WHEN a module's reply resolves its own pending flow (the outstanding question is answered, declined, or the flow otherwise completes) THEN the System SHALL clear the recorded pending state for that participant, so a subsequent, unrelated message is classified normally again. A module SHALL only ever clear pending state that it itself owns (recorded by the same module) — it SHALL NOT clear a pending flow recorded by a different module.
- See `task_qm.md` v1.26 for the implementation record.

### v1.25 — 2026-08-23
Explicit feature request: when a conversation has touched on multiple courses, the reminder flow (AC9–AC14) must not try to recall or infer which course/schedule to use — it must ask the participant to state both explicitly, in addition to email, before saving anything; an incomplete reminder should simply be left unresolved rather than nagged about while other queries continue. Applies identically whether the reminder was offered by the System (AC9) or requested unprompted by the participant. Implementing and live-testing this surfaced four compounding bugs, all fixed in the same pass — see `task_qm.md` v1.25 for the full implementation record:
1. The 2-turn assistant-only window used to detect "is a reminder question pending" never captured a participant-initiated request (the word "remind" only ever appeared in the participant's OWN message, never restated by the assistant's follow-up), so the ambiguity guard silently never ran for that path at all.
2. Course-ambiguity detection itself was scoped too narrowly (the same 2-turn window) to reliably see courses discussed several turns before the reminder was actually requested.
3. Even with a correct SYSTEM DIRECTIVE forbidding it, the conversational agent was observed naming a specific (wrong) course before asking which one was meant — requiring the same deterministic, agent-bypassing backstop already used for AC13's intake question.
4. A stage-detection regex (`\bintake\b`) meant to distinguish "answering the intake question" from "answering the course question" false-matched the unrelated boilerplate ending every ordinary course-schedule reply ("...which intake you'd like to go for..."), which could suppress the ambiguity check entirely whenever a course/schedule reply was recently in the conversation.

- **Requirement 4 AC15 (new)**: see below.
- **Router cross-check simplified**: the router's two separate reminder-continuation overrides (one scanning the assistant's recent turns, one scanning the participant's new message) were consolidated into one shared check (`_reminder_in_progress()`, delegating to Module A's own detection) once bug (1) above showed neither alone was sufficient — a bare email-only reply continuing a participant-initiated request needed both.

### v1.24 — 2026-08-23 (decision recorded, no requirement or code change)
Confirmed, after investigating `leads`'s schema constraints and existing consumers (`design_qm.md` v1.24), that Requirement 4 AC14's reminder storage correctly belongs in its own table rather than the legacy `leads` table — `leads` is structurally one-row-per-participant (`UNIQUE` on phone and whatsapp_id) and three existing code paths depend on that, including enrollment-status sync that would otherwise mark unrelated reminders "enrolled." No requirement changed; recorded for the future scheduled-reminder-agent's benefit (Requirement 10).

### v1.23 — 2026-08-23
Follow-up to v1.22 — re-reported as reminder data being "overwritten," but a direct database check against the exact live test the report referenced confirmed the data was correct (two independent, valid rows). The report traced to a visibility gap: nothing in the admin dashboard surfaced the new `reminders` table, so there was no way to see this through the interface being checked. Requirement 4 AC14 already covers the underlying data guarantee; no requirement change — this adds the admin-facing visibility for it (`task_qm.md` v1.23).

### v1.22 — 2026-08-23
Explicit feature request, correcting an earlier assessment in this same session that treated the underlying gap as a non-issue: a participant should be able to have more than one reminder on file at once — one per course — not have a second reminder for a different course silently overwrite the first. `customers`/`leads` were never designed to hold more than one course/date pair per participant; this adds a dedicated table for it. Implementing this surfaced three further, related gaps in the same area, all fixed in the same pass: a reply naming only a course (not shaped like an email/confirmation/date) in answer to "which course would you like a reminder for?" had no deterministic routing anchor; a course change with no accompanying date left a stale date from a different, earlier course attached to the new one; and the agent was observed, live, pairing a real intake date with the wrong course in its own tool call.

- **Requirement 4 AC14 (new)**: a participant SHALL be able to have more than one reminder recorded at once, one per distinct course, without an earlier one being overwritten when a later, different one is saved. The System SHALL NOT record the same course and intake date combination more than once for the same participant. A reminder's recorded course and intake date SHALL always correspond to an actual, currently-scheduled intake of that course — never a combination that doesn't correspond to any real offering, and never a date left over from a different course the participant previously discussed.
- **Requirement 3 AC7/AC9 (merged and broadened)**: recognising a reply as continuing a pending reminder exchange SHALL depend only on the recent conversation referencing reminder intent, not additionally on the reply itself matching a specific expected shape (an email, an agree/decline word, a date, an intake reference) — replaced with a single rule covering every stage of the exchange, since each shape-specific version was found missing a real reply shape the others didn't cover.
- See `task_qm.md` v1.22 for the full implementation record, including the new `reminders` table.

### v1.21 — 2026-08-23
Reported: a reminder's final confirmation named the wrong course's intake dates — the course actually saved (correctly, per the conversation) disagreed with the dates listed in the same reply. Root-caused to two independent mechanisms computing "which course is this reminder for" separately and drifting apart: a pre-built instruction guessed the course from a customer-record snapshot taken before the turn started, while the agent's own save used its live understanding of the conversation — and when a course briefly mentioned earlier had since been abandoned in favour of a different one, the two disagreed. Per explicit guidance to reduce the number of interacting special cases rather than add another patch, the mechanism was simplified to a single authority: nothing about which course or dates a reminder needs is decided before the participant's turn is processed; a check taken only after processing, against the actual database result, is now the sole source of truth.

- **Requirement 4 AC13 (strengthened)**: the course and intake options presented when asking a participant which date to be reminded about SHALL reflect the course actually recorded for that reminder as of that point in the conversation — not a course inferred from an earlier point in the conversation that the participant has since moved on from.
- See `task_qm.md` v1.21 for the implementation record, including the resulting simplification (two now-unused helper functions and an unused marker/tuple removed).

### v1.20 — 2026-08-23
Reported: a fully registered participant who reached Module B's numbered intake list ("1. [SH2609] 28 Jul 2026...") and replied "intake 1" never got enrolled — the reply was silently absorbed and the enrolment was never created, even though multi-course enrolment had been verified working earlier the same day. Root-caused to a regression in v1.18's `_pending_reminder_intake_ask()` (Requirement 3 AC9): it checked only for the word "intake" in the recent conversation to decide a reply belonged to Module A's reminder flow — but Module B's own enrolment intake question ("which intake would you like to register for?") also contains "intake", so it was wrongly claiming the reply too, hijacking it away from Module B before the enrolment agent ever saw it.

- **Requirement 3 AC9 (strengthened)**: recognising a reply as answering Module A's reminder intake-date question SHALL require the recent conversation to reference both "remind" and "intake" together, not "intake" alone — Module B's own enrolment intake question never mentions "remind", so this distinguishes the two without weakening AC9's original intent.
- See `task_qm.md` v1.20 for the implementation record.
- Separately, in the same review: confirmed (not a bug) that `customers`/`leads` retain only the single most recent course of interest — a second reminder request for a different course correctly overwrites, rather than accumulates alongside, the first. Storing more than one simultaneously would require dedicated storage as part of Requirement 10 (Scheduled Follow-ups & Reporting), which is not yet implemented; no requirement change made.

### v1.19 — 2026-08-23
Reported: a repeat reminder request for a different course, from a participant who'd already completed one reminder, silently failed to save — read by the reporter as "the lead record can only be updated once." Root-caused via the live query log to four distinct, compounding gaps, none of which was actually a database restriction (`leads`/`customers` upsert correctly on every call, confirmed by inspection): (1) the router's reminder-intent detector (v1.17) missed the natural inflection "remindered," misrouting the request into the registration gate; (2) an explicit course reference ("course 1") repeatedly resolved to whichever course was most recently discussed instead of the one actually named, including a same-turn staleness bug where a deterministic course-interest update wasn't visible until the following turn; (3) the reminder-pending detector (v1.18) required "email" to co-occur with "remind," which never held for a second reminder in the same conversation since the email was already on file and never re-mentioned; (4) once (1)-(3) were fixed, a date-matching heuristic (v1.18) treated any two intakes sharing the same year as ambiguous, and — independently — the agent was found consistently inventing the earliest intake as a default rather than asking, when it should have asked.

- **Requirement 3 AC8 (strengthened)**: reminder-intent detection in the participant's own message must match any inflection of "remind" (e.g. "remindered," "reminding"), not only "remind"/"reminder"/"reminders".
- **Requirement 4 AC8 (strengthened)**: an explicit course reference (a C-code or catalogue position) SHALL be resolved to that exact course for the current turn, not to whichever course was most recently discussed in conversation — and any same-turn deterministic update to the customer's preferred course SHALL be visible to that same turn's processing, not only turns after it.
- **Requirement 4 AC13 (strengthened)**: recognising a reminder exchange as still in progress SHALL NOT require the word "email" to also be present — a repeat or later-stage reminder turn may have no reason to mention email again. Matching a participant's chosen intake against the course's schedules SHALL prefer the more specific match when multiple schedules share a common element (e.g. a year); the System SHALL NOT save an assumed or default intake date in place of asking, and SHALL NOT state or imply a specific date was saved unless the participant actually specified one.
- See `task_qm.md` v1.19 for the full implementation record.

### v1.18 — 2026-08-22
Three related fixes found while investigating one reported transcript ("Three Xin"), where a reminder-offer reply was misrouted to the registration prompt, plus the participant's own follow-up observation that a reminder could be saved with no specific intake date even though the course had two. Fixing the second issue properly surfaced two further, more severe gaps: `customers` had no column to persist a resolved intake date to at all (so a "date saved" confirmation was never actually true), and the existing fabrication guard (Requirement 4 AC12, v1.14) turned out to falsely block a **genuine, successfully invoiced enrollment**, contradicting real system state — a more severe failure mode than the guard was built to prevent.

- **Requirement 3 AC7 (strengthened)**: recognising a pending reminder offer/email-ask must span the last two assistant turns, not only the latest one — the offer and its follow-up ask often land on separate turns once the participant replies without giving an email yet, and the ask-only turn has no reason to repeat the word "remind" on its own.
- **Requirement 3 AC9 (new)**: a reply to Module A's "which intake?" ask (Requirement 4 AC13) must stay with Module A regardless of the reply's shape (a bare date, ordinal, or SH-code) or what a general classifier would otherwise conclude.
- **Requirement 4 AC13 (new)**: a reminder SHALL NOT be considered complete — and the participant SHALL NOT be told it is — while its course has more than one upcoming intake and none has been resolved; the System SHALL ask which intake, and only report success once one is chosen, mirroring Requirement 5 AC5's enrollment-side rule.
- **Requirement 4 AC12 (reworded)**: verifying a claimed enrollment is genuine SHALL be based on the actual persisted enrollment record for that participant (e.g. a fresh database read), not on any per-turn in-process flag — a ContextVar-based flag was found, live, to not reliably reflect a real, successful enrollment created via delegation, and blocking a real success on a false negative is a more severe failure than the fabrication it was built to catch.
- See `task_qm.md` v1.18 for the full implementation record, including the `customers.course_date` schema gap this surfaced.

### v1.17 — 2026-08-22
Closes a gap distinct from the reminder-reply routing fixed in v1.15/v1.16 AC7: those only cover a participant *replying* to a reminder offer Module A already made. A participant *volunteering* reminder intent out of the blue — with no prior offer pending — had nothing in the Router to anchor it to ENQUIRY, so it was left entirely to the LLM classifier. Observed live (from an earlier transcript this session): "may I change my mind. Can you send reminder for me" was classified ENROLLMENT (the LLM read "change my mind" as reconsidering enrollment), which routed to Module B and tripped the registration-completeness prompt before the participant had ever stated any enrollment intent — confusing the reminder ask with an enrollment ask.

- **Requirement 3 AC8 (new)**: a participant's own new message stating reminder intent is classified ENQUIRY deterministically, independent of whether a reminder offer is currently pending (AC7) — unless the same message also carries an explicit enrollment signal (AC5), which still wins.
- See `task_qm.md` v1.17 for the implementation record.

### v1.16 — 2026-08-22
Fixes two regressions introduced by the v1.15 fixes, both found on live re-test of the same conversation shapes those fixes were meant to close. First, the NRIC near-miss detector (Task 6.4) false-triggered on the ordinary word "MENTIONED" (9 letters, starts with M — coincidentally matches the NRIC shape with zero digits). Second, the fuzzy reminder-context detector (Task 31) still required "email" and "remind" within the same sentence, and failed on a reply that legitimately splits them across two sentences.

- **Requirement 2 AC12 (strengthened)**: near-miss NRIC detection must not fire for a shape-matching token with no digits at all.
- **Requirement 4 AC11 (strengthened)**: reminder-context recognition must not depend on any distance or sentence-boundary constraint between the relevant wording.
- See `task_qm.md` v1.16 for the implementation record.

### v1.15 — 2026-08-22
Fixes two bugs found in one reported transcript. First, a fourth recurrence of the name-corruption bug: "I am just trying to explore my option." → `full_name = "Option"` — the Task 6.3 cue-required redesign closed the general case but not this one, since "I am"/"I'm" is itself an ambiguous cue (as likely to precede an ordinary statement as a name). Second, a reminder-offer reply ("limmin@test.com") got routed to Module C instead of Module A, because Module A's own paraphrasing of the required "share your email" sentence didn't exactly match the fixed marker text the routing logic depended on — the third time an exact-marker-reproduction mechanism has proven insufficiently reliable on its own this session (after a casing failure and, before that, none at all).

- **Requirement 2 AC13 (new)**: the regex name-extraction cue list is restricted to phrases where a name is the *only* grammatically valid continuation — ambiguous phrases like "I am"/"I'm" are removed from it and now correctly fall through to the semantically-aware LLM path instead.
- **Requirement 4 AC11 (reworded)**: recognising a reminder-offer/email-request in progress must not depend on exact-sentence reproduction — the agent's own paraphrasing of the same request must still be recognised.
- See `task_qm.md` v1.15 for the implementation record.

### v1.14 — 2026-08-22
Fixes a critical bug found via the live query log: a participant's mistyped NRIC ("SI785000W" — capital I for digit 1) was silently rejected with no explanation, abandoning the registration flow; the participant's next message then reached Module A, which fabricated a complete enrollment confirmation ("I've got you enrolled... invoice... shortly") after only calling the lead-capture tool — no enrollment was ever created. This is the most severe bug found this session: a chatbot falsely telling a participant they are enrolled in a paid course.

- **Requirement 2 AC12 (new)**: an NRIC-shaped-but-invalid reply must be named specifically, and must keep the registration prompt open for a corrected resend rather than silently abandoning the flow.
- **Requirement 4 AC12 (new)**: Module A must never state or imply an enrollment happened unless a real enrollment record was actually created that turn — enforced structurally, since the equivalent prompt instruction was already present and was not followed.
- See `task_qm.md` v1.14 for the implementation record.

### v1.13 — 2026-08-22
UX polish, not a bug fix: the fixed registration-confirmation template (`orchestrator.py: _REG_CONFIRM`) always included a generic "How can we help you today?" capability menu and a "just ask me anything" filler line — redundant at its one call site, since it's always immediately followed by the actual resumed content (e.g. the course-switch note from v1.12), and its "• Enrol in a course" bullet sat confusingly right next to that real enrolment content.

- **Requirement 2 AC8**: reworded to specify the confirmation must be brief and profile-specific, not padded with menu/filler text when real content immediately follows.
- See `task_qm.md` v1.13 for the implementation record.

### v1.12 — 2026-08-22
New capability, not a bug fix: a participant discussing one course, then typing a *different, individually valid* course reference two turns later (e.g. a typo — "course 1" when they meant "course 4", right after discussing course 4) got enrolled with no acknowledgment of the switch. Traced via the live query log first — confirmed the system correctly resolved and validated exactly what was typed; the gap was that nothing flagged the topic jump against recent context before committing to it.

- **Requirement 5 AC12 (new)**: the confirmation summary must name both courses and invite confirmation when the resolved course differs from one explicitly discussed more recently in the same conversation. Deliberately doesn't try to determine which course was "really" intended, and doesn't block the confirmation step — it gives the participant one clear, explicit chance to catch their own mistake before the existing yes/no confirmation.
- See `task_qm.md` v1.12 for the implementation record.

### v1.11 — 2026-08-22
Fixes a gap in the v1.7/v1.10 reminder-offer feature: agreeing to the offer without giving an email got treated as if an email had been provided, and a subsequent bare email reply was misrouted away from Module A entirely, so `leads.email` stayed null even though the participant did eventually supply one.

- **Requirement 4 AC10 (new)**: agreeing to the reminder offer without an actual email must not be treated as if the email was given — the System must ask for it, the same standard already applied to registration and enrollment confirmation.
- **Requirement 4 AC11 / Requirement 3 AC7 (new)**: a reply to the reminder offer or its follow-up email request must stay routed to the module that made it, deterministically, regardless of what a general classifier would otherwise conclude from the reply's content.
- See `task_qm.md` v1.11 for the implementation record.

### v1.10 — 2026-08-21
Fixes two bugs from the same reported transcript: a third recurrence of the name-corruption bug (this time "sorry, change my mind" → full_name), and Module B mis-handling a bare "Ok sure" as re-confirming a declined enrollment offer instead of answering its own prior message (an improvised reminder offer it had no way to fulfil).

- **Requirement 2 AC11 (rewritten)**: the regex name-extraction path now only ever extracts on an explicit self-introduction cue, not by eliminating filler words from an arbitrary sentence — closes the whole class of bug rather than the specific phrasing reported this time, after two prior filler-list patches (v1.5, v1.7) each only closed one specific phrasing.
- **Requirement 5 AC10–AC11 (new)**: a bare generic reply with no pending confirmation and no course/intake reference of its own must not be reinterpreted as restarting enrolment; an offer to save an email/interest for a reminder must only be made if it's actually going to be fulfilled (via delegation to Module A), never as an empty promise.
- See `task_qm.md` v1.10 for the implementation record.

### v1.9 — 2026-08-21
Fixes a reported false-positive registration trigger: a bare "yes" replying to a generic opener ("are you interested in our training courses?") opened the registration gate, even though the participant hadn't selected any specific course or intake. Requirement 3 AC5 already specified the correct rule (a bare confirmation is only ENROLLMENT when the assistant's prior message actually offered a specific course/intake) and `design_qm.md` already documented it as enforced by `_last_assistant_offered_enrollment()` — but that check was only wired into the no-API-key heuristic fallback, never into the LLM classification path actually used whenever an API key is configured. AC5 reworded to state the rule bidirectionally and explicitly deterministic.

- **Requirement 3 AC5**: reworded to cover the negative case explicitly (no signal + no prior offer → ENQUIRY, deterministically) — closing the gap between the documented design and the actual code.
- See `task_qm.md` v1.9 for the implementation record.

### v1.8 — 2026-08-21
New capability, not a bug fix: Module A and Module B agents can now delegate sub-tasks to each other mid-turn (via CrewAI's built-in agent-to-agent delegation) so a single compound message spanning both specialities gets one complete reply, instead of the entry module only answering the half its own tools cover. Module C is explicitly out of scope for this pass.

- **New Requirement 12**: Agent-to-Agent Delegation (Module A ↔ Module B) — see its own section for full ACs. The two properties that must hold regardless of delegation are stated explicitly: the registration gate (AC1/AC3) and the Task 10.8 Enroll-Participant tool-gating (AC4).
- See `design_qm.md` v1.8 and `task_qm.md` v1.8 for the implementation record and verification detail.

### v1.7 — 2026-08-21
Fixes three related bugs found from one test transcript: a participant declining registration got a plain sentence ("I just want to explore the course") saved as their `full_name`; their stated course interest ("I interest in course 4") never reached the `customers` table at all, both because the lead-capture tool wrote to the deprecated `leads` table instead, and because nothing captured interest deterministically when no lead-capture tool call happened; and there was no proactive prompt offering a reminder before a non-committing conversation ended.

- **Requirement 2 AC11** (new): the registration name-extraction regex path must reduce an ordinary sentence to nothing extractable, not just recognise already-known decline phrases — closes the gap the LLM-path fix (v1.5) didn't cover, since this instance never reached the LLM path at all.
- **Requirement 4 AC8–AC9** (new): explicit course references (C-code or catalogue position) must deterministically persist as `preferred_course`; a winding-down conversation should, once, summarise discussed courses and offer to save an email for a reminder.
- See `task_qm.md` v1.7 for the implementation record.

### v1.6 — 2026-08-21
Fixes a reported bug: a participant selected an intake, declined the confirmation ("not yet, I change my mind on the intake"), then selected a different intake — the system enrolled them directly on that second selection, skipping the confirmation step entirely (Requirement 5 AC2). This is the same LLM-judgement-only gap explicitly flagged as an accepted risk in Task 10.4 (v1.2) resurfacing in a new trigger shape; now closed with a structural fix rather than another prompt rewording, since prompt-only attempts at this exact class of problem had already failed twice before (Task 10.4's original two rewrites).

- **Requirement 5 AC2**: reworded to make explicit that a fresh confirmation is required every time a course+intake resolves, not just the first time — including after a decline and re-selection.
- See `task_qm.md` v1.6 for the implementation record.

### v1.5 — 2026-08-21
Fixes two reported bugs from the same conversation transcript: (1) a participant naming a course intake date that doesn't exist (e.g. "register me for this course on 1-2 Jan 2026") got the requested date silently dropped once registration completed, re-shown the real options with no acknowledgment of what they'd asked for — confusing, reads as the bot having ignored them; (2) that same trigger message (no name present) caused the LLM name-extraction fallback to hallucinate a plausible-looking but fabricated name ("Not Moment") onto the customer record, since that path had no validation the regex path already enforced.

- **Requirement 5**: new AC5a — an invalid/unmatched intake reference must be explicitly acknowledged, not silently dropped.
- **Requirement 2 AC10**: strengthened — the LLM name-extraction result must now pass the same structural check as the regex path, plus a new provenance check (every word of the extracted name must actually appear in the participant's own message) — closes the hallucination gap.
- See `task_qm.md` v1.5 for the implementation record.

### v1.4 — 2026-08-21
Fixes a reported financial-correctness bug: a participant with more than one enrollment (especially two intakes of the same course) could get the wrong invoice number back when asking to pay for, or asking the balance of, a specific one they'd referred to by list position ("item 2") or by date — the system would silently resolve to a different enrollment (typically whichever was most recently created) instead. Root cause: multiple independent "just look up the latest enrollment" defaults across the codebase, and a multi-enrollment status listing whose numbering was left to the LLM to compose each time rather than being a fixed, referenceable format.

- **Requirement 5 AC6**: added the fixed-numbered-format requirement for multi-enrollment status listings.
- **Requirement 6**: added new AC10 — a specific enrollment referenced by list position or course+date must resolve to that exact invoice, not a default.
- See `task_qm.md` v1.4 for the implementation record.

### v1.3 — 2026-08-20
Slash commands are removed entirely. A message beginning with `/` is no longer special-cased in any way — it is treated exactly like any other free-text message and routed through the Intent Router's classification (Requirement 3). This is a deliberate, explicitly-accepted tradeoff: `OPENAI_API_KEY` is now effectively required for the system to do anything precise (enrol, check status, resend an invoice); without it, participants only get each module's canned fallback reply, not deterministic command execution.

**Explicitly unaffected** — neither of these was ever command-syntax-dependent, so command removal doesn't touch them: (1) Module C's money path (Requirement 6) — settlement is triggered by an image attachment, not by `/pay` text, and continues to bypass the LLM entirely; (2) the C-code+SH-code quick-enrol shortcut (Requirement 5) — it matches codes anywhere in free text, never required a `/` prefix, and remains the sole confirmation-exempt fast path now that `/enroll` no longer exists as a distinct command.

- **Requirement 2**: AC4's enrollment-intent trigger no longer mentions the `/enroll` command — enrollment intent is now solely a free-text `ENROLLMENT` classification (Requirement 3).
- **Requirement 3** (Intent Routing): AC1–AC2 (command-registry parsing, usage-error replies) removed outright — replaced with a single rule that `/`-prefixed text is never special-cased. AC3–AC7 (image routing, free-text classification, heuristic fallback, bare-confirmation handling, minimum-registration routing) are unchanged, since none of them depended on command parsing.
- **Requirement 4** (Module A): AC6 (deterministic `/help /courses /fees /schedule /sfc` with no API key) removed — replaced with a canned-fallback-only behaviour, since there is no deterministic command path left to substitute for the agent.
- **Requirement 5** (Module B): former AC6 (`/enroll` command creates directly, no confirmation) removed outright — every enrollment now goes through the confirm-then-create flow (AC2–AC4) unconditionally, except the quick-enrol shortcut noted above. Acceptance criteria renumbered (former AC7–AC10 shift to AC6–AC9).
- **Requirement 6** (Module C): unchanged — see "Explicitly unaffected" above.
- **Requirement 7**: renamed *Free-Text-Only Interaction (Slash Commands Removed)* and rewritten from the ground up — see its own section for the full replacement.
- **NFR Reliability / Performance**: the fallback behaviour on a missing/failing API key is now "canned reply or keyword heuristic," not "deterministic command execution," since the latter no longer exists.
- **Scope, Feature Summary, Success Criteria, Glossary**: every mention of slash commands as a parallel interaction mode removed or reworded.

### v1.2 — 2026-08-20
Two changes, both scoped to the conversational (free-text) enrollment flow — the deterministic `/enroll` command and the C-code+SH-code quick-enrol shortcut are explicitly unaffected, by design.

- **Requirement 2**: clarified (not a new acceptance criterion — Requirements 4 AC7 and 10 AC2–AC3 already implement this) that progressive registration's primary purpose is enabling every enquiry, including from participants who never complete a full profile or purchase, to be captured as a lead and reachable by the scheduled follow-up reminder.
- **Requirement 5** (Module B): adds a mandatory confirmation step to the conversational enrollment path — once a specific course and intake are resolved, the System must summarise them back to the participant and obtain explicit agreement before creating the enrollment; a decline is acknowledged (not enrolled, not treated as an error) and the conversation continues normally. Acceptance criteria renumbered (new AC2–AC4 inserted; former AC3–AC8 shift to AC5–AC10).

### v1.1 — 2026-08-19
Registration is now **progressive** instead of an upfront gate. A participant may enquire about courses using nothing more than their WhatsApp identity; full profile details (name, NRIC, email, confirmed phone) are only requested at the point they express enrollment intent. This change touches registration, routing, Module A, Module B, deterministic commands, and lead scheduling.

- **Requirement 2** (renamed *Progressive Registration*): first contact now creates only a minimum registration (`whatsapp_id`); the full four-field profile is requested just-in-time when enrollment intent is detected, not before every substantive reply.
- **Requirement 3** (Intent Routing): explicitly allows ENQUIRY routing for minimum-registered participants; only ENROLLMENT-classified messages trigger the Requirement 2 profile-completion check.
- **Requirement 4** (Module A): adds a standing lead-update rule — every enquiry, from a registered or minimum-registered participant alike, updates the lead/customer record, not only when contact details are explicitly volunteered.
- **Requirement 5** (Module B): adds an explicit registration-completeness check as the first step of any enrollment attempt, deferring to Requirement 2 when incomplete and resuming the original enrollment request once complete.
- **Requirement 7** (Deterministic Commands): clarifies that enquiry commands (`/courses /fees /schedule /sfc /help`) require only minimum registration, while `/enroll` triggers the profile-completion prompt when the profile is incomplete.
- **Requirement 10** (Scheduled Follow-ups): lead follow-up reminders are now conditional on an email address being on file; leads without an email are skipped rather than reminded by another channel.
- **Non-Functional (Usability)** and **Scope**: reworded to reflect that the "ask for missing information" behaviour is triggered by enrollment intent, not by every unregistered message.
- **Glossary**: adds *Minimum Registration* and *Full Registration* as distinct, referenced terms.

### v1.0 — 2026-08-12
Initial baseline requirements document.

## Introduction

Q&M Dental Group runs its **2-Day Basic Certificate in Dental Assisting** enquiry → enrollment → payment journey entirely through a single WhatsApp conversation. Today this depends on staff manually answering course questions, processing enrollments, and eyeballing payment screenshots — work that is repetitive, slow outside office hours, and error-prone at volume. This system replaces that manual handling with an agentic AI backend (Python CrewAI) fronted by a WhatsApp gateway, implementing the architecture defined in the project proposal `QM_AI_Powered_Enquiry_and_Enrollment_System_V2_4.pdf`.

The system deliberately splits work between LLM agents (for open-ended, free-text conversation) and deterministic code (for anything that must never be wrong — critically, payment settlement, which is triggered by an image attachment rather than any command syntax). This document captures the requirements as implemented, to serve as the baseline specification for the spec-driven development process going forward.

### Feature Summary
A WhatsApp-based agentic system that lets a participant enquire about dental training courses, enrol in a specific intake, and get their payment verified and receipted — entirely through natural conversation, with human staff looped in only where automation is not confident.

### Business Value
- Removes manual, repetitive first-line handling of course enquiries, enrollments, and payment screenshot checks from Q&M staff.
- Available 24/7 through a channel participants already use (WhatsApp), lowering the barrier to enquire and enrol.
- Reduces payment-processing turnaround from "next business day" to near-instant for the majority of confidently-matched payments, while still routing anything ambiguous to a human.
- Produces an auditable trail (chat memory, staff queue, payments, credit notes) for every automated decision.

### Scope
**In scope:**
- Inbound/outbound WhatsApp messaging via a Node.js gateway (`whatsapp-server/`).
- Minimum participant registration at first contact (`whatsapp_id`), with progressive collection of name, NRIC, email, and confirmed phone deferred until the participant expresses enrollment intent.
- Free-text-only handling for course enquiries, lead capture, enrollment, invoicing, payment verification, and receipts (slash commands are removed — see Requirement 7).
- AI-assisted (vision) payment proof verification with human-staff fallback for low-confidence or mismatched cases.
- Staff escalation queue, admin dashboard, and a full-CRUD database administration UI.
- Scheduled lead follow-ups and a nightly accounts report.
- Cancellation and credit-note issuance.

**Out of scope (see README §12 for the authoritative list):**
- Direct Singpass/MySkillsFuture balance integration — the system links out to the official portal instead of querying it.
- Production-grade WhatsApp Business Cloud API integration — the current gateway (`whatsapp-web.js`) is explicitly a development/POC integration.
- Full EMVCo SGQR-compliant PayNow QR payloads on invoices — the current QR encodes a simplified reference.
- Any course other than the one currently seeded (2-Day Basic Certificate in Dental Assisting), though the schema supports a multi-course catalogue.

## Requirements

### Requirement 1: WhatsApp Message Gateway & Delivery

**User Story:** As a prospective or enrolled participant, I want to interact with Q&M Training entirely through my own WhatsApp app, so that I don't need to install a separate app or visit a web portal to enquire, enrol, and pay.

#### Acceptance Criteria

1. WHEN a participant sends a WhatsApp message (text, or an image with an optional caption) to the connected number THEN the System SHALL forward it as a JSON payload to the backend's `/webhook/whatsapp` endpoint.
2. WHEN the backend produces a reply for an inbound message THEN the System SHALL deliver it to the originating participant by calling the gateway's `/send-reply` endpoint, addressed by the participant's stable `whatsapp_id`.
3. IF the participant's WhatsApp account is a linked-identity (`@lid`) account THEN the System SHALL send replies to the `@lid` JID, not a phone-derived `@c.us` JID.
4. WHERE the backend configuration `WHATSAPP_ENABLED` is `false` THEN the System SHALL print the reply to the console instead of calling the gateway.
5. IF the inbound webhook payload has no resolvable `whatsapp_id` THEN the System SHALL drop the message without generating a reply or raising an unhandled error.

#### Additional Details
- **Priority**: High
- **Complexity**: Medium
- **Dependencies**: None (entry point for all other requirements)
- **Assumptions**: Each participant uses one personal WhatsApp account; the gateway (`whatsapp-server/`) is running and linked via QR scan before any message can be received.

---

### Requirement 2: Progressive Registration — Minimum Contact at First Message, Full Profile at Enrollment

**User Story:** As a prospective participant, I want to enquire about Q&M's courses using nothing more than my WhatsApp identity, and only be asked for my name, NRIC, email, and phone number at the point I actually want to enrol, so that browsing course information never feels like filling out a form first.

#### Acceptance Criteria

1. WHEN a new `whatsapp_id` sends its first message THEN the System SHALL create a `customers` record keyed by that `whatsapp_id` (its `@lid` or `@c.us` JID) as the participant's **minimum registration**, without requiring `full_name`, `nric`, `email`, or a confirmed phone number to be supplied first.
2. WHERE the account type is `@c.us` (a legacy WhatsApp account whose phone is always resolvable from the gateway payload) THEN the System SHALL auto-confirm the phone number on the customer record at first contact, opportunistically, without this being a precondition for answering enquiries.
3. WHEN a minimum-registered participant sends an enquiry (course, fee, schedule, or SkillsFuture question) THEN the System SHALL route and answer it normally (Requirement 4), without asking for `full_name`, `nric`, `email`, or phone.
4. WHEN a participant's message expresses enrollment intent (free text classified `ENROLLMENT` per Requirement 3) THEN the System SHALL check whether the customer record already has `phone_confirmed`, `full_name`, `nric`, and `email` all present.
5. IF all four fields are already present THEN the System SHALL proceed directly into the enrollment flow (Requirement 5) without asking the participant anything further.
6. IF any of the four fields are missing THEN the System SHALL reply with a free-text prompt asking only for the fields still missing, and SHALL NOT create the enrollment (or invoke Module B's enrollment tool) until they are supplied.
7. WHEN a customer's reply during this enrollment-triggered registration supplies some but not all missing fields THEN the System SHALL save the supplied fields immediately, so the participant is never asked to repeat information already given.
8. WHEN all four fields become present THEN the System SHALL mark the customer registered, confirm this to the participant, and resume the enrollment they originally asked for — without requiring them to restate their course/intake choice. That confirmation SHALL be brief and specific to their profile being registered (not their course), and SHALL NOT be padded with a generic capability menu or a "just ask me anything" filler when the resumed content immediately follows in the same reply — the resumed content itself is the answer to "what happens next", and repeating menu options (including the word "Enrol", easily confused with the resumed enrolment content sitting right below it) adds noise rather than clarity.
9. IF an extracted phone number is already linked to a different `whatsapp_id` THEN the System SHALL reject the registration attempt and ask the participant to confirm the correct number.
10. IF the participant's free-text reply (during enrollment-triggered registration) cannot be parsed for a full name by regex, and an OpenAI API key is configured THEN the System SHALL use a narrowly-scoped LLM call to extract only the name (never the other three fields) — the extracted result SHALL be validated against the same structural and provenance checks as the regex path (looks like a real name; every word of it actually appears in the participant's own message) before being accepted, SHALL be discarded (treated as no name found) if either check fails, and this validation applies regardless of whether the message actually names anyone.
11. IF a participant's free-text reply during registration is an ordinary conversational sentence with no name in it (e.g. "I just want to explore the course", "sorry, change my mind") THEN the System SHALL NOT extract and save any part of that sentence as `full_name`. The regex extraction path SHALL only ever extract a name when the message contains an explicit self-introduction cue ("my name is X", "I'm X", "call me X") — it SHALL NOT attempt to infer a name from an arbitrary sentence by elimination (stripping known filler words and accepting whatever remains), since no such filler list can ever enumerate every non-name phrasing a participant might use. A message without an explicit cue — including a bare name with no cue at all — SHALL fall through to the narrowly-scoped LLM extraction path (AC10) instead.
12. IF a participant's reply during registration contains a token that is shaped like an NRIC (a letter, seven further characters, then a letter) but does not satisfy the strict digits-only format THEN the System SHALL name that specific token and describe the correct format in its next prompt, rather than silently treating the reply as if it supplied nothing — and SHALL keep the registration prompt open for a corrected resend rather than abandoning the flow, even though no field was successfully captured that turn. This detection SHALL NOT fire for a token that is shaped correctly but contains no digits at all (an ordinary English word of the same letter-count coincidentally matching the shape, e.g. "MENTIONED") — a genuine near-miss NRIC always retains most of its digits even when mistyped, so requiring at least one digit present distinguishes a real attempt from an unrelated word.
13. The regex name-extraction path (AC11) SHALL only treat a phrase as a self-introduction cue when a name is the only thing that can grammatically follow it (e.g. "my name is", "call me") — it SHALL NOT treat a grammatically ambiguous phrase (e.g. "I am", "I'm", which as often precedes an ordinary statement as a name) as such a cue, since doing so reintroduces the AC11 filler-stripping weakness within the captured span. A message using an ambiguous phrase to state a real name SHALL still be captured correctly, via the LLM extraction path (AC10) rather than the regex path.

#### Additional Details
- **Priority**: High
- **Complexity**: Medium
- **Dependencies**: Requirement 1
- **Assumptions**: A phone number is a reliable-enough identity anchor to reject collisions; NRIC format follows Singapore's `[STFGM]nnnnnnnX` pattern; enrollment intent (Requirement 3) is always classified before Module B attempts to create an enrollment record, so the completeness check in AC4 always runs first.
- **Note**: the primary business purpose of allowing minimum registration is lead capture, not just a friendlier onboarding flow — a participant who enquires but never completes the full profile or a purchase is still captured as a lead (Requirement 4 AC7) and remains eligible for the scheduled follow-up reminder once they have an email on file (Requirement 10 AC2–AC3). Progressive registration is what makes that population of "enquired but never registered" leads exist and be reachable in the first place.

---

### Requirement 3: Intent Routing

**User Story:** As a participant, I want my message handled by the right specialist — enquiry, enrollment, or payment — always in plain English, so that I get a relevant answer without needing to learn any special syntax.

#### Acceptance Criteria

1. WHEN an inbound message is received THEN the System SHALL treat its text as free text regardless of whether it begins with `/` — the System SHALL NOT parse it against a command registry or apply any positional/pipe-delimited parameter parsing.
2. IF an inbound message carries an image attachment THEN the System SHALL route it to Module C directly, without invoking the Router Agent.
3. WHEN an inbound message is free text with no image THEN the System SHALL classify it into ENQUIRY, ENROLLMENT, or PAYMENT using the Router Agent (LLM), using only the participant's own last 3 messages plus a single fixed-vocabulary label describing the assistant's most recent action (never the assistant's raw reply text) as context — bounded to the participant's current conversation session (a new session begins after 60 minutes of inactivity), so an earlier, unrelated conversation is never read as still-relevant context.
4. IF `OPENAI_API_KEY` is not configured, or the Router Agent call raises an error THEN the System SHALL fall back to keyword-based heuristic classification (e.g. "enrol"/"signup" → ENROLLMENT, "paid"/"paynow" → PAYMENT, else ENQUIRY).
5. WHEN a short confirming reply (a bare number, an ordinal like "intake 2", or "yes") is received and the assistant's immediately preceding message offered an enrollment/intake choice THEN the System SHALL classify the reply as ENROLLMENT — and conversely, WHEN such a bare confirming reply has no enrollment signal of its own AND the assistant's immediately preceding message did NOT offer a specific course/intake (e.g. a generic "are you interested in our courses?" opener) THEN the System SHALL classify it as ENQUIRY, deterministically, regardless of what the Router Agent's own classification returns. The explicit-enrollment-signal keyword set SHALL recognise any inflection of "enrol"/"enroll" (e.g. "enrolling", "enrolled"), and SHALL also recognise "attend" (and its inflections) as stating enrolment intent.
6. WHEN a participant has only minimum registration (Requirement 2) THEN the System SHALL still route `ENQUIRY`-classified messages to Module A; only an `ENROLLMENT`-classified message triggers the profile-completion check in Requirement 2.
7. WHEN the assistant's immediately preceding message referenced reminder intent — the initial offer, a follow-up asking for the email, asking which course, or asking which intake (Requirement 4 AC9–AC14) — THEN the System SHALL classify the participant's reply as ENQUIRY regardless of the reply's own shape (an email address, an agree/decline word, a course name or position, a date, an ordinal, an SH-code, or anything else), deterministically, regardless of what the Router Agent's own classification returns — UNLESS the reply also carries an explicit enrollment signal of its own (AC5), or states genuine payment intent, in which case enrollment or payment intent still takes priority respectively. A message that is nothing but an email address SHALL NOT be treated as stating payment intent merely because its address happens to contain a payment-related word (e.g. "pay@company.com") — it continues to be treated as answering the reminder flow's own email-ask, consistent with the shape-independence of this rule; a payment claim combined with other text is unaffected by this exception. Three narrower, shape-specific versions of this rule were tried in turn (requiring an email/agree/decline-shaped reply; additionally requiring the preceding message to mention "intake"), and each still missed a real reply shape the others didn't anticipate — merged into this single, shape-independent rule instead. Recognising that a reminder exchange is pending SHALL consider the assistant's last TWO turns, not only the latest one — a multi-step exchange often splits the signal that it's about a reminder from the specific thing being asked across separate turns — and MUST distinguish this from Module B's own, superficially similar "which intake would you like to register for?" question (Requirement 5), which SHALL be routed and handled as Requirement 5 describes, not diverted here.
8. WHEN a participant's own new message states reminder intent (e.g. "can you remind me", "send me a reminder", or any other inflection of the word, such as "remindered"), REGARDLESS of whether the assistant's preceding message made a reminder offer of its own (distinct from AC7, which only covers a reply to an existing offer) THEN the System SHALL classify it as ENQUIRY, deterministically, regardless of what the Router Agent's own classification returns — UNLESS the same message also carries an explicit enrollment signal of its own (AC5), in which case enrollment intent takes priority.
9. (Merged into AC7, v1.22 — a reply to Module A's "which intake?" ask is now covered by AC7's single, shape-independent rule rather than a separate criterion.)
10. WHEN a module composes a reply that leaves a question specific to its own flow unresolved (Module A's reminder exchange still missing an email/course/intake, Requirement 4 AC9–AC15; Module B's enrollment still awaiting an intake selection or explicit confirmation, Requirement 5 AC2) THEN the System SHALL record which module and which flow is pending for that participant.
11. WHEN a participant's new message arrives AND a pending flow (AC10) is on record for them AND has not expired (30 minutes since last set) THEN the System SHALL dispatch that message directly to the module owning the pending flow, without invoking the Router Agent or the keyword heuristic — UNLESS the message carries an explicit, unambiguous signal for a different module (an enrollment keyword/course code per AC5, or a payment keyword), in which case it SHALL be classified normally per AC3–AC9 instead. A course or schedule code SHALL NOT by itself count as such a signal when it merely restates the course/schedule already recorded as part of the pending flow itself (e.g. repeating the course code a reminder or confirmation is already about) — only a code naming a course genuinely different from the one already pending signals a switch.
12. WHEN a module's reply resolves its own pending flow THEN the System SHALL clear the recorded pending state for that participant. A module SHALL only clear pending state it itself owns — never a flow recorded by a different module.
13. The Router Agent's classification SHALL include a confidence level. WHEN confidence is low AND none of AC5, AC7, AC8, AC10–AC12's deterministic signals resolve the message THEN the System SHALL reply with a short disambiguation menu instead of dispatching to a module, and SHALL record this as its own pending flow (AC10) so the participant's next reply is matched against the menu rather than reclassified from scratch; an unparseable reply to the menu SHALL fall through to normal classification rather than repeating the menu indefinitely.
14. WHEN Module C determines a message routed to it is not actually about payment THEN the System SHALL redirect it to the correct module and produce a real answer from that module, capped at one redirect per message — if the redirected module also cannot handle it, the System SHALL fall back to a generic error reply rather than redirecting again.

#### Additional Details
- **Priority**: High
- **Complexity**: Medium
- **Dependencies**: Requirements 1, 2 (minimum registration only — routing to Module A never requires the full profile)
- **Assumptions**: A message that happens to begin with `/` may not be understood the way a participant intends, since it now receives no special interpretation — this is an accepted tradeoff of removing commands (Requirement 7), not a bug. AC10–AC12's pending-state dispatch is a structural replacement for the ROUTING MECHANISM only — the underlying keyword-based classification (AC1–AC9) remains as the fallback for a participant not currently mid-flow, and is unchanged by this revision.

---

### Requirement 4: Module A — Course Enquiry & Lead Capture

**User Story:** As a prospective participant, I want to ask about courses, fees, schedules, and SkillsFuture funding in my own words, so that I can decide whether to enrol without waiting for a staff reply.

#### Acceptance Criteria

1. WHEN a participant asks about course offerings, fees, intake schedules, or SkillsFuture eligibility THEN the System SHALL answer using only the FAQ knowledge base and the course-data tools, carrying over figures, dates, and codes exactly as the tool returned them.
2. IF the participant volunteers contact or preference details (name, NRIC, email, preferred course, intake date) at any point in the conversation THEN the System SHALL persist the supplied fields via the lead-capture tool. A supplied preferred course SHALL be resolved against the actual course catalogue (C-code, catalogue position — including a "course N" phrasing — or name) before being persisted; a value that does not resolve to a real, currently-offered course SHALL NOT be persisted. WHILE a reminder (AC9–AC15) is still pending for a specific course THEN an incidental course-information lookup for a different course arising in the same conversation SHALL NOT overwrite the course already recorded as that pending reminder's subject.
3. IF Module A cannot confidently answer a query, or the query is sensitive or out of scope THEN the System SHALL create a staff-queue escalation and tell the participant a colleague will follow up.
4. WHEN a participant asks about their own enrollment or payment status THEN the System SHALL NOT infer or fabricate an answer from conversation history, and SHALL direct them to ask about their enrollment status directly (Module B, Requirement 5 AC6) instead.
5. WHEN a participant asks a follow-up that depends on an earlier turn (e.g. "what about the fees?" after a course was named earlier) THEN the System SHALL resolve the missing reference from chat history before calling a tool, rather than asking the participant to repeat themselves.
6. WHERE `OPENAI_API_KEY` is not configured THEN the System SHALL reply with a canned fallback message rather than attempting to answer precisely, since no deterministic command path remains to substitute for the agent.
7. WHEN any enquiry is received THEN the System SHALL update the lead/customer record for that `whatsapp_id` with whatever information the interaction yields — regardless of whether the participant is minimum-registered or fully registered, and regardless of whether contact details were explicitly volunteered (AC2) — so no enquiry activity goes unrecorded.
8. WHEN a participant's message contains an explicit, unambiguous course reference (a C-code, or a stated catalogue position such as "course 4") THEN the System SHALL persist that course as the customer's preferred course, regardless of which module ultimately handles the message and regardless of whether a course-information tool happens to be invoked that turn; the same explicit reference SHALL also be resolved to that exact course when composing the reply for THIS same turn, not to whichever course was most recently discussed in the conversation, and any deterministic update this triggers to the customer's preferred course SHALL be visible to this same turn's processing (not only to turns after it).
9. WHEN a conversation with a participant who has not committed to enrolling appears to be winding down (hesitation, deferral, or a closing remark rather than a further question) THEN the System SHALL, at most once per conversation, summarise the course(s) discussed and offer to save the participant's email for a future reminder, and SHALL persist any email given in response via the lead-capture tool.
10. IF a participant agrees to the reminder offer (AC9) but their reply does not contain an actual email address THEN the System SHALL NOT call the lead-capture tool and SHALL NOT state or imply that an email or reminder has been saved — agreeing to the idea of a reminder is not the same as providing the email itself. It SHALL instead ask the participant for their email address, and SHALL only save it once an actual address is given in a subsequent reply — the same standard already applied to progressive registration (Requirement 2) and enrollment confirmation (Requirement 5 AC2–AC3), which likewise never treat an agreement alone as if the underlying information had also been supplied.
11. WHEN the assistant's immediately preceding message was the reminder offer (AC9) or its follow-up request for an email (AC10) THEN the System SHALL route the participant's reply — an email address, or a short agree/decline reply — back to the same module that made the offer, regardless of what a general-purpose classifier would otherwise conclude from the reply's content alone (e.g. an email address resembling something relevant to a different module). Recognising that the preceding message was such an offer/request SHALL NOT depend on the conversational agent reproducing an exact required sentence verbatim, on the relevant wording appearing within a fixed distance of other wording, or on it appearing within a single sentence — its own free-text paraphrasing of the same request, however structured across the message, SHALL still be recognised as the same state.
12. Module A SHALL NEVER state or imply, in any reply, that a participant has been enrolled in a course, that an enrollment is confirmed, or that an invoice has been or will be sent — regardless of what its own conversational reasoning concludes — unless a real enrollment record was actually created during that same turn (whether directly or via delegation to Module B). This SHALL be enforced structurally (the System itself detecting and replacing any such claim absent a genuine enrollment), not solely by prompt instruction, since instructing the agent not to fabricate this has been observed to fail on its own — `Save Lead Data` (lead capture) is not evidence an enrollment occurred and SHALL NOT be treated as satisfying this condition. Verifying genuineness SHALL be based on the participant's actual persisted enrollment record (e.g. comparing a fresh database read of it against what it was at the start of the turn), not on any per-turn in-process flag set from inside a tool call — such a flag was found, live, to not reliably reflect a real success reached via delegation, and incorrectly blocking a genuine confirmation is a more severe failure than the fabrication this criterion exists to prevent.
13. WHEN a participant's reminder is being saved (AC9–AC11) against a course that has more than one upcoming intake, and no specific intake has been resolved from the conversation THEN the System SHALL NOT state or imply that the reminder is fully saved — it SHALL still persist whatever is already known (so nothing already given is lost) and ask the participant which intake they'd like the reminder for, mirroring Requirement 5 AC5's equivalent rule for enrollment; the System SHALL NOT select or persist an assumed or default intake in place of asking, and SHALL NOT state or imply that a specific date was saved unless the participant actually specified one. WHEN the participant subsequently names or selects a specific intake THEN the System SHALL persist it and only then confirm the reminder as complete; matching the participant's reply to one of the course's actual intakes SHALL prefer the more specific match when multiple intakes share a common element (e.g. the same year), rather than treating any shared element as making the match ambiguous. Recognising that a reminder exchange is still in progress, for the purpose of this criterion and AC11, SHALL NOT require the word "email" to also be present in the recent conversation — a reminder already past the email step (e.g. a second reminder request in the same conversation, where the email is already on file) has no reason to mention it again.
14. A participant SHALL be able to have more than one reminder on record at the same time, one per distinct course, WITHOUT saving a reminder for a different course overwriting a previously-saved one for an earlier course. The System SHALL NOT record the same course and intake date combination more than once for the same participant (no duplicates on re-affirmation). A recorded reminder's course and intake date SHALL always correspond to an actual, currently-scheduled intake of that specific course — the System SHALL detect and correct a recorded combination that does not (whether from an invented date, or a real date belonging to a different course than the one it was paired with), rather than leaving an invalid combination on record. Naming only a course (with no date) SHALL NOT carry over a previously-recorded intake date from a different course as if it belonged to the new one.
15. WHEN a reminder is being set up (AC9–AC14) and the recent conversation has discussed more than one course, THEN the System SHALL NOT infer, recall, or default to any one of them — it SHALL explicitly ask the participant to state which course (and, per AC13, which intake) the reminder is for, and SHALL NOT persist a course/date combination, or tell the participant a reminder is saved, until the participant has done so. This applies identically regardless of whether the reminder exchange was started by the System's own offer (AC9) or by the participant asking for a reminder unprompted, mid-conversation. WHILE any required piece (email, course, or intake) is still missing, the System SHALL still let the participant ask about other things in the same conversation without being forced to resolve the reminder first — an incomplete reminder SHALL simply stay unresolved rather than being repeatedly re-asked. Before reverting or deleting a participant's recorded course/date or an already-persisted reminder on the grounds that it looks ambiguous, the System SHALL first confirm against the actual persisted `reminders` record that the course+date in question was not already genuinely saved before the current turn began — an already-completed reminder SHALL NOT be reverted or deleted merely because a later, unrelated message doesn't name a course.

#### Additional Details
- **Priority**: High
- **Complexity**: Medium
- **Dependencies**: Requirements 2 (minimum registration only — Module A never requires the full profile), 3
- **Assumptions**: The FAQ knowledge base is kept current by staff; the agent never needs to answer from outside it. AC9's "conversation winding down" judgement is LLM-based, not deterministic — the trigger may fire earlier or later than ideal, or occasionally not at all in a given conversation, which is an accepted tradeoff for keeping it conversational rather than a rigid rule (unlike AC8, which is deterministic since it directly affects saved data, not just reply timing). AC13's "ask which intake" instruction is likewise prompt-guided and not always followed to the letter in a single compound reply — a deterministic backstop re-checks the actual saved state afterward and appends the question itself if it's still outstanding, so the requirement holds even when the agent's own phrasing doesn't fully comply. AC14's correctness check is likewise a deterministic backstop, not a prompt instruction alone — the agent was observed, live, pairing a real intake date with the wrong course in its own tool call despite an explicit directive naming the correct one. AC15's "don't guess, ask" rule was found to need the same deterministic-backstop treatment as AC13/AC14 in two of its stages (not just a prompt directive) — see `task_qm.md` v1.25 for exactly where and why.

---

### Requirement 5: Module B — Enrollment, Invoice & Status Pipeline

**User Story:** As a registered participant, I want to confirm the exact course and intake I'm choosing before it's finalised, and then receive my invoice automatically, so that I never end up enrolled in something I didn't mean to pick, without manual back-and-forth with staff.

#### Acceptance Criteria

1. WHEN a participant sends any enrollment-intent message THEN the System SHALL first perform the registration-completeness check (Requirement 2, AC4); IF the profile is incomplete THEN the System SHALL defer to the profile-completion prompt instead of proceeding with any of the criteria below, and SHALL resume the original enrollment request automatically once the profile completes.
2. WHEN a participant's free-text conversational message resolves to a specific course and a specific intake (by SH-code, or by confirming a previously shown intake list) THEN the System SHALL summarise the resolved course name and intake date back to the participant and ask them to confirm, and SHALL NOT create the enrollment record until the participant agrees — this applies identically the first time a course+intake resolves and every subsequent time (e.g. after the participant declined a prior summary and then named or selected a different intake instead): a fresh confirmation is always required before enrolling, never assumed from confidence alone.
3. WHEN the participant confirms the summarised course and intake (e.g. "yes", "go ahead", "confirm") THEN the System SHALL validate the enrollment fields and, if valid, create the enrollment record, generate an invoice PDF, and email it to the participant — matching exactly the course and intake that were summarised.
4. IF the participant declines or disagrees with the summarised course/intake (e.g. "no", "not that one", "actually a different date") THEN the System SHALL NOT create the enrollment, SHALL acknowledge the participant's response without repeating the same summary, and SHALL continue the conversation normally (e.g. able to answer further questions or restart course/intake selection) rather than treating it as an error or ending the interaction.
5. IF a participant has named a course but not a specific intake in conversational free text THEN the System SHALL NOT create an enrollment, and SHALL instead present the available intake options and ask the participant to choose one — this precedes the confirmation step in AC2, since a specific intake must be resolved before it can be summarised.
5a. IF a participant named a specific date or intake (anywhere in the message that triggered enrollment intent, including one made before registration completed) that does not match any of the course's actual scheduled intakes THEN the System SHALL explicitly tell the participant that date/intake is not available for this course before presenting the real options — SHALL NOT silently disregard what they asked for and re-prompt as if nothing had been said.
6. WHEN a participant asks for their enrollment status THEN the System SHALL report either the single latest enrollment (for a general "my status" ask) or every enrollment on record (for "all my courses"), matching what was asked; WHEN reporting more than one enrollment THEN the System SHALL use a fixed, numbered format (one entry per enrollment, each carrying its own invoice number) so a participant can reliably refer back to a specific entry by its position later in the conversation.
7. WHEN a participant asks to resend their invoice THEN the System SHALL resend the latest invoice email unchanged, without regenerating a new invoice number.
8. IF a course code and a schedule code both appear together in one free-text message THEN the System SHALL enrol the participant directly from the regex-matched codes, without invoking the LLM agent for that turn and without the confirmation step in AC2–AC4 — the sole fast-path exemption, since matching both exact codes together is itself an unambiguous instruction — PROVIDED the registration-completeness check in AC1 has already passed.
9. IF required fields (name, NRIC, email) are omitted from an enrollment request THEN the System SHALL fill them from the participant's registered profile automatically.
10. IF a participant declined a confirmation summary (AC4) and their next message is a short generic reply (e.g. "ok sure", "yes") with no course/intake reference of its own THEN the System SHALL NOT reinterpret it as re-confirming or re-selecting a course/intake from earlier conversation history — it SHALL instead be treated as a reply to whatever the assistant's own immediately preceding message actually asked (e.g. an offer to record their interest for a follow-up reminder).
11. IF Module B offers to record the participant's interest or email for a follow-up reminder after a decline (permitted, not required, under AC4) THEN it SHALL only do so if it can actually fulfil that offer — by delegating the save action to Module A (Requirement 12), which holds the lead-capture tool — and SHALL NOT claim to have saved anything it has not actually saved.
12. IF the course being resolved for the confirmation summary (AC2) differs from a course the participant explicitly referenced earlier in the same conversation, more recently than any other explicit course reference THEN the System SHALL name both courses in that summary and invite the participant to confirm the switch is intentional, rather than presenting only the newly-resolved course as though no discrepancy exists — this does not block or delay the confirmation step, and does not require the System to determine which course the participant actually intended.

#### Additional Details
- **Priority**: High
- **Complexity**: High
- **Dependencies**: Requirements 2 (full-profile completeness check, triggered here), 3, 4 (course data)
- **Assumptions**: Every enrollment is for exactly one course and one intake; seat capacity is tracked per intake (`course_schedules`); a participant cannot reach status/invoice/payment enquiries (Requirements 5 AC6–7, 6) without having completed full registration, since no enrollment can exist otherwise. The confirmation step (AC2–AC4) applies to every conversational enrollment now that the `/enroll` command no longer exists — the C-code+SH-code quick-enrol shortcut (AC8) is the only remaining exemption, since matching both exact codes together is itself an unambiguous instruction.

---

### Requirement 6: Module C — Payment Verification & Receipts

**User Story:** As a participant who has paid for a course, I want to submit a screenshot of my payment and receive automatic confirmation and a receipt, so that I don't have to wait for a staff member to manually check it.

#### Acceptance Criteria

1. WHEN a message containing an image is received against an outstanding invoice THEN the System SHALL extract the proof type, amount, reference, and confidence from the image using a vision model, before any conversational agent is invoked for that turn.
2. IF the extracted proof is a recognised type (PayNow transfer or SkillsFuture claim), its amount does not exceed the outstanding balance for that proof type (within a small rounding tolerance), and its confidence meets the configured auto-confirm threshold THEN the System SHALL record the payment as confirmed, generate a receipt PDF, and email it to the participant.
3. IF the extracted amount, proof type, or confidence fails the confirmation checks THEN the System SHALL record the payment as a mismatch, create a staff-queue task, notify the accounts mailbox by email, and tell the participant a human will review it.
4. IF the screenshot is unreadable or not a recognisable payment/claim proof THEN the System SHALL ask the participant to resend a clearer screenshot, without creating a staff escalation.
5. WHEN every proof required for an invoice (SkillsFuture claim and/or PayNow, depending on the course's subsidy split) has been confirmed and the confirmed amounts sum to the full course fee THEN the System SHALL mark the enrollment `receipt_issued`.
6. IF a confirmed payment leaves an outstanding remainder THEN the System SHALL tell the participant exactly which proof type(s) and amount(s) are still needed.
7. WHEN a participant asks what they still owe THEN the System SHALL report the itemised outstanding balance per proof type rather than a single estimated figure.
8. WHEN a participant asks for a receipt THEN the System SHALL resend every confirmed-payment receipt for their latest enrollment, since an invoice may have more than one.
9. IF a participant does not state or previously mention an invoice number when submitting proof THEN the System SHALL fall back to their own latest outstanding invoice rather than rejecting the submission.
10. IF a participant has more than one enrollment (including two intakes of the same course) and refers to a specific one by list position (e.g. "item 2") or by course name plus intake date, rather than stating the invoice number directly, THEN the System SHALL resolve the reference to that exact enrollment's invoice — never to whichever enrollment a lookup would default to (e.g. the most recently created one) — for both outstanding-balance questions and payment submission.

#### Additional Details
- **Priority**: High
- **Complexity**: High
- **Dependencies**: Requirements 2, 3, 5
- **Assumptions**: WhatsApp delivers at most one image per message, so an invoice needing two proof types is settled across two separate messages; the vision model is available and correctly configured.
- **Note**: this requirement was never command-syntax-dependent — AC1's trigger is an image attachment, not `/pay` text — so it is entirely unaffected by Requirement 7's removal of slash commands.

---

### Requirement 7: Free-Text-Only Interaction (Slash Commands Removed)

**User Story:** As a participant, I want to talk to the system entirely in my own words, with no special syntax to learn or remember, so that texting it feels exactly like texting a person.

#### Acceptance Criteria

1. WHEN any inbound message is received THEN the System SHALL treat it as free text and route it through the Intent Router's classification (Requirement 3) — the System SHALL NOT match it against a command registry, and SHALL NOT apply positional or pipe-delimited parameter parsing to it, regardless of its content.
2. IF a message begins with `/` THEN the System SHALL NOT special-case it in any way — it SHALL be classified and handled exactly like any other free-text message. The System MAY fail to usefully interpret it if its content doesn't resemble a natural request; no guaranteed interpretation is required.
3. WHEN a module's free-text (agentic) path and any other internal caller both need the same fact or action (e.g. course fees, enrollment) THEN the System SHALL call the identical underlying service function for both, so results can never diverge.
4. WHERE a request omits an optional field that has a value on the participant's registered profile THEN the System SHALL fill it from that profile automatically.
5. WHERE `OPENAI_API_KEY` is not configured THEN the System SHALL reply with each module's existing canned fallback message (Requirements 4, 5, 6) rather than executing any request precisely, since no deterministic command path exists to substitute for the agent — this is an accepted reliability tradeoff (NFR Reliability), not a defect.

#### Additional Details
- **Priority**: High
- **Complexity**: Low
- **Dependencies**: Requirements 2, 4, 5, 6
- **Assumptions**: `OPENAI_API_KEY` is effectively required for the system to be practically useful; the canned fallback replies exist only to avoid total silence when it's unset or failing, not to preserve functional parity with the agentic path. The C-code+SH-code quick-enrol shortcut (Requirement 5 AC8) and the image-triggered payment path (Requirement 6 AC1) are unaffected by this requirement — neither was ever command-syntax-dependent.

---

### Requirement 8: Staff Escalation & Admin Oversight

**User Story:** As Q&M Training staff, I want visibility into every enquiry or payment the system could not resolve automatically, so that no participant request is silently dropped.

#### Acceptance Criteria

1. WHEN Module A cannot confidently answer an enquiry, or Module C detects a payment mismatch THEN the System SHALL create a `staff_queue` entry recording the reason, the originating module, and the participant.
2. WHEN a staff-queue entry is created for a payment mismatch THEN the System SHALL notify the accounts mailbox by email with the detected amount, proof type, and confidence.
3. THE System SHALL provide an authenticated admin dashboard listing enrollments, leads, open staff-queue items, and credit notes.

#### Additional Details
- **Priority**: Medium
- **Complexity**: Low
- **Dependencies**: Requirements 4, 6
- **Assumptions**: Staff check the dashboard and mailboxes periodically rather than requiring real-time paging.

---

### Requirement 9: Database Administration UI

**User Story:** As a system administrator, I want to browse and edit every table directly from a browser, so that I can correct data issues without a separate database client.

#### Acceptance Criteria

1. THE System SHALL provide an authenticated web UI that lists, paginates (25/50/100/250 rows), creates, edits, and deletes rows in every table defined in the schema.
2. WHEN a row is opened for editing THEN the System SHALL disable read-only fields (`id`, `created_at`, `updated_at`, `resolved_at`) and auto-detect an appropriate input control per column (number, date, checkbox, JSON textarea).
3. WHEN a delete action is requested THEN the System SHALL require an explicit confirmation before removing the row.
4. IF a request to the admin dashboard or database admin UI does not include valid HTTP Basic Authentication credentials THEN the System SHALL reject the request before returning any data.

#### Additional Details
- **Priority**: Medium
- **Complexity**: Medium
- **Dependencies**: None beyond the schema itself
- **Assumptions**: Access is restricted to trusted internal staff; this UI operates directly on live data with no separate staging copy.

---

### Requirement 10: Scheduled Follow-ups & Reporting

**User Story:** As Q&M Training staff, I want the system to nudge unresponsive leads and produce a daily accounts summary automatically, so that opportunities aren't lost and accounts has a routine reconciliation record.

#### Acceptance Criteria

1. WHERE `SCHEDULER_ENABLED` is `true` THEN the System SHALL run a lead follow-up check once daily at the configured hour, covering every lead/customer record regardless of whether they hold minimum or full registration (Requirement 2).
2. IF a lead/customer record has an email address on file THEN the System SHALL send that lead a follow-up reminder.
3. IF a lead/customer record has no email address on file THEN the System SHALL skip that lead for this follow-up run — no reminder is sent, and this is not treated as an error.
4. WHERE `SCHEDULER_ENABLED` is `true` THEN the System SHALL generate a CSV summary of enrollments by status and email it to the accounts mailbox once daily at the configured hour.
5. WHERE `SCHEDULER_ENABLED` is `true` THEN the System SHALL, once daily at a configured hour, email every saved reminder (Requirement 4 AC9–AC14) whose course intake date falls within a 3-day lead time and that has not already been sent, and SHALL mark it sent so it is never emailed twice.

#### Additional Details
- **Priority**: Low
- **Complexity**: Low
- **Dependencies**: Requirements 4 (lead capture/update on every enquiry), 5, 6 (data to summarise)
- **Assumptions**: The backend process runs continuously (or is restarted daily) so the scheduler can fire at its configured hour; an email address is required to determine follow-up eligibility even though many leads now exist with only minimum registration.

---

### Requirement 11: Cancellation & Credit Notes

**User Story:** As Q&M Training staff, I want to cancel an enrollment and issue a credit note when a participant withdraws, so that refunds are tracked formally and the released seat becomes available to others.

#### Acceptance Criteria

1. WHEN a credit note is requested for an active enrollment THEN the System SHALL total the confirmed payments made to date, create a credit note record, and mark the enrollment `cancelled`.
2. IF the cancelled enrollment held a seat on a course intake THEN the System SHALL release that seat back to the intake's available capacity.
3. WHEN a credit note is approved THEN the System SHALL generate a credit note PDF and email it to the participant — this generation/delivery SHALL happen via a short-interval background dispatch, not synchronously inside the staff approval action, so the approval itself confirms immediately regardless of PDF/email latency.
4. IF a credit note is requested for an enrollment already marked `cancelled` THEN the System SHALL reject the request rather than issuing a duplicate credit note.
5. A registered participant SHALL be able to request cancellation of their own enrollment conversationally, resolving which enrollment from a stated invoice number or their sole/latest active enrollment. The System SHALL summarise what will be cancelled and require the participant's explicit confirmation in a subsequent reply before actually cancelling — the same standard already required before creating an enrollment (Requirement 5 AC2–AC4).

#### Additional Details
- **Priority**: Medium
- **Complexity**: Medium
- **Dependencies**: Requirements 5, 6
- **Assumptions**: Credit note approval is a manual staff action taken outside the WhatsApp conversation (via the admin/accountant portal). AC5's conversational cancellation is participant-initiated; approval of the resulting credit note remains the staff action described in AC1–AC3.

---

### Requirement 12: Agent-to-Agent Delegation (Module A ↔ Module B)

**User Story:** As a participant, I want a single message that touches more than one topic (e.g. a course question and my own enrollment status) answered completely in one reply, so that I don't have to split my question across separate messages just because the system's specialists don't talk to each other.

#### Acceptance Criteria

1. WHEN the Router classifies a turn as ENQUIRY and the participant's message also needs something only Module B's tools can answer (their own enrollment status/invoice, or an enrollment action) THEN the System SHALL let Module A delegate that part of the task to Module B's agent and compose one combined reply, PROVIDED the registration-completeness check (Requirement 2 AC4) already passes for this participant — otherwise Module A SHALL redirect them to ask directly (Requirement 4 AC9), exactly as if delegation did not exist.
2. WHEN the Router classifies a turn as ENROLLMENT and the participant's message also needs general course/fee/schedule/SkillsFuture knowledge Module B has no tool for THEN the System SHALL let Module B delegate that part to Module A's agent and compose one combined reply — this direction requires no registration-completeness gating, since Module A never creates or modifies state.
3. IF a participant is not fully registered THEN the System SHALL NOT make Module B's agent available to Module A as a delegation target under any circumstances — this SHALL be enforced by Module B's agent being absent from the set of agents Module A's turn is even constructed with, not by prompt instructions alone.
4. WHEN Module B's agent is delegated to (whether as the turn's own entry point or as a coworker) THEN it SHALL apply the identical tool-gating already required by Requirement 5 AC2–AC3 (the `Enroll Participant` tool present only on a turn that is a clean agreement to a confirmation it just sent) — computed by the same function regardless of which path invoked it.
5. WHEN a delegation hand-off occurs THEN the System SHALL record it in the query trace log, including which agent delegated to which and the task/context handed off, so the behaviour is diagnosable the same way tool calls already are.

#### Additional Details
- **Priority**: Medium
- **Complexity**: High
- **Dependencies**: Requirements 2 (registration gate), 4, 5 (the two agents being delegated between)
- **Assumptions**: Module C (payment) is explicitly out of scope for this requirement — no delegation to/from it yet, a deliberately scoped-down first pass. Delegation is additive to the existing Router-based primary routing (Requirement 3), never a replacement for it. Whether delegation actually fires for a given compound message is LLM judgement, not deterministic — the two properties that must hold regardless (registration gating, Enroll Participant tool-gating) are the only parts enforced structurally.

---

## Non-Functional Requirements

### Performance Requirements
- WHEN a participant sends a message THEN the System SHALL forward it to the backend and return a reply within the gateway's configured forward timeout (120 seconds by default) before surfacing a delivery failure.
- IF the OpenAI API is slow or the key is invalid THEN the System SHALL still eventually respond via its canned/heuristic fallback rather than leaving the participant without any reply (documented known limitation: an invalid key degrades gracefully but with slower retries — see README §12).

### Security Requirements
- WHEN a request is made to the admin dashboard or the database administration UI THEN the System SHALL require valid HTTP Basic Authentication before returning any data.
- IF a CrewAI tool call includes an LLM-supplied argument for identity (`whatsapp_id`, phone) or binary media THEN the System SHALL ignore it and use only the value held in server-side, per-request context — never a value the model filled in.
- THE System SHALL scope every customer-data query by `whatsapp_id` so no request can return another participant's records.

### Usability Requirements
- WHEN a participant is prompted to complete their profile (triggered by enrollment intent, per Requirement 2) THEN the System SHALL ask for missing information in plain conversational English, never in command/pipe syntax.
- WHEN a minimum-registered participant sends an enquiry THEN the System SHALL answer it without first requesting name, NRIC, email, or phone.
- WHEN an agent composes a reply from a tool's raw output THEN the System SHALL rewrite it as natural, concise conversational text while preserving every code, date, and amount exactly as returned.
- IF a participant's request is genuinely ambiguous after checking conversation history THEN the System SHALL ask one short clarifying question rather than guessing.

### Reliability Requirements
- IF `OPENAI_API_KEY` is unset, or a CrewAI agent call raises an exception THEN the System SHALL fall back to a canned reply or keyword heuristic (Requirement 3 AC4) rather than returning an error to the participant — this no longer includes precise deterministic command execution, which has been removed (Requirement 7); the participant gets a safe, non-error reply, not necessarily a useful one.
- IF the webhook payload is malformed or missing a `whatsapp_id` THEN the System SHALL log and drop the message safely, without raising an unhandled exception back to the gateway.
- IF writing the chat-memory record for a turn fails THEN the System SHALL still deliver the generated reply to the participant (memory persistence is best-effort and non-blocking).

## Constraints and Assumptions

### Technical Constraints
- The backend requires Python `>=3.10,<3.14` (a CrewAI constraint); Python 3.14 is explicitly unsupported.
- The WhatsApp gateway (`whatsapp-web.js`) requires a real local Chrome/Chromium executable and is a development/POC-grade integration, not the official Meta WhatsApp Business Cloud API.
- All persistent state lives in a single PostgreSQL 16 instance; no other datastore is used.
- Vision-based payment verification depends on an OpenAI vision-capable model (`gpt-4o` by default) being reachable and correctly configured.
- The gateway and backend are decoupled solely by the `/webhook/whatsapp` and `/send-reply` HTTP contract, so the gateway can be replaced (e.g. with the Cloud API) without changing the backend.

### Business Constraints
- SkillsFuture balance is not verified via Singpass; the system relies on the participant's own claim screenshot and links out to the official MySkillsFuture portal for anything it cannot itself confirm.
- PayNow QR codes on invoices encode a simplified payment reference for the POC, not a full EMVCo SGQR payload.
- Only one course (2-Day Basic Certificate in Dental Assisting) is seeded at present, though the schema and agents support a multi-course catalogue.

### Assumptions
- Each participant interacts through a single personal WhatsApp account mapped to one `whatsapp_id`.
- WhatsApp delivers at most one image per message; the system never assumes a multi-image batch.
- Staff monitor the admin dashboard and the staff/accounts mailboxes periodically rather than requiring real-time alerting.
- A phone number is a sufficiently reliable identity anchor that a collision (the same number claimed by two `whatsapp_id`s) should block registration rather than merge accounts.
- A `whatsapp_id` alone (minimum registration) is sufficient to treat a participant as a lead worth recording and re-engaging, even before they ever supply an email or complete full registration.

## Success Criteria

### Definition of Done
- [ ] All acceptance criteria in Requirements 1–11 are implemented and demonstrable through both the WhatsApp gateway and the `/webhook/whatsapp/sync` test endpoint.
- [ ] Every inbound message, including one beginning with `/`, is routed through free-text classification with no command-registry parsing (Requirement 7).
- [ ] With `OPENAI_API_KEY` unset, the system degrades to canned fallback replies rather than erroring (Non-Functional: Reliability) — precise command execution is no longer part of that guarantee, by design.
- [ ] No customer-scoped query can be shown to return another participant's data (Non-Functional: Security).
- [ ] The admin dashboard and database admin UI both require authentication and are exercised against the live schema.
- [ ] The status state machine (`enquiry → enrolled → invoice_sent → awaiting_payment → paid → receipt_issued`, with a `cancelled` branch) is fully reachable and observable via `/dbadmin/`.
- [ ] A minimum-registered participant (`whatsapp_id` only) can complete course/fee/schedule/SkillsFuture enquiries and other free-text ENQUIRY turns end-to-end without ever being asked for name, NRIC, email, or phone, until they express enrollment intent.

### Acceptance Metrics
- 0 instances of command-registry parsing remaining anywhere in the routing path — 100% of inbound messages, regardless of a leading `/`, are routed through free-text classification (Requirement 7 AC1).
- 100% of payment screenshots that are confirmed produce a receipt PDF delivered by email within the same request/response cycle.
- 0 instances of a tool call using an LLM-supplied `whatsapp_id`/phone/image argument instead of server-side context (verifiable by code inspection of `tools/*.py`).
- 0 unhandled exceptions returned to the WhatsApp gateway for malformed inbound payloads.
- 100% of enquiries (registered or minimum-registered) result in an updated lead/customer record.
- 0 follow-up reminders attempted against a lead/customer record with no email on file.

## Glossary

| Term | Definition |
|------|------------|
| Agent | A CrewAI `Agent` instance (role, goal, backstory, tools) built through `kickoff_agent()`; the LLM-reasoning unit for free-text handling. |
| C-code | The course catalogue's short identifier (e.g. `C2601`) used in tool output and the C-code+SH-code free-text quick-enrol shortcut. |
| Conversation Session (Episode) | A burst of activity by one participant, bounded by a 60-minute gap of inactivity (`chat_memory.session_id`); scopes what counts as "recent" for the Router Agent's classification context (Requirement 3, AC3) — distinct from a pending flow's own 30-minute expiry (Requirement 3, AC11). |
| Deterministic path | Any code path that produces a response without invoking an LLM — now limited to the image-triggered payment-settlement flow (Requirement 6) and the C-code+SH-code quick-enrol shortcut (Requirement 5 AC8); slash commands no longer exist as a deterministic path (Requirement 7). |
| EARS | Easy Approach to Requirements Syntax — the WHEN/IF/WHILE/WHERE... SHALL phrasing used for acceptance criteria in this document. |
| Full Registration | A customer record with `phone_confirmed`, `full_name`, `nric`, and `email` all present; required before an enrollment can be created (Requirement 2, AC4–8). |
| Intent Router | The component (`crews/router.py`) that decides whether an inbound message is handled deterministically or classified into Module A/B/C. |
| Minimum Registration | The baseline customer record created at first contact, keyed by `whatsapp_id` only — no name, NRIC, email, or phone required — sufficient to enquire about courses (Requirement 2, AC1). |
| Module A | Enquiry & Lead Nurturing agent — answers FAQs, captures leads, escalates unclear queries. |
| Module B | Enrollment, Invoice & Status Pipeline agent — validates and creates enrollments, manages invoices and status lookups. |
| Module C | Payment Verification & Accounts agent — vision-verifies payment screenshots, issues receipts, supports cancellations/credit notes. |
| SH-code | A course intake's short identifier (e.g. `SH2601`) used to select a specific schedule/date. |
| Staff queue | The `staff_queue` table holding escalated items awaiting human review. |
| `verdict` | The outcome of one payment-proof evaluation: `confirmed`, `mismatch`, or `unreadable`. |
| `whatsapp_id` | The stable WhatsApp JID (e.g. `6591234567@c.us` or `153811586920512@lid`) used as the primary key for customer identity and data isolation. |

---

## Requirements Review Checklist

### Completeness
- [x] All user stories have clear roles, features, and benefits
- [x] Each requirement has specific acceptance criteria using EARS format
- [x] Non-functional requirements are addressed (performance, security, usability, reliability)
- [x] Success criteria are defined and measurable

### Quality
- [x] Requirements are written in active voice
- [x] Each acceptance criterion is testable against the running system (WhatsApp or `/webhook/whatsapp/sync`)
- [x] Requirements describe observable behaviour, not internal implementation, except where the deterministic/agentic split is itself the requirement
- [x] Terminology is consistent throughout and defined in the Glossary

### EARS Format Validation
- [x] WHEN statements describe specific events or triggers
- [x] IF statements describe clear conditions or states
- [x] WHILE statements are not required by this system (no continuously-running participant-facing behaviour beyond the scheduler, covered via WHERE)
- [x] WHERE statements describe specific configuration contexts (`WHATSAPP_ENABLED`, `SCHEDULER_ENABLED`, account type)
- [x] All statements use SHALL for system responses

### Traceability
- [x] Requirements are numbered and organized by system component, matching the module boundaries in `backend/app/`
- [x] Dependencies between requirements are stated in each requirement's Additional Details
- [x] Requirements trace to `README.md` §2 (proposal → CrewAI mapping) and §11 (software architecture)
- [x] Assumptions and constraints are documented separately from acceptance criteria
