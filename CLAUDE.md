# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Q&M AI Enquiry & Enrollment System — an agentic AI system that runs Q&M Dental Group's course enquiry → enrollment → payment journey entirely through one WhatsApp conversation. It implements a project proposal that originally specified n8n workflows; this codebase replaces n8n with a Python CrewAI backend (the proposal's n8n "AI Agent nodes" map one-to-one onto CrewAI agents with registered tools — see `README.md` §2 for the full mapping table).

Three independently-run components, no shared build system:

| Folder | Role | Stack |
|---|---|---|
| `whatsapp-server/` | Gateway — only component that speaks to WhatsApp (inbound webhook forwarding + outbound `/send-reply`) | Node.js, `whatsapp-web.js`, Express |
| `backend/` | All business logic and AI reasoning — Intent Router + Modules A/B/C, PDF/email, scheduler, admin portals | Python 3.12, CrewAI, FastAPI, psycopg |
| `docker-compose.yml` | Local PostgreSQL 16 | Docker |

The gateway and backend are decoupled purely by the webhook contract (`POST /webhook/whatsapp` in, gateway's `POST /send-reply` out) — swapping `whatsapp-web.js` for the Meta WhatsApp Business Cloud API in production only touches `whatsapp-server/`, never `backend/`.

## Commands

Three processes run simultaneously, each in its own terminal, from the repo root unless noted.

**Database:**
```powershell
docker compose up -d              # start Postgres 16; auto-applies backend/db/schema.sql + seed.sql on first boot
docker compose down                # stop, keep data volume
docker compose down -v             # stop + wipe data volume
```

**Backend** (Python 3.12 required — CrewAI needs `>=3.10,<3.14`; do not use 3.14):
```powershell
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env             # then fill in OPENAI_API_KEY, DATABASE_URL, etc.
.venv\Scripts\python run.py        # starts uvicorn on BACKEND_HOST:BACKEND_PORT (default 0.0.0.0:8000)
```
Health check: `GET http://localhost:8000/health` → `{"status":"ok","db":true,"openai_configured":true}`

**WhatsApp gateway** (Node 18+, needs a local Chrome install):
```powershell
cd whatsapp-server
$env:PUPPETEER_SKIP_DOWNLOAD="true"; npm install
copy .env.example .env             # confirm CHROME_PATH
npm start                          # scan the printed QR with WhatsApp on first run
```

**Testing without the WhatsApp gateway** — set `WHATSAPP_ENABLED=false` in `backend/.env`, then drive the backend directly:
```powershell
curl -s -X POST http://localhost:8000/webhook/whatsapp/sync `
  -H "Content-Type: application/json" `
  -d '{"from":{"phone":"6591234567","name":"Test User"},"message":{"body":"/courses"}}'
```

**Agentic evals** (Ragas-based; scores the CrewAI agents' free-text reasoning, not the deterministic `/command` paths — requires the backend already running and `OPENAI_API_KEY` set):
```powershell
cd backend/evals
pip install -r requirements-eval.txt
python run_eval.py [--base-url http://localhost:8000] [--model gpt-4o-mini]
```
Runs every scenario YAML in `backend/evals/scenarios/` (no built-in single-scenario filter) and writes `backend/evals/reports/report.md`. See `backend/README_AGENT.md` (§"Agent Task Catalogue") for the metric-selection guide and which Ragas metric to use for a new scenario — it documents specific empirically-observed failure modes (e.g. `TopicAdherenceScore` is unstable on any transcript containing a bulleted/numbered list; `AgentGoalAccuracyWithReference` silently drops later turns in multi-intent conversations). There is no non-agentic unit test suite — deterministic `/command` paths and the money path (any message with an image attached) never invoke the LLM, so they're meant to be tested with conventional fixture-based tests against `/webhook/whatsapp/sync`, not Ragas.

**Admin UIs** (basic-auth `admin` / `qm-admin`, credentials in `backend/app/admin.py`):
- `http://localhost:8000/admin/` — enrollments/leads/staff-queue/credit-notes dashboard
- `http://localhost:8000/dbadmin/` — full CRUD over all 11 tables, no separate DB client needed
- `http://localhost:8000/docs` — Swagger UI

## Architecture

### Request flow

```
WhatsApp ─→ whatsapp-server (Node gateway) ─→ POST /webhook/whatsapp ─→ orchestrator.py
                                                                              │
                                                             registration gate, then
                                                                              ▼
                                                                    crews/router.py
                                                          (Intent Router: classifies
                                                           free text into A/B/C;
                                                           /commands bypass this)
                                                                              │
                                              ┌───────────────────────────────┼───────────────────────────────┐
                                              ▼                               ▼                               ▼
                                        crews/module_a.py              crews/module_b.py              crews/module_c.py
                                        (enquiry/lead)                 (enrollment/invoice)            (payment/receipt)
                                              │                               │                               │
                                              └───────────────┬───────────────┴───────────────┬───────────────┘
                                                               ▼                               ▼
                                                          tools/*.py                     services/*.py
                                                     (thin CrewAI @tool wrappers)   (business logic, single
                                                                                     source of truth for both
                                                                                     agentic and /command paths)
                                                                              │
                                                                              ▼
                                                                      repositories.py
                                                                  (only place SQL is written)
                                                                              │
                                                                              ▼
                                                                        PostgreSQL
                                                                              │
                                                                              ▼
                                                          whatsapp_client.py ─→ gateway's POST /send-reply ─→ WhatsApp
```

### Key design decisions

- **Deterministic vs. agentic split**: every `/command` (`/help /courses /fees /schedule /sfc /enroll /mystatus /invoice /pay /receipt`) is answered by code, never by an LLM agent. Only free-text messages reach the Intent Router and a module agent. Both paths call the exact same `services/*` functions, so results (fees, enrollment rules, payment settlement) are identical either way.
- **Money path is always deterministic**: any message carrying an image (a payment screenshot) is settled by `services/payments.py: process_payment()` *before* the Module C agent is ever invoked — vision-verification and payment recording never depend on LLM judgement, even though `Settle Payment` is nominally one of Module C's tools.
- **Tools never trust LLM-supplied identity**: `tools/*.py` read the sender's phone/whatsapp_id and any attached screenshot from `app/context.py` (per-message context set by the orchestrator), never from arguments the LLM fills in. Keeps PII/binary data out of the LLM token stream and prevents an agent from acting as an arbitrary user.
- **Tools are shared across modules** where it lets an agent finish a task in one turn: Module B borrows Module A's `List Courses`/`Course Schedule` to resolve "course 1/2" references; Module C borrows Module B's `Enrollment Status`/`Resend Invoice` to look up a forgotten invoice number.
- **Every agent's prompt is assembled from the same three pieces**: a persona/backstory with hard behavioural rules, the shared `commands.py: COMMAND_REFERENCE` block (so free-text intent and `/command` syntax never drift apart), and a per-turn task description (chat history + new message). Built through the common `kickoff_agent()` helper in `crews/_base.py`.
- **No-LLM degradation**: with `OPENAI_API_KEY` unset, all `/command`s still work fully; the Intent Router falls back to `_heuristic()` keyword matching; free-text messages fall back to a menu.
- **`enrollments.status` and `payments.verdict` are independent state machines** — an enrollment can sit at `awaiting_payment` with one `confirmed` payment (e.g. SkillsFuture portion) and one `pending` placeholder (e.g. PayNow portion still outstanding), since one enrollment can require multiple payment proofs verified independently. State machine: `enquiry → enrolled → invoice_sent → awaiting_payment → paid → receipt_issued`, with a `cancelled` branch (credit note) off any state.
- **`customers` is the authoritative identity table**, keyed by `whatsapp_id` (stable JID from the gateway — `phone` may be null/unconfirmed for `@lid` accounts until the user confirms it). `leads` is a deprecated pre-`customers` table kept only for backward compatibility — new code should never write to it directly.

### Layering (`backend/app/`)

- `crews/` — agent definitions only (role/goal/backstory/task). No business logic.
- `tools/` — CrewAI `@tool`-decorated thin wrappers per module (`MODULE_A_TOOLS`/`MODULE_B_TOOLS`/`MODULE_C_TOOLS` in `tools/__init__.py`), delegate to `services/`.
- `services/` — actual business logic (`courses.py`, `enrollment.py`, `payments.py`, `leads.py`); the single source of truth called by both `/command` and agentic paths.
- `repositories.py` — the only file that writes SQL; every service goes through it.
- `orchestrator.py` — inbound webhook → registration gate → router → module → reply, the top-level control flow.
- `config.py` — `pydantic-settings` loaded from `backend/.env`; see `.env.example` for the full key list (notable ones: `OPENAI_API_KEY` blank = fully offline/deterministic mode; `WHATSAPP_ENABLED=false` = print replies to console instead of calling the gateway; `QUERY_LOG_ENABLED` = write the full query→orchestration→agent→tools→response trace to `backend/logs/`).
- `database.py` — psycopg connection pool + schema bootstrap (auto-applies `db/schema.sql` on startup even without Docker).
- `db/schema.sql` — idempotent (`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` throughout, safe to re-run). `db/migrate_*.sql` files are one-time migrations for pre-existing databases only — **new installs need only `schema.sql`**, the migration files explicitly say so in their own header comments.

Generated PDFs/CSVs go to `backend/generated/` (gitignored). Query traces go to `backend/logs/` (gitignored).
