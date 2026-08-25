# Q&M AI-Powered Enquiry & Enrollment System

An agentic AI system that runs Q&M Dental Group's **2-Day Basic Certificate in
Dental Assisting** enquiry → enrollment → payment journey entirely through a
single WhatsApp conversation.

It implements the architecture in the project proposal
(`QM_AI_Powered_Enquiry_and_Enrollment_System_V2_4.pdf`) but replaces the n8n
workflow engine with a **Python CrewAI agentic backend**. The proposal's n8n
"AI Agent nodes" map one-to-one onto CrewAI agents with registered tools.

```
WhatsApp ──> Intent Router (CrewAI/OpenAI) ──> Module A / B / C ──> Tools ──> Postgres
   ^                                                                               |
   └─────────────────────── outbound reply (Send WhatsApp Reply) <────────────────┘
```

---

## Table of Contents

- [1. Components](#1-components-all-self-contained-in-this-directory)
- [2. Proposal → CrewAI mapping](#2-proposal--crewai-mapping)
- [3. Software Installation](#3-software-installation)
  - [3.1 Node.js 18+ (WhatsApp gateway)](#31-nodejs-18-whatsapp-gateway)
  - [3.2 Google Chrome (required by whatsapp-web.js)](#32-google-chrome-required-by-whatsapp-webjs)
  - [3.3 Python 3.12 (backend)](#33-python-312-backend)
  - [3.4 Docker Desktop (PostgreSQL database)](#34-docker-desktop-postgresql-database)
  - [3.5 OpenAI API key](#35-openai-api-key)
- [4. Project Setup](#4-project-setup)
  - [Step 1 — Clone / open the project](#step-1--clone--open-the-project)
  - [Step 2 — Start PostgreSQL](#step-2--start-postgresql)
  - [Step 3 — Backend Python environment](#step-3--backend-python-environment)
  - [Step 4 — Backend configuration](#step-4--backend-configuration)
  - [Step 5 — WhatsApp gateway Node.js setup](#step-5--whatsapp-gateway-nodejs-setup)
- [5. Running the Application](#5-running-the-application)
  - [Terminal 1 — PostgreSQL (Docker)](#terminal-1--postgresql-docker)
  - [Terminal 2 — Backend (FastAPI + CrewAI)](#terminal-2--backend-fastapi--crewai)
  - [Terminal 3 — WhatsApp Gateway (Node.js)](#terminal-3--whatsapp-gateway-nodejs)
  - [Verify everything is connected](#verify-everything-is-connected)
- [6. Testing Without WhatsApp](#6-testing-without-whatsapp)
- [7. Database Management UI](#7-database-management-ui)
  - [Tables managed](#tables-managed)
  - [Features](#features)
- [8. Stopping the Application](#8-stopping-the-application)
- [9. Status state machine (Module B/C)](#9-status-state-machine-module-bc)
- [10. Folder structure](#10-folder-structure)
- [11. Software Architecture](#11-software-architecture)
  - [11.1 Overview design](#111-overview-design)
  - [11.2 Backend — agent architecture](#112-backend--agent-architecture)
    - [Intent Router](#intent-router)
    - [Module A — Enquiry & Lead Nurturing](#module-a--enquiry--lead-nurturing)
    - [Module B — Enrollment, Invoice & Payment Pipeline](#module-b--enrollment-invoice--payment-pipeline)
    - [Module C — Payment Verification & Accounts](#module-c--payment-verification--accounts)
  - [11.3 Database structure](#113-database-structure)
- [12. Notes & scope](#12-notes--scope)

---

## 1. Components (all self-contained in this directory)

| Folder               | Role                                                    | Stack                                           |
|----------------------|---------------------------------------------------------|-------------------------------------------------|
| `whatsapp-server/`   | **Frontend / gateway** — inbound + outbound only        | Node.js 18+, `whatsapp-web.js`, Express         |
| `backend/`           | **Agentic backend** — Router + Modules A/B/C, PDF, email, scheduler, portals | Python 3.12, CrewAI, FastAPI, OpenAI, psycopg |
| `docker-compose.yml` | **Database** — local PostgreSQL 16                      | Docker / PostgreSQL 16                          |

---

## 2. Proposal → CrewAI mapping

| Proposal (n8n)                                    | This implementation                                                                    |
|---------------------------------------------------|----------------------------------------------------------------------------------------|
| Shared WhatsApp Intent Router                     | `app/crews/router.py` — deterministic command schema (§10) + CrewAI agent for free text |
| Module A — AI Chatbot & Lead Nurturing            | `app/crews/module_a.py` — conversational CrewAI agent + FAQ + Postgres chat memory    |
| Module B — Enrollment, Invoice & Payment Pipeline | `app/crews/module_b.py` — validate → invoice PDF → email → status machine             |
| Module C — Payment Verification & Accounts        | `app/crews/module_c.py` — OpenAI **vision** verify → auto-confirm/flag → receipt      |
| AI Agent node tools (HTTP, Email, Postgres…)      | `app/tools/*` (CrewAI `@tool`s) over `app/services/*`                                 |
| PDF microservice (Puppeteer)                      | `app/pdf.py` (ReportLab, in-process)                                                   |
| Postgres Chat Memory / state machine              | `app/repositories.py` + `db/schema.sql`                                               |
| Schedule Triggers (follow-ups, nightly report)    | `app/scheduler.py` (APScheduler)                                                       |
| Admin dashboard + Accountant portal               | `app/admin.py` (FastAPI + HTML, basic-auth)                                            |
| Database management web UI                        | `app/dbadmin.py` (full CRUD for all 8 tables, paginated, basic-auth)                   |

WhatsApp command schema (proposal §10):
`/help /courses /fees /schedule /sfc /enroll /mystatus /invoice /pay /receipt`

---

## 3. Software Installation

Install all required software **once** on your machine before setting up the
project. Skip any item you already have installed.

### 3.1 Node.js 18+ (WhatsApp gateway)

The gateway (`whatsapp-server/`) is a Node.js process. You need Node 18 or
later (LTS recommended).

**Windows:**

1. Go to https://nodejs.org and download the **LTS** installer (`.msi`).
2. Run the installer — accept all defaults. This installs both `node` and `npm`.
3. Verify in a new terminal:
   ```powershell
   node --version    # should print v18.x.x or higher
   npm --version
   ```

**Check if already installed:**
```powershell
node --version
```
If it prints a version, you already have Node — skip the install above.

---

### 3.2 Google Chrome (required by whatsapp-web.js)

`whatsapp-web.js` drives WhatsApp Web via Puppeteer, which needs a real Chrome
executable. Google Chrome is almost certainly already installed on Windows.

**Verify the default path:**
```powershell
Test-Path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```
Should print `True`. If `False`, download and install Chrome from
https://www.google.com/chrome.

> The `.env` key `CHROME_PATH` in `whatsapp-server/.env` must point to this
> file. The default value already covers the standard Windows install path.

---

### 3.3 Python 3.12 (backend)

CrewAI requires Python `>=3.10,<3.14`. **Do not use Python 3.14** (the current
default on some machines). Install Python 3.12 alongside any existing version.

**Windows — download installer:**

1. Go to https://www.python.org/downloads/release/python-3129/
2. Under "Files", download **Windows installer (64-bit)** (`python-3.12.9-amd64.exe`).
3. Run the installer:
   - Tick **"Add Python 3.12 to PATH"**.
   - Click **"Install Now"** (or Customize if you want a specific location).
4. Verify the **`py` launcher** picks it up:
   ```powershell
   py -3.12 --version    # Python 3.12.x
   ```

> If `py` is not found, Python was installed without the launcher. Re-run the
> installer and check "Add Python launcher for all users".

**Already have Python 3.12?**
```powershell
py -3.12 --version
```
If it prints `Python 3.12.x`, you are good — skip the install above.

---

### 3.4 Docker Desktop (PostgreSQL database)

Docker Desktop provides the local PostgreSQL 16 container used by the backend.

**Windows:**

1. Go to https://www.docker.com/products/docker-desktop and download the
   Windows installer.
2. Run the installer — accept all defaults. Restart if prompted.
3. Launch **Docker Desktop** from the Start menu and wait for it to show
   "Docker Desktop is running" (the whale icon in the system tray turns steady).
4. Verify:
   ```powershell
   docker --version          # Docker version 25.x.x
   docker compose version    # Docker Compose version v2.x.x
   ```

> **No Docker?** Install native PostgreSQL 16 instead
> (https://www.postgresql.org/download/windows/). Create a database
> `qm_enrollment`, a user `qm_user` with password `qm_password`, and grant all
> privileges. The backend will auto-apply the schema on first start.

---

### 3.5 OpenAI API key

The CrewAI agents and the vision payment verifier both call the OpenAI API.

1. Sign in at https://platform.openai.com.
2. Go to **API keys** → **Create new secret key**.
3. Copy the key — you will paste it into `backend/.env` in Step 5 below.

> **No API key?** The system degrades gracefully. All slash commands (`/help`,
> `/courses`, `/enroll`, `/pay` with a screenshot, etc.) work deterministically
> without any LLM. Free-text messages fall back to a menu. Leave
> `OPENAI_API_KEY` blank (or omit it) for fully offline testing.

---

## 4. Project Setup

Do this once after installing the software above.

### Step 1 — Clone / open the project

Open a terminal in the project root (the folder containing this README):
```powershell
cd "c:\MyWork\_NYP_Project\Q&M\codes\WA_CrewAI"
```

All paths below are relative to this root.

---

### Step 2 — Start PostgreSQL

```powershell
docker compose up -d
```

This pulls the PostgreSQL 16 image (first run only, ~200 MB), creates the
`qm_enrollment` database, and applies `backend/db/schema.sql` and
`backend/db/seed.sql` automatically.

Verify the container is healthy:
```powershell
docker ps --filter name=qm_postgres
```
The `STATUS` column should show `Up ... (healthy)`.

> The backend also bootstraps the schema automatically on first start if you use
> native PostgreSQL instead of Docker — so the `docker compose up` step is
> optional when not using Docker.

---

### Step 3 — Backend Python environment

```powershell
cd backend

# Create a Python 3.12 virtual environment
py -3.12 -m venv .venv

# Install all dependencies
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

Expected output ends with: `Successfully installed crewai-... fastapi-... uvicorn-...`

---

### Step 4 — Backend configuration

```powershell
# Still inside backend/
copy .env.example .env
```

Open `backend/.env` in any text editor and fill in:

```ini
# REQUIRED — your OpenAI API key (leave blank for offline/deterministic testing)
OPENAI_API_KEY=sk-...

# Database (matches the Docker Compose defaults — no change needed for Docker)
DATABASE_URL=postgresql://qm_user:qm_password@localhost:5432/qm_enrollment

# Set false to print WhatsApp replies to console instead of calling the gateway
WHATSAPP_ENABLED=true

# Email delivery — set true and fill in SMTP_* to send real emails
EMAIL_ENABLED=false
```

**Full list of configuration keys** (`backend/.env`):

| Key | Default | Description |
|-----|---------|-------------|
| `OPENAI_API_KEY` | _(blank)_ | OpenAI secret key. Agents + vision require this. |
| `OPENAI_MODEL` | `gpt-4o-mini` | LLM for conversational agents (Module A/B/C). |
| `OPENAI_VISION_MODEL` | `gpt-4o` | Vision model for payment screenshot verification (Module C). |
| `OPENAI_TEMPERATURE` | `0.2` | Sampling temperature for agents. |
| `DATABASE_URL` | `postgresql://qm_user:qm_password@localhost:5432/qm_enrollment` | psycopg connection string. |
| `BACKEND_HOST` | `0.0.0.0` | Bind address for the FastAPI server. |
| `BACKEND_PORT` | `8000` | Port for the FastAPI server. |
| `WHATSAPP_SEND_URL` | `http://localhost:3000/send-reply` | Gateway `/send-reply` endpoint. |
| `WHATSAPP_ENABLED` | `true` | `false` = print replies to console (dev/test). |
| `EMAIL_ENABLED` | `false` | `true` = send real emails via SMTP. |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP server host. |
| `SMTP_PORT` | `587` | SMTP port (587 = STARTTLS). |
| `SMTP_USER` | _(blank)_ | SMTP login username. |
| `SMTP_PASSWORD` | _(blank)_ | SMTP login password / app password. |
| `SMTP_FROM` | `Q&M Training <no-reply@example.com>` | Sender display name + address. |
| `STAFF_EMAIL` | `staff@example.com` | Escalation recipient for Module A flags. |
| `ACCOUNTS_EMAIL` | `accounts@example.com` | Nightly report recipient. |
| `COMPANY_UEN` | `200008000C` | Company UEN printed on PDFs. |
| `PAYNOW_UEN` | `200008000C` | UEN encoded in the PayNow QR code on invoices. |
| `AUTO_CONFIRM_THRESHOLD` | `0.80` | Vision confidence threshold for auto-confirming payments. |
| `SCHEDULER_ENABLED` | `true` | Enable background follow-up + nightly report jobs. |
| `FOLLOWUP_CHECK_CRON_HOUR` | `9` | Hour (SGT) the lead follow-up job runs. |
| `NIGHTLY_REPORT_CRON_HOUR` | `20` | Hour (SGT) the nightly accounts report runs. |
| `QUERY_LOG_ENABLED` | `true` | `false` = skip writing the query → orchestration → agent → tools → response trace to `backend/logs/` (console trace still prints). |

---

### Step 5 — WhatsApp gateway Node.js setup

```powershell
cd ..\whatsapp-server     # from backend/, go up then into whatsapp-server

# Install Node dependencies (skips Chromium download — uses your system Chrome)
$env:PUPPETEER_SKIP_DOWNLOAD="true"
npm install

# Create config
copy .env.example .env
```

Open `whatsapp-server/.env` and confirm `CHROME_PATH` is correct for your
machine (default covers the standard Windows Chrome install path):

```ini
CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
```

**Full list of configuration keys** (`whatsapp-server/.env`):

| Key | Default | Description |
|-----|---------|-------------|
| `BACKEND_WEBHOOK_URL` | `http://localhost:8000/webhook/whatsapp` | Backend endpoint for inbound messages. |
| `REPLY_SERVER_PORT` | `3000` | Port this gateway listens on for `/send-reply`. |
| `CHROME_PATH` | Windows Chrome path | Path to Chrome/Chromium executable. |
| `HEADLESS` | `false` | `false` = visible browser (easier for first QR scan). Set `true` after first scan. |
| `FORWARD_TIMEOUT_MS` | `120000` | Timeout (ms) waiting for the backend to respond. |

---

## 5. Running the Application

Three processes must run simultaneously. Open **three separate terminal windows**.

### Terminal 1 — PostgreSQL (Docker)

```powershell
cd "c:\MyWork\_NYP_Project\Q&M\codes\WA_CrewAI"
docker compose up
```

(Use `docker compose up -d` to run detached and keep using this terminal for
other commands. Use `docker compose logs -f` to watch logs.)

The database is ready when you see:
```
qm_postgres  | database system is ready to accept connections
```

---

### Terminal 2 — Backend (FastAPI + CrewAI)

```powershell
cd "c:\MyWork\_NYP_Project\Q&M\codes\WA_CrewAI\backend"
.venv\Scripts\python run.py
```

The backend is ready when you see:
```
INFO     qm.backend: Q&M backend ready on 0.0.0.0:8000
INFO     uvicorn.error: Application startup complete.
```

Verify at: http://localhost:8000/health

Expected response:
```json
{"status":"ok","db":true,"openai_configured":true}
```

---

### Terminal 3 — WhatsApp Gateway (Node.js)

```powershell
cd "c:\MyWork\_NYP_Project\Q&M\codes\WA_CrewAI\whatsapp-server"
npm start
```

On **first run**, a QR code prints in the terminal:

```
[QR] Scan this with WhatsApp:
████████████████████
████  ██  ████  ████
...
```

1. Open WhatsApp on your phone.
2. Tap **Settings** → **Linked Devices** → **Link a Device**.
3. Scan the QR code shown in the terminal.

Once scanned you will see:
```
[READY] WhatsApp client is ready
[STATUS] Listening on port 3000
```

The session is saved in `whatsapp-server/.wwebjs_auth/` — subsequent starts
skip the QR scan.

> After the first successful QR scan, set `HEADLESS=true` in
> `whatsapp-server/.env` to run the browser invisibly.

---

### Verify everything is connected

Send the connected WhatsApp number:
```
/help
```

You should receive the Q&M command menu back within a few seconds.

---

## 6. Testing Without WhatsApp

Use the synchronous test endpoint to exercise the full backend without the
WhatsApp gateway running. Set `WHATSAPP_ENABLED=false` in `backend/.env` first.

```powershell
# List available courses
curl -s -X POST http://localhost:8000/webhook/whatsapp/sync `
  -H "Content-Type: application/json" `
  -d '{\"from\":{\"phone\":\"6591234567\",\"name\":\"Test User\"},\"message\":{\"body\":\"/courses\"}}'

# Enroll a participant
curl -s -X POST http://localhost:8000/webhook/whatsapp/sync `
  -H "Content-Type: application/json" `
  -d '{\"from\":{\"phone\":\"6591234567\",\"name\":\"Test User\"},\"message\":{\"body\":\"/enroll Infection Control|Tan Wei Ling|S8512345A|tanwl@email.com\"}}'

# Check enrollment status
curl -s -X POST http://localhost:8000/webhook/whatsapp/sync `
  -H "Content-Type: application/json" `
  -d '{\"from\":{\"phone\":\"6591234567\",\"name\":\"Test User\"},\"message\":{\"body\":\"/mystatus\"}}'
```

curl -s -X POST http://localhost:8000/webhook/whatsapp/sync -H "Content-Type: application/json" -d "{\"from\":{\"phone\":\"6591234567\",\"name\":\"Test User\"},\"message\":{\"body\":\"/mystatus\"}}"



**Admin dashboard** (basic-auth `admin` / `qm-admin`):
http://localhost:8000/admin/

**Database Manager** (basic-auth `admin` / `qm-admin`):
http://localhost:8000/dbadmin/

**API docs (Swagger UI):**
http://localhost:8000/docs

**API health check:**
http://localhost:8000/health

---

## 7. Database Management UI

A built-in web interface lets you view and edit every table directly in the
browser — no separate database client (e.g. pgAdmin) required.

**URL:** http://localhost:8000/dbadmin/
**Credentials:** `admin` / `qm-admin` (HTTP Basic Auth)

### Tables managed

| Table | Contents |
|-------|----------|
| `courses` | Course catalogue (name, code, fee, SkillsFuture subsidy cap) |
| `course_schedules` | Intake dates per course |
| `leads` | WhatsApp leads captured by Module A |
| `enrollments` | Enrollment pipeline with status state machine |
| `payments` | Payment verification records from Module C |
| `chat_memory` | Per-phone conversational history (Module A) |
| `staff_queue` | Open escalation tasks for staff |
| `credit_notes` | Credit note requests and approvals |

### Features

- **Browse** — paginated table view (25 / 50 / 100 / 250 rows per page), newest first.
- **Add Row** — `+ Add Row` button opens a modal with auto-detected field types
  (number inputs, date pickers, checkboxes for booleans, JSON textareas for JSONB columns).
- **Edit** — orange **Edit** button opens the row pre-filled; read-only fields
  (`id`, `created_at`, `updated_at`, `resolved_at`) are disabled automatically.
- **Delete** — red **Del** button with a confirmation dialog before deletion.
- **Refresh** — reloads the current page without losing the selected table.

> The DB Manager operates directly on the live database. Changes made here
> immediately affect the running system. Use with care in a production environment.

---

## 8. Stopping the Application

Stop in reverse order:

```powershell
# Terminal 3 — WhatsApp gateway
Ctrl+C

# Terminal 2 — backend
Ctrl+C

# Terminal 1 — database (if running attached)
Ctrl+C

# Or stop the detached container:
docker compose down
```

To also delete the database volume (wipe all data):
```powershell
docker compose down -v
```

---

## 9. Status state machine (Module B/C)

```
enquiry --> enrolled --> invoice_sent --> awaiting_payment --> paid --> receipt_issued
                                                          |
                                                          +--> cancelled (credit note)
```

---

## 10. Folder structure

```
WA_CrewAI/
├── docker-compose.yml           # PostgreSQL 16 container
├── README.md                    # this file
├── whatsapp-server/             # Node.js WhatsApp gateway (frontend)
│   ├── server.js
│   ├── package.json
│   ├── .env.example
│   └── README.md
└── backend/                     # Python CrewAI agentic backend
    ├── run.py                   # uvicorn launcher
    ├── requirements.txt
    ├── .env.example
    ├── README.md
    ├── db/
    │   ├── schema.sql           # tables + status machine + doc-number sequences
    │   └── seed.sql             # courses + intake dates
    └── app/
        ├── main.py              # FastAPI app + webhook + lifespan
        ├── orchestrator.py      # inbound -> router -> module -> reply
        ├── config.py            # pydantic-settings (.env)
        ├── database.py          # psycopg pool + helpers + schema bootstrap
        ├── repositories.py      # data-access layer (CRUD)
        ├── llm.py               # CrewAI LLM + OpenAI vision client
        ├── whatsapp_client.py   # outbound -> gateway /send-reply
        ├── pdf.py               # invoice / receipt / credit-note PDFs + PayNow QR
        ├── email_service.py     # SMTP delivery (logs if disabled)
        ├── context.py           # per-message context (phone + screenshot for tools)
        ├── scheduler.py         # APScheduler: follow-ups + nightly report
        ├── admin.py             # admin dashboard + accountant portal
        ├── dbadmin.py           # database management UI (full CRUD, all tables)
        ├── crews/               # Intent Router + Module A/B/C agents
        ├── tools/               # CrewAI @tools per module
        └── services/            # business logic (enrollment, payments)
```

---

## 11. Software Architecture

### 11.1 Overview design

```
┌───────────┐      ┌─────────────────────┐      ┌───────────────────────┐      ┌────────────┐
│ WhatsApp  │ <--> │  whatsapp-server     │ <--> │  backend               │ <--> │ PostgreSQL │
│ (client)  │      │  (Node.js gateway)   │      │  (FastAPI + CrewAI)    │      │ database   │
└───────────┘      └─────────────────────┘      └───────────────────────┘      └────────────┘
```

1. **WhatsApp (frontend)** — the participant's own WhatsApp app/Web session. No custom client — just standard WhatsApp.
2. **`whatsapp-server/`** (Node.js + `whatsapp-web.js` + Express) — the only component that actually speaks to WhatsApp.
   - *Inbound*: listens for the linked session's `message` events, downloads any attached media, and `POST`s a JSON payload to the backend's `/webhook/whatsapp`.
   - *Outbound*: exposes `POST /send-reply`, which the backend calls to deliver each agent's reply back to the participant.
3. **`backend/app`** (FastAPI + CrewAI) — all business logic and AI reasoning lives here. `main.py` receives the webhook; `orchestrator.py` handles the registration gate and hands off to the Intent Router (`crews/router.py`), which routes to Module A/B/C (§11.2); the chosen module's reply goes back out through `whatsapp_client.py` to the gateway's `/send-reply`.
4. **PostgreSQL** (`docker-compose.yml`, or any Postgres 16 instance) — the only persistent state. Every module reads/writes through `repositories.py`; nothing in `crews/` or `tools/` talks to the database directly.

The gateway and backend are decoupled purely by this webhook contract — replacing `whatsapp-web.js` with the Meta WhatsApp Business Cloud API for production (§12) only touches `whatsapp-server/`, not the backend.

### 11.2 Backend — agent architecture

Four CrewAI agents, matching the proposal's four "AI Agent nodes":

| Agent | File | Responsibility |
|---|---|---|
| Intent Router | `crews/router.py` | Parses `/commands` deterministically; classifies free text into A/B/C via an LLM (keyword heuristics fall back when no API key is set) |
| Module A | `crews/module_a.py` | Enquiry & lead nurturing — FAQs, fees, schedules, lead capture, staff escalation |
| Module B | `crews/module_b.py` | Enrollment, invoice & status — validate → enroll → invoice → status/resend |
| Module C | `crews/module_c.py` | Payment verification & accounts — vision-verifies PayNow/SkillsFuture screenshots, issues receipts, handles credit notes |

All four share the same layered structure rather than duplicating logic:

- **`crews/`** — agent definitions only (role, goal, backstory, task description), built through the shared `kickoff_agent()` helper in `crews/_base.py`. Every agent's backstory embeds `commands.py`'s `COMMAND_REFERENCE` block, so the LLM always sees the same canonical command syntax regardless of module — one prompt fragment, four agents.
- **`tools/`** — CrewAI `@tool`-decorated functions grouped per module (`MODULE_A_TOOLS`, `MODULE_B_TOOLS`, `MODULE_C_TOOLS` in `tools/__init__.py`). Tools are thin wrappers: they read the sender's identity/screenshot from `app/context.py` (never from LLM-supplied arguments) and delegate to `services/`. Tools are shared across modules where it helps an agent finish a task in one turn — e.g. Module B borrows Module A's `List Courses` / `Course Schedule` tools to resolve "course 1/2" references, and Module C borrows Module B's `Enrollment Status` / `Resend Invoice` tools to look up an invoice number the participant forgot.
- **`services/`** — the actual business logic (`courses.py`, `enrollment.py`, `payments.py`, `leads.py`). This is the single source of truth: both the deterministic `/command` path and the agentic free-text path call the *same* service functions, so a participant gets identical results (fees, enrollment rules, payment settlement) whichever path they use.
- **`repositories.py`** — the only place SQL is written; every service goes through it.

Every agent's prompt is assembled from the same three pieces — a **persona/backstory** (who it is, hard behavioural rules), the shared **`COMMAND_REFERENCE`** block (so free text and `/commands` stay mutually consistent), and a per-turn **task description** (conversation history + the new message). Only free-text messages reach the LLM at all — every `/command` is answered deterministically by code, never by an agent (see §2). Below is what each agent's prompt actually constrains, and the tool belt it reasons over.

#### Intent Router

The only agent that runs on *every* free-text message, before any module does. It never touches the database or takes action — its entire job is a one-word classification.

- **Prompt**: classify the new message as `ENQUIRY` (courses/fees/funding/schedules) → Module A, `ENROLLMENT` (enrolling, status, invoice — **including** a short reply like "intake 2" or "yes" that only makes sense as confirming an option the assistant just offered, even without the word "enroll") → Module B, or `PAYMENT` (submitting proof, asking about receipts) → Module C. It's given the last 6 turns of chat history specifically so it can catch that confirmation case, plus the `COMMAND_REFERENCE` block so its notion of "enrollment intent" lines up with the real `/enroll` syntax.
- **Tools**: none — this agent only ever returns a label string (`ENQUIRY` / `ENROLLMENT` / `PAYMENT`).
- **No-LLM fallback**: `_heuristic()` — keyword matching (`"enrol"`, `"payment"`, `"paynow"`, …) plus the same confirmation-after-an-enrollment-offer check, so routing still works with `OPENAI_API_KEY` unset.

#### Module A — Enquiry & Lead Nurturing

- **Prompt** (`crews/module_a.py`): backstory = the full `FAQ_KNOWLEDGE_BASE` (course/policy facts) + a persona block that instructs the agent to:
  - Answer only from the knowledge base/tools, never invent fees, dates, or funding amounts.
  - Write a natural, human, conversational reply — a tool's output is source *data*, not a reply template; don't paste its raw numbered/bulleted layout (especially for broad "show me everything" requests), and never fabricate a placeholder like `Your Name|Your NRIC|Your Email` in an `/enroll` example.
  - Still carry over exact figures/codes/dates from tool output verbatim — natural phrasing is for the sentences *around* the facts, never the facts themselves.
  - Resolve references to earlier turns (e.g. "what about the fees?") from the supplied chat history before calling a tool, and only ask a clarifying question if the history genuinely doesn't resolve it.
  - Call `Save Lead Data` whenever the participant volunteers contact/interest details, and `Flag For Staff Review` when a query is out of scope or it isn't confident.
- **Tools** (`MODULE_A_TOOLS`):

  | Tool | Wraps | Purpose |
  |---|---|---|
  | `List Courses` | `services/courses.py: format_courses()` | All active courses with C-codes and fees |
  | `Course Fees` | `format_fees()` | Full fee breakdown + SkillsFuture subsidy for one course |
  | `Course Schedule` | `format_schedule()` | Upcoming intakes (SH-codes) + ready-to-send `/enroll` line for one course |
  | `SkillsFuture Info` | `format_sfc()` | General SkillsFuture Credit eligibility info (no Singpass lookup) |
  | `Save Lead Data` | `services/leads.py: save_lead()` | Persist any name/NRIC/email/preferred-course details volunteered mid-chat |
  | `Flag For Staff Review` | `leads.py: flag_for_staff()` | Escalate to `staff_queue` + notify `STAFF_EMAIL` |

#### Module B — Enrollment, Invoice & Payment Pipeline

- **Prompt** (`crews/module_b.py`): backstory instructs the agent to:
  - Resolve courses/intakes referenced by the numbered list shown earlier, or by a bare confirmation like "intake 2"/"yes" — read from chat history, act, don't hand the decision back to the participant as a command to type.
  - **Hard rule**: never call `Enroll Participant` without a specific intake the participant has named or chosen — even though the tool *would* silently default to the next available date (that default exists only for the `/enroll` command shortcut). If the intake isn't known yet, call `Course Schedule`, name the resolved course back to the participant (so they can correct a wrong match), list the options, and stop — don't enroll on that turn.
  - Once both course and intake are established, call `Validate Enrollment` then `Enroll Participant` itself — don't ask the participant to type `/enroll` once it already has enough information.
  - Never round/reword/guess a code, invoice number, or amount from a tool's output — and specifically never upgrade "SkillsFuture claimable" into "fully claimable" unless the tool itself confirms S$0 net payable.
  - Use `My Enrollments` (not `List Courses`/`Course Schedule`, which only describe the catalog) for "status of all my courses" — `Enrollment Status` is only the single latest one.
- **Tools** (`MODULE_B_TOOLS`):

  | Tool | Wraps | Purpose |
  |---|---|---|
  | `Validate Enrollment` | `services/enrollment.py: validate_enrollment()` | Pre-flight check of course/name/NRIC/email before enrolling |
  | `Enroll Participant` | `enrollment.py: enroll()` | Create the enrollment, generate + email the invoice, take the schedule seat |
  | `Enrollment Status` | `enrollment.py: status_text()` | The single latest enrollment's status |
  | `My Enrollments` | `enrollment.py: all_status_text()` | Every enrollment on record, or one filtered by course |
  | `Resend Invoice` | `enrollment.py: resend_invoice()` | Re-send the latest invoice email |
  | `List Courses` *(shared with Module A)* | `courses.py: format_courses()` | Resolve "course 1"/"course 2" references |
  | `Course Schedule` *(shared with Module A)* | `courses.py: format_schedule()` | Show intake options so the participant can pick one |

#### Module C — Payment Verification & Accounts

- **Prompt** (`crews/module_c.py`): this agent only ever runs for **free-text payment talk with no image attached** — any message that actually carries a screenshot is settled deterministically by `process_payment()` before the LLM is ever invoked (§11.1), so the money path never depends on the agent's judgement. Its backstory instructs it to:
  - Understand that one invoice may need up to two *kinds* of proof — a SkillsFuture claim confirmation and/or a PayNow transfer confirmation — sent as separate screenshots, since WhatsApp only ever delivers one image per message.
  - Use `Payment Balance` rather than guessing when asked "what do I still owe" — it returns the exact itemised remainder (SkillsFuture / PayNow / both / fully paid).
  - Look up a forgotten invoice number itself via `Enrollment Status`/`Resend Invoice` instead of telling the participant to go find it.
  - Use `Resend Receipt` for receipt requests, knowing it resends *every* receipt for the enrollment (there can be more than one).
- **Tools** (`MODULE_C_TOOLS`):

  | Tool | Wraps | Purpose |
  |---|---|---|
  | `Settle Payment` | `services/payments.py: process_payment()` | Vision-verify an attached screenshot and record it (rarely reached by the agent — see above) |
  | `Resend Receipt` | `payments.py: resend_receipt()` | Re-send every confirmed-payment receipt for the enrollment |
  | `Payment Balance` | `payments.py: outstanding_balance_text()` | Itemised remaining balance by proof type |
  | `Resend Invoice` *(shared with Module B)* | `enrollment.py: resend_invoice()` | Look up/resend the invoice when the invoice number is forgotten |
  | `Enrollment Status` *(shared with Module B)* | `enrollment.py: status_text()` | Confirm which enrollment/invoice is outstanding |

### 11.3 Database structure

PostgreSQL 16, 8 tables (`db/schema.sql`), manageable live via `/dbadmin/` (§7):

| Table | Key columns | Relationships |
|---|---|---|
| `customers` | `whatsapp_id` (unique, primary identity), `phone`, registration fields | Referenced by `whatsapp_id` from every other user-scoped table |
| `courses` | `course_id` (C-code), `full_fee`, `sf_subsidy_cap` | Parent of `course_schedules`, `enrollments` |
| `course_schedules` | `schedule_id` (SH-code), `seats`, `seats_taken` | FK → `courses`; referenced by `enrollments.schedule_id` |
| `leads` | legacy alias, kept for backward compatibility | superseded by `customers` |
| `enrollments` | `invoice_no`, `fee` / `sf_subsidy` / `net_payable`, `schedule_id`, `status` state machine | FK → `courses`, `course_schedules`; parent of `payments`, `credit_notes` |
| `payments` | `payment_type` (`paynow` / `skillsfuture_claim`), `verdict`, `receipt_no` | FK → `enrollments` — **one enrollment can have multiple payment rows**, since an invoice may need a SkillsFuture claim proof, a PayNow proof, or both, each verified and receipted independently |
| `chat_memory` | `whatsapp_id`, `role`, `content` | Per-user conversation history read by every agent for multi-turn context |
| `staff_queue` | `reason`, `module`, `status` | Escalations raised by Module A (unclear enquiry) or Module C (payment mismatch) |
| `credit_notes` | `credit_note_no`, `amount`, `status` | FK → `enrollments` — cancellation/refund workflow |

`enrollments.status` and `payments.verdict` are independent state machines: an enrollment can sit at `awaiting_payment` while it already has one `confirmed` payment (e.g. the SkillsFuture portion) and one `pending` placeholder (e.g. the PayNow portion still outstanding) — see §9.

---

## 12. Notes & scope

- **PayNow QR** in invoices encodes a readable payment reference for the POC;
  swap for a proper EMVCo SGQR payload in `app/pdf.py` for production.
- **Payment verification** is AI-assisted: a confident amount match
  auto-confirms; mismatches or low confidence are flagged to staff (proposal §3).
- **SkillsFuture balance** is intentionally **not** integrated with Singpass —
  the bot links users to the official MySkillsFuture portal (proposal §3/§9).
- **Email** is logged to console unless `EMAIL_ENABLED=true` with SMTP set.
- An invalid `OPENAI_API_KEY` degrades gracefully but causes slow retries —
  leave it blank for offline testing.
- `whatsapp-web.js` is for development / POC. To go to production, replace the
  inbound handler and `/send-reply` sender with Meta WhatsApp Business Cloud API
  calls — the backend contract stays identical (proposal §6).

See [backend/README.md](backend/README.md) and [whatsapp-server/README.md](whatsapp-server/README.md) for component details.
