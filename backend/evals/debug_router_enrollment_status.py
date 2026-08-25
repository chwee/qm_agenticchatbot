"""Quantifies how often the Router misclassifies an enrollment-status
enquiry as ENQUIRY (-> Module A) instead of ENROLLMENT (-> Module B), per
the router's own stated rule: "ENROLLMENT = wanting to enroll, checking
status, getting invoice". Calls router.classify_free_text() directly, with
no chat history, so only the message wording itself is being judged.
"""
from __future__ import annotations

import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import context, database  # noqa: E402
from app.crews import router  # noqa: E402

database.init_db()

PHRASINGS = [
    "What are the courses I enrolled?",
    "what the courses I registered for?",
    "what am I enrolled in?",
    "show me my enrollments",
    "which courses have I signed up for?",
]

N_TRIALS = 3

context.set_message({
    "whatsapp_id": "router-debug@c.us",
    "phone": "90000000",
    "display_name": "Router Debug",
    "customer": {},
    "body": "",
    "media": None,
})

for phrasing in PHRASINGS:
    results = []
    for _ in range(N_TRIALS):
        module = router.classify_free_text(phrasing, has_media=False)
        results.append(module)
    print(f"{phrasing!r:55s} -> {results}")
