"""FastAPI application — backend entry point.

Exposes the WhatsApp webhook (the backend side of the shared Intent Router entry
point) plus the admin/accountant portal. On startup it ensures the database
schema exists and starts the schedulers.
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

# Windows consoles default to cp1252; replies contain Unicode (–, •, emoji).
# Make stdout/stderr UTF-8 tolerant so logging/printing never crashes a request.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

from . import admin, database, dbadmin, scheduler
from .config import settings
from .orchestrator import process_and_reply

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("qm.backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        database.init_db()
    except Exception as e:  # noqa: BLE001
        log.error("DB init failed (is PostgreSQL running?): %s", e)
    scheduler.start()
    log.info("Q&M backend ready on %s:%s", settings.backend_host, settings.backend_port)
    yield
    scheduler.shutdown()
    database.close_pool()


app = FastAPI(title="Q&M AI Enquiry & Enrollment System", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin.router)
app.include_router(dbadmin.router)


@app.get("/health")
def health():
    db_ok = True
    try:
        database.query_one("SELECT 1 AS ok")
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"status": "ok", "db": db_ok, "openai_configured": bool(settings.openai_api_key)}


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(payload: dict, background: BackgroundTasks):
    """Inbound from the WhatsApp gateway. Acknowledge fast; process + reply async.

    The reply is delivered out-of-band via the gateway's /send-reply (the agents'
    'Send WhatsApp Reply' action), so we deliberately do NOT return a `reply` key
    here (that would make the gateway double-send).
    """
    background.add_task(process_and_reply, payload)
    return {"status": "received"}


@app.post("/webhook/whatsapp/sync")
async def whatsapp_webhook_sync(payload: dict):
    """Synchronous variant for testing without the gateway: returns the reply text.

    Use with WHATSAPP_ENABLED=false to inspect replies via curl/HTTP directly.

    `module`/`agent` identify which module/agent actually composed the reply
    (`A`/`B`/`C`, or `ROUTER` for a disambiguation menu, or blank for a
    registration-gate prompt) — a dev-tool convenience for the WhatsApp mock's
    "who answered" display, not part of the production gateway contract
    (`/webhook/whatsapp`, above, never returns a body at all).
    """
    result = await run_in_threadpool(process_and_reply, payload)
    return {
        "status": "ok",
        "reply": result.get("reply", ""),
        "module": result.get("module", ""),
        "agent": result.get("agent", ""),
    }


