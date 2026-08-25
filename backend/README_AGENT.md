# Backend — Q&M Agentic AI (CrewAI)

Python CrewAI backend implementing the Intent Router and Modules A/B/C from the
proposal. FastAPI exposes the WhatsApp webhook and the admin/accountant portal.

## Layout

```
backend/
├── run.py                 # uvicorn launcher (python run.py)
├── requirements.txt
├── .env.example           # copy to .env
├── db/
│   ├── schema.sql         # tables + status machine + doc-number sequences
│   └── seed.sql           # courses + intake dates
├── evals/                 # Ragas agentic test harness (see docs/ragsagenttest.docx)
└── app/
    ├── main.py            # FastAPI app + webhook + lifespan
    ├── orchestrator.py    # inbound -> router -> module -> reply
    ├── config.py          # pydantic-settings (.env)
    ├── database.py        # psycopg pool + helpers + schema bootstrap
    ├── repositories.py    # data-access layer (CRUD)
    ├── llm.py             # CrewAI LLM + OpenAI vision client
    ├── whatsapp_client.py # outbound -> gateway /send-reply
    ├── pdf.py             # invoice / receipt / credit-note PDFs + PayNow QR
    ├── email_service.py   # SMTP delivery (logs if disabled)
    ├── faq.py             # Module A knowledge base
    ├── context.py         # per-message context (phone + screenshot for tools)
    ├── scheduler.py       # APScheduler: follow-ups + nightly report
    ├── admin.py           # admin dashboard + accountant portal
    ├── crews/             # Intent Router + Module A/B/C agents
    ├── tools/             # CrewAI @tools per module
    └── services/          # business logic (courses, leads, enrollment, payments)
```

## Architecture: agents & tools

| Module | CrewAI agent (`role`) | Tools |
|--------|--------------------|-------|
| Router | WhatsApp Intent Router | *(none — pure classifier, no tools)* |
| A | Q&M Training Enquiry Assistant | List Courses, Course Fees, Course Schedule, SkillsFuture Info, Save Lead Data, Flag For Staff Review |
| B | Q&M Enrollment Pipeline Agent | Validate Enrollment, Enroll Participant, Enrollment Status, My Enrollments, Resend Invoice, *List Courses (shared)*, *Course Schedule (shared)* |
| C | Q&M Payment & Accounts Agent | Settle Payment, Resend Receipt, Payment Balance, *Resend Invoice (shared)*, *Enrollment Status (shared)* |

**Design choice:** slash commands (the stateless schema, proposal §10) are
handled deterministically for correctness; free-text / conversational intents go
through the CrewAI agents. The **money path** (`/pay` verification, and any
free-text message with an image attached) always runs deterministically through
`payments.process_payment()` — it never depends on the conversational LLM, even
though `Settle Payment` is nominally one of Module C's tools (see §Module C
below). Tools read the sender's phone and the screenshot from `app/context.py`,
keeping PII/binary data out of the LLM token stream.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | health + db + openai status |
| POST | `/webhook/whatsapp` | inbound from gateway (fast ack, async reply) |
| POST | `/webhook/whatsapp/sync` | inbound, returns reply JSON (testing — also what `evals/` drives) |
| GET  | `/admin/` | admin dashboard (HTML, basic-auth) |
| GET  | `/admin/enrollments` `?status_filter=` | enrollments JSON |
| GET  | `/admin/leads` / `/admin/staff-queue` / `/admin/credit-notes` | JSON views |
| POST | `/admin/credit-notes/request` `?enrollment_id=&reason=` | request credit note |
| POST | `/admin/credit-notes/{id}/approve` | approve + email credit-note PDF |
| POST | `/admin/reports/nightly` | trigger the nightly accounts report now |
| GET  | `/dbadmin/` | database manager UI (HTML, basic-auth) |
| GET  | `/dbadmin/tables` | list managed tables (JSON) |
| GET  | `/dbadmin/tables/{table}/columns` | column metadata (JSON) |
| GET  | `/dbadmin/tables/{table}/rows` | paginated rows (JSON) |
| POST | `/dbadmin/tables/{table}/rows` | insert row |
| PUT  | `/dbadmin/tables/{table}/rows/{id}` | update row |
| DELETE | `/dbadmin/tables/{table}/rows/{id}` | delete row |

Admin basic-auth: `admin` / `qm-admin` (change in `app/admin.py`).

### Database Manager

Open `http://localhost:8000/dbadmin/` in a browser while the backend is running.
Login with username `admin` and password `qm-admin`.

Managed tables: `customers`, `courses`, `course_schedules`, `leads`, `enrollments`, `payments`, `chat_memory`, `staff_queue`, `credit_notes`.

## Run

```bash
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env        # set OPENAI_API_KEY, DATABASE_URL, etc.
.venv\Scripts\python run.py
```

Generated PDFs/CSVs are written to `backend/generated/` (gitignored).

---

## Agent Task Catalogue — Ragas Test Planning Reference

This section inventories every tool each agent actually has (as of the current
code — cross-checked directly against `app/tools/*.py` and `app/crews/*.py`,
not the proposal), then derives the concrete, testable *tasks* each agent can
perform by using them — alone or chained. It's laid out so each task row can
be turned directly into a scenario under `backend/evals/scenarios/`, the way
the four existing scenarios were built (see `docs/ragsagenttest.docx` for the
full methodology and worked calibration traces).

### Metric-selection guide (learned empirically — see `ragsagenttest.docx` Appendices A–D)

| Task shape | Recommended Ragas metric | Why |
|---|---|---|
| Single, well-defined pass/fail behaviour (a refusal, a specific disclosure, "did it correctly report X") | **AspectCritic** (`strictness: 3`, `threshold: 1.0`) | One holistic judge call per attempt, majority-voted. The most reliable metric found in practice — used to fix all three scenarios that failed under the other two metrics. Tell the judge explicitly what *not* to evaluate (e.g. "don't fact-check dollar amounts you have no ground truth for") or it will invent grounds for a "No." |
| Breadth of on-topic engagement across a longer, mixed conversation | TopicAdherenceScore (`mode: "precision"`) | Only reach for this when the transcript has **no** bulleted/numbered list in it. Its topic-extraction step is empirically unstable on any reply containing a list (observed the same unchanged transcript score anywhere from 0.375 to 1.000 across repeated calls) — prefer AspectCritic instead for anything touching the course catalogue or an intake list. |
| A conversation that sustains **one single goal** end-to-end, no side intents | AgentGoalAccuracyWithReference | Its own summarisation step (`InferGoalOutcomePrompt`) collapses the *whole* conversation into one goal/end-state anchored on the *first* turn — it silently drops later, unrelated turns from its own summary before your `reference` is even consulted. Never use it for a chained/multi-intent scenario (e.g. "enrol, then ask something else") — it will false-negative regardless of reference wording. |
| Exact sequence of tool calls (name + args) | ToolCallAccuracy | **Not usable yet** — requires capturing real tool-call traces from inside the CrewAI agent, which the current black-box HTTP harness cannot see. Needs the opt-in, default-off `step_callback` hook proposed as "Phase 2" in `ragsagenttest.docx` §2.3. |
| Ground truth is a concrete DB/system state (an enrollment row exists, an invoice amount matches) | *(no Ragas metric — query the database directly)* | Proved more reliable than any LLM judge for outcome-verification tasks (used to measure the real `module_b` "intake 2" failure rate at 80%, and to validate its fix at 10/10, entirely via `repo.enrollments_by_whatsapp_id()`). Combine with AspectCritic for the *conversational quality* of the same turn if needed. |
| Deterministic / non-agentic code paths (any message with a screenshot attached, all `/command` replies) | *(out of scope for this harness — use conventional fixture-based tests)* | These paths never invoke the LLM at all (see Module C note below), so there is no agentic reasoning for a Ragas judge to evaluate. |

---

### Router — WhatsApp Intent Router

**Role/goal:** classify each inbound free-text message into exactly one of `ENQUIRY` (→ A), `ENROLLMENT` (→ B), or `PAYMENT` (→ C). Runs on every free-text message before any module does. Has **no tools** — it never touches the database or takes action; its entire output is a single classification word. `app/crews/router.py`.

Because it has no tools and produces a label rather than a conversational reply, none of the three Ragas agent metrics used elsewhere in this project fit it directly (there's no "goal outcome" or "topic" to score — just a classification decision). It's still worth testing, just with a different approach:

| Task ID | Task | Example trigger | Suggested test approach |
|---|---|---|---|
| R1 | Classify a clear-intent enquiry | "What courses do you offer?" → A | Deterministic assertion: call the sync endpoint, assert which module's reply shape came back (not a Ragas metric) |
| R2 | Classify a clear-intent enrollment request | "I want to enroll in X" → B | Same — deterministic classification assertion |
| R3 | Classify a clear-intent payment message | "I already paid, here's my proof" → C | Same |
| R4 | Resolve a bare confirmation as continuing a just-offered enrollment choice | Assistant just listed intakes; participant replies "intake 2" / "2" / "yes" → B | Deterministic assertion, but **also** a natural companion check to Module B's own T-B2 below — the router must hand off correctly *before* Module B can resolve it |
| R5 | Route a message with an attached image straight to Module C regardless of caption content | Any image attachment → C | Deterministic — `classify_free_text()` short-circuits on `has_media` before any LLM call |

---

### Module A — Q&M Training Enquiry Assistant

**Role/goal:** answer dental-training enquiries accurately from the FAQ knowledge base and tools, capture leads, escalate when unsure. `app/crews/module_a.py`.

**Tools:**

| Tool | Input(s) | Wraps | What it does |
|---|---|---|---|
| `List Courses` | *(none)* | `services/courses.py: format_courses()` | Every active course with its C-code and fee |
| `Course Fees` | `course` (C-code or name) | `format_fees()` | Full fee breakdown + SkillsFuture subsidy for one course |
| `Course Schedule` | `course` (C-code or name) | `format_schedule()` | Upcoming intakes (SH-codes) + a ready-to-send `/enroll` line for one course |
| `SkillsFuture Info` | *(none)* | `format_sfc()` | General SkillsFuture Credit eligibility info (no Singpass/individual balance lookup) |
| `Save Lead Data` | `name, nric, dob, email, preferred_course, course_date` (all optional) | `services/leads.py: save_lead()` | Persists whichever contact/interest fields the participant volunteered mid-chat; phone comes from context, not the LLM |
| `Flag For Staff Review` | `reason, message` (message optional) | `leads.py: flag_for_staff()` | Escalates to `staff_queue` + notifies `STAFF_EMAIL`, for anything out of scope or answered with low confidence |

**Tasks:**

| Task ID | Task | Example trigger | Tools used | Suggested Ragas metric |
|---|---|---|---|---|
| A1 | Browse the full course catalogue | "What courses do you offer?" | List Courses | AspectCritic (list-heavy reply — avoid TopicAdherenceScore) |
| A2 | Get the fee/subsidy breakdown for one named course | "What's the fee for infection control?" | Course Fees | AspectCritic |
| A3 | Get intake dates for one named course | "When's the next intake for X?" | Course Schedule | AspectCritic |
| A4 | General SkillsFuture Credit eligibility (no course named) | "Can I use SkillsFuture credit?" | SkillsFuture Info | AspectCritic |
| A5 | Multi-turn context carry-over across an enquiry chain | courses → "the fee for X" → "can I use SkillsFuture credit for **that**" (this project's `module_a_ontopic_fees` scenario) | List Courses → Course Fees → (context resolution, no new tool call) | AspectCritic — validated in `ragsagenttest.docx` Appendix D; do not use TopicAdherenceScore, its topic-extraction step is unstable on turn 1's course-list reply |
| A6 | Capture lead details volunteered unprompted | "I'm Mary, mary@email.com, interested in the CPR course" | Save Lead Data | AspectCritic *or* direct DB check (`leads`/`customers` row) — DB check is ground truth if available |
| A7 | Escalate an out-of-scope or off-topic request | "Can you write me a Python script?" (this project's `module_a_offtopic_should_refuse` scenario) | Flag For Staff Review (or a plain decline) | AspectCritic — validated in Appendix D of `ragsagenttest.docx`; give the transcript at least one genuine on-topic turn if ever attempting TopicAdherenceScore here, a single all-off-topic turn cannot score >0 under that metric regardless of correctness |
| A8 | Escalate a sensitive/ambiguous enquiry the FAQ can't confidently answer | A policy question outside `faq.py`'s knowledge base | Flag For Staff Review | AspectCritic |
| A9 | Never fabricate a fee, date, code, or `/enroll` example with placeholder text | Any enquiry reply | *(any of the above)* | AspectCritic, phrased as a negative/guardrail check — "did it ever invent a figure or a placeholder like 'Your Name\|Your NRIC\|Your Email'?" |
| A10 | Stay within the FAQ scope while broadly summarising rather than pasting raw tool output for a broad request | "Show me everything with schedules" | List Courses (+ Course Schedule per course, at agent's discretion) | AspectCritic — checks *style* (natural summary vs. raw dump), which is inherently a single-judgment check, not a topic-breadth one |

---

### Module B — Q&M Enrollment Pipeline Agent

**Role/goal:** enroll participants accurately, or retrieve their enrollment status/invoice. `app/crews/module_b.py`. Contains a hard rule (already the subject of a confirmed, fixed defect — see `ragsagenttest.docx` Appendix on `module_b_enroll_happy_path`): never call `Enroll Participant` while the intake is still unspecified.

**Tools:**

| Tool | Input(s) | Wraps | What it does |
|---|---|---|---|
| `Validate Enrollment` | `course, full_name, nric, email` (name/nric/email optional, auto-filled from profile) | `services/enrollment.py: validate_enrollment()` | Pre-flight check; returns `VALID` or a specific fixable error |
| `Enroll Participant` | `course, full_name, nric, email, schedule_no, sh_code` | `enrollment.py: enroll()` | Creates the enrollment, generates + emails the invoice, takes the schedule seat. **Must never be called without an explicit `sh_code`/`schedule_no` the participant chose** — the tool would otherwise silently default to the next available date |
| `Enrollment Status` | *(none)* | `enrollment.py: status_text()` | The participant's single latest enrollment |
| `My Enrollments` | `course` (optional filter) | `enrollment.py: all_status_text()` | Every enrollment on record for the participant |
| `Resend Invoice` | *(none)* | `enrollment.py: resend_invoice()` | Re-sends the latest invoice email |
| `List Courses` *(shared with A)* | *(none)* | `courses.py: format_courses()` | Resolves "course 1"/"course 2" references |
| `Course Schedule` *(shared with A)* | `course` | `courses.py: format_schedule()` | Shows intake options so the participant can pick one |

**Tasks:**

| Task ID | Task | Example trigger | Tools used (sequence) | Suggested Ragas metric |
|---|---|---|---|---|
| B1 | Full free-text enrollment happy path, intake given up front | "Enroll me in C2601, SH2601" (or equivalent free text naming both) | List Courses (if needed) → Course Schedule (if needed) → Validate Enrollment → Enroll Participant | AspectCritic + DB ground truth (`repo.enrollments_by_whatsapp_id`) — this project's most reliable validation pattern |
| B2 | Enrollment resolved via a bare confirmation referencing a just-shown intake list | course named → agent lists intakes → "intake 2" (this project's `module_b_enroll_happy_path` scenario — the one confirmed defect + fix in this codebase) | Course Schedule → (context resolution) → Validate Enrollment → Enroll Participant | AspectCritic + DB ground truth. **Run this one across multiple trials, not once** — the underlying defect was intermittent (measured 80% failure rate pre-fix, 0% post-fix across 10 trials); a single-run PASS is not sufficient evidence |
| B3 | Correctly withhold enrollment when no intake is specified yet | "I want to enroll in the infection control course" (first turn only, no intake given) | Course Schedule (must stop here — no Validate/Enroll call) | AspectCritic, negative/guardrail framing — "did it correctly withhold enrollment and show intake options instead of enrolling by default?" **plus** DB check confirming no enrollment row exists |
| B4 | Check the single latest enrollment's status | "What's my enrollment status?" | Enrollment Status | AspectCritic |
| B5 | Check status of every enrollment, optionally filtered to one course | "What am I enrolled in?" / "Am I enrolled in the CPR course?" | My Enrollments | AspectCritic |
| B6 | Resend the invoice email | "Can you resend my invoice?" | Resend Invoice | AspectCritic |
| B7 | Resolve a course/intake reference to a numbered list shown earlier in the conversation | "course 2" / "SH2604" referenced directly | List Courses or Course Schedule → (resolution) | AspectCritic |
| B8 | Surface a specific validation failure rather than enrolling anyway | Invalid NRIC/email supplied, or unknown course | Validate Enrollment (returns the specific error) | AspectCritic — checks the exact error was relayed, not paraphrased away |
| B9 | Never silently default to the next available intake in conversational flow | Any enrollment request without an explicit intake choice | Enroll Participant must NOT be called | AspectCritic + DB check (no enrollment row / correct schedule_id if one was created) |

---

### Module C — Q&M Payment & Accounts Agent

**Role/goal:** help participants submit payment proof and resend receipts. `app/crews/module_c.py`. **Important scoping note:** any message carrying an image is settled *before* the LLM agent is ever invoked (`payments.process_payment()` runs deterministically — see `run()` in `module_c.py`). The conversational agent below only ever runs for free-text payment talk **with no image attached**.

**Tools:**

| Tool | Input(s) | Wraps | What it does |
|---|---|---|---|
| `Settle Payment` | *(none — screenshot read from context)* | `services/payments.py: process_payment()` | Vision-verifies an attached screenshot and records it. **Rarely actually reached by the agent's own reasoning** — see scoping note above; nominally in the tool belt but the deterministic code path in `run()` almost always gets there first |
| `Resend Receipt` | *(none)* | `payments.py: resend_receipt()` | Re-sends every confirmed-payment receipt for the enrollment (an invoice can have more than one, e.g. SkillsFuture + PayNow) |
| `Payment Balance` | *(none)* | `payments.py: outstanding_balance_text()` | Itemised remaining balance (SkillsFuture claim amount / PayNow amount / both / fully paid) |
| `Resend Invoice` *(shared with B)* | *(none)* | `enrollment.py: resend_invoice()` | Looks up/resends the invoice when the participant has forgotten the invoice number |
| `Enrollment Status` *(shared with B)* | *(none)* | `enrollment.py: status_text()` | Confirms which enrollment/invoice is outstanding |

**Tasks:**

| Task ID | Task | Example trigger | Tools used | Suggested Ragas metric |
|---|---|---|---|---|
| C1 | Report the outstanding balance for the latest enrollment | Enroll (via B) → "how much do I still need to pay?" (this project's `module_c_balance_after_enroll` scenario) | Payment Balance | AspectCritic — **do not use AgentGoalAccuracyWithReference**; confirmed in `ragsagenttest.docx` Appendix C that its summarisation step drops this kind of follow-up turn from a multi-intent (enrol → ask balance) conversation regardless of reference wording |
| C2 | Resend payment receipt(s) | "Can I get my receipt again?" | Resend Receipt | AspectCritic |
| C3 | Recover a forgotten invoice number via conversation, then guide the participant to pay | "I forgot my invoice number, how do I pay?" | Enrollment Status or Resend Invoice → (guidance reply naming the invoice) | AspectCritic |
| C4 | Correctly ask for a screenshot when a participant claims payment without attaching one | "I already paid" (no image) | *(no settlement tool — must not fabricate confirmation)* | AspectCritic, negative/guardrail framing — must not claim payment is confirmed |
| C5 | Resolve an invoice number mentioned in free text (e.g. "pay for INV-2026-0861") without requiring the exact `/pay` format | Free-text mention of a specific invoice number, no image | Payment Balance or Enrollment Status, scoped to that invoice | AspectCritic |
| C6 | *(Not agent-testable via this harness)* Vision-verify a PayNow/SkillsFuture screenshot and settle payment | Any message with an image attached | `Settle Payment` — but reached via the deterministic path in `run()`, not agent reasoning | Out of scope for Ragas agent metrics — this path never invokes the CrewAI agent. Test with a conventional fixture-based test instead (a known screenshot image + assert on `payments` table / reply text) |

---

### Using this catalogue

Each task row above maps directly onto a scenario YAML under `backend/evals/scenarios/` — `id`, `module`, a `turns:` list built from the example trigger (chained with any setup turns needed, e.g. an enrollment before a balance check), and a `metric`/`definition` (or `reference`/`allowed_topics`) chosen per the metric-selection guide. See `docs/ragsagenttest.docx` §7 ("Setting Up New Test Cases") for the authoring steps, and its Appendices A–D for four fully worked examples of writing, running, diagnosing, and fixing a scenario using this exact method.
