"""Runs the "enroll then say 'intake 2'" flow N times with a fresh synthetic
identity each time, to measure how often Module B correctly resolves the bare
confirmation into a real enrollment vs. re-emitting the same intake list.

Ground truth is read directly from the enrollments table (not the reply text),
so a "success" here means a real enrollment row was created for that identity.
Does not modify any app/ file - see debug_module_b.py for the tracing approach.
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

from client import SyncClient
from identity import new_identity

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import database, repositories as repo  # noqa: E402

N_TRIALS = 5
database.init_db()

client = SyncClient()
results = []

for i in range(1, N_TRIALS + 1):
    chat_id, phone, name = new_identity(f"repeat{i}")
    reg_body = f"{name} | S1234567A | eval-{phone}@example.com"
    client.send(chat_id, phone, name, reg_body)

    turn1 = "I want to enroll in the infection control course"
    reply1 = client.send(chat_id, phone, name, turn1)
    reply2 = client.send(chat_id, phone, name, "intake 2")

    enrollments = repo.enrollments_by_whatsapp_id(chat_id)
    enrolled = len(enrollments) > 0
    results.append(enrolled)

    print(f"\n{'='*70}\nTRIAL {i}  chat_id={chat_id}")
    print(f"  turn1 reply (first 100 chars): {reply1[:100]!r}")
    print(f"  turn2 reply (first 150 chars): {reply2[:150]!r}")
    print(f"  ENROLLED (ground truth from DB): {enrolled}")

client.close()

n_success = sum(results)
print(f"\n{'='*70}")
print(f"RESULT: {n_success}/{N_TRIALS} trials correctly resolved 'intake 2' into a real enrollment")
print(f"Observed failure rate: {(N_TRIALS - n_success) / N_TRIALS:.0%}")
