"""Database Management UI — full CRUD for all PostgreSQL tables.

Mounted at /dbadmin/ with HTTP Basic auth (admin / qm-admin).

  GET  /dbadmin/                        → HTML dashboard
  GET  /dbadmin/tables                  → list of table names
  GET  /dbadmin/tables/{t}/columns      → column metadata
  GET  /dbadmin/tables/{t}/rows         → paginated rows (JSON)
  POST /dbadmin/tables/{t}/rows         → insert row
  PUT  /dbadmin/tables/{t}/rows/{id}    → update row
  DELETE /dbadmin/tables/{t}/rows/{id}  → delete row
"""
from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import database as db
from .services import payments as payment_service

router = APIRouter(prefix="/dbadmin", tags=["dbadmin"])
_security = HTTPBasic(realm="Q&M Database Manager")

_ADMIN_USER = "admin"
_ADMIN_PASS = "qm-admin"

MANAGED_TABLES = [
    "customers",        # central identity store — start here
    "courses",
    "course_schedules",
    "reminders",
    "leads",            # legacy; new rows go to customers
    "enrollments",
    "payments",
    "chat_memory",
    "staff_queue",
    "credit_notes",
    "conversation_state",  # sticky-routing signal — Requirement 3 AC10-AC12
]

_READ_ONLY_COLS = {"id", "created_at", "updated_at", "resolved_at", "sent_at", "notified_at"}

# Every managed table except the seeded course catalog — wiped by the
# "Clear All Memory" dashboard action. courses/course_schedules are
# reference/config data, not participant memory: wiping them would need a
# reseed before the app works again, so they're deliberately excluded.
_WIPE_TABLES = [t for t in MANAGED_TABLES if t not in ("courses", "course_schedules")]


def _auth(creds: HTTPBasicCredentials = Depends(_security)) -> str:
    ok = secrets.compare_digest(creds.username, _ADMIN_USER) and secrets.compare_digest(
        creds.password, _ADMIN_PASS
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Q&M Database Manager"'},
        )
    return creds.username


def _check_table(table: str) -> str:
    if table not in MANAGED_TABLES:
        raise HTTPException(status_code=404, detail=f"Table '{table}' is not managed")
    return table


def _get_pk_column(table: str) -> str:
    """The table's actual primary-key column — every managed table used
    `id SERIAL PRIMARY KEY` until conversation_state (Task 44), which is
    keyed on `whatsapp_id` instead (no `id` column at all) and broke every
    "id"-hardcoded query below with `UndefinedColumn: column "id" does not
    exist`. Falls back to 'id' only if a table genuinely has no PK
    constraint (shouldn't happen for anything in MANAGED_TABLES)."""
    row = db.query_one(
        """SELECT kcu.column_name
           FROM information_schema.table_constraints tc
           JOIN information_schema.key_column_usage kcu
             ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
           WHERE tc.table_schema = 'public' AND tc.table_name = %s
             AND tc.constraint_type = 'PRIMARY KEY'
           LIMIT 1""",
        (table,),
    )
    return (row or {}).get("column_name") or "id"


def _get_columns(table: str) -> list[dict]:
    pk = _get_pk_column(table)
    cols = db.query_all(
        """SELECT column_name, data_type, is_nullable, column_default
           FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = %s
           ORDER BY ordinal_position""",
        (table,),
    )
    for c in cols:
        c["is_pk"] = c["column_name"] == pk
    return cols


def _validate_and_coerce(table: str, body: dict) -> dict:
    allowed = {c["column_name"] for c in _get_columns(table)} - _READ_ONLY_COLS
    bad = set(body) - allowed
    if bad:
        raise HTTPException(
            status_code=422, detail=f"Unknown or read-only columns: {sorted(bad)}"
        )
    return {
        k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
        for k, v in body.items()
    }


# ── API endpoints ────────────────────────────────────────────────────────────

@router.get("/tables", dependencies=[Depends(_auth)])
def list_tables() -> list[str]:
    return MANAGED_TABLES


@router.get("/tables/{table}/columns", dependencies=[Depends(_auth)])
def get_columns(table: str):
    _check_table(table)
    return _get_columns(table)


@router.get("/tables/{table}/rows", dependencies=[Depends(_auth)])
def list_rows(
    table: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    _check_table(table)
    offset = (page - 1) * page_size
    pk = _get_pk_column(table)
    rows = db.query_all(
        f"SELECT * FROM {table} ORDER BY {pk} DESC LIMIT %s OFFSET %s",
        (page_size, offset),
    )
    total = (db.query_one(f"SELECT COUNT(*) AS n FROM {table}") or {}).get("n", 0)
    return {"rows": rows, "total": total, "page": page, "page_size": page_size}


@router.post("/tables/{table}/rows", dependencies=[Depends(_auth)])
def create_row(table: str, body: dict):
    _check_table(table)
    coerced = {k: v for k, v in _validate_and_coerce(table, body).items() if v is not None}
    if not coerced:
        raise HTTPException(status_code=422, detail="No fields provided")
    cols = list(coerced)
    row = db.query_one(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s']*len(cols))}) RETURNING *",
        list(coerced.values()),
    )
    return row


@router.put("/tables/{table}/rows/{row_id}", dependencies=[Depends(_auth)])
def update_row(table: str, row_id: str, body: dict):
    _check_table(table)
    pk = _get_pk_column(table)
    coerced = _validate_and_coerce(table, body)
    if not coerced:
        raise HTTPException(status_code=422, detail="No fields provided")
    sets = ", ".join(f"{k} = %s" for k in coerced)
    row = db.query_one(
        f"UPDATE {table} SET {sets} WHERE {pk} = %s RETURNING *",
        list(coerced.values()) + [row_id],
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Row not found")
    return row


@router.delete("/tables/{table}/rows/{row_id}", dependencies=[Depends(_auth)])
def delete_row(table: str, row_id: str):
    _check_table(table)
    pk = _get_pk_column(table)
    row = db.query_one(f"DELETE FROM {table} WHERE {pk} = %s RETURNING {pk}", (row_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Row not found")
    return {"deleted": row_id}


@router.delete("/chat-memory/all", dependencies=[Depends(_auth)])
def clear_chat_memory():
    """Wipe every row from chat_memory — all users lose their conversation
    history/context. Scoped to this one table only (not a generic
    delete-everything endpoint) since it's the one table meant to be
    disposable; other tables hold financial/enrollment records."""
    count = (db.query_one("SELECT COUNT(*) AS n FROM chat_memory") or {}).get("n", 0)
    db.execute("DELETE FROM chat_memory")
    return {"deleted": count}


@router.delete("/all-memory", dependencies=[Depends(_auth)])
def clear_all_memory():
    """Wipe every participant/transactional table back to a clean slate —
    customers, leads, reminders, enrollments, payments, chat_memory,
    staff_queue, credit_notes, conversation_state (_WIPE_TABLES, everything
    in MANAGED_TABLES except the seeded course catalog). All FK
    relationships among these tables (reminders/conversation_state →
    customers, payments/credit_notes → enrollments) are within this same
    set, so a single TRUNCATE handles them together; CASCADE is a safety
    net, not load-bearing, since nothing outside this set references into
    it. RESTART IDENTITY resets id sequences back to 1 for a genuinely
    clean slate. courses/course_schedules are deliberately untouched — see
    _WIPE_TABLES."""
    counts = {t: (db.query_one(f"SELECT COUNT(*) AS n FROM {t}") or {}).get("n", 0) for t in _WIPE_TABLES}
    db.execute(f"TRUNCATE TABLE {', '.join(_WIPE_TABLES)} RESTART IDENTITY CASCADE")
    return {"deleted": counts, "total": sum(counts.values())}


# ── Business-action endpoints ────────────────────────────────────────────────

@router.post("/enrollments/{enrollment_id}/cancel", dependencies=[Depends(_auth)])
def cancel_enrollment(enrollment_id: int, reason: str = "Cancellation"):
    """Cancel an enrollment and raise a credit note (Module C workflow)."""
    result = payment_service.request_credit_note(enrollment_id, reason)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    return result


@router.post("/credit-notes/{credit_note_id}/approve", dependencies=[Depends(_auth)])
def approve_credit_note(credit_note_id: int):
    """Approve a credit note — generates PDF and emails the participant."""
    result = payment_service.approve_credit_note(credit_note_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed"))
    return result


@router.post("/credit-notes/{credit_note_id}/reject", dependencies=[Depends(_auth)])
def reject_credit_note(credit_note_id: int):
    """Reject a credit note (no email sent — staff to contact participant manually)."""
    row = db.query_one(
        "UPDATE credit_notes SET status = 'rejected', resolved_at = now() WHERE id = %s RETURNING *",
        (credit_note_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Credit note not found")
    return {"ok": True, "credit_note": row}


# ── HTML dashboard ───────────────────────────────────────────────────────────

_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Q&M — Database Manager</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,Arial,sans-serif;background:#f0f2f8;color:#1a1a2e;display:flex;height:100vh;overflow:hidden}
/* ── Sidebar */
#sidebar{width:190px;background:#1F3864;color:#fff;display:flex;flex-direction:column;flex-shrink:0}
.brand{padding:14px 12px 10px;border-bottom:1px solid #2d4f7c}
.brand h2{font-size:14px;font-weight:700;line-height:1.3}
.brand small{display:block;font-size:10px;font-weight:400;color:#8eb4e3;text-transform:uppercase;letter-spacing:.8px;margin-top:2px}
.tbl-item{padding:9px 14px;cursor:pointer;font-size:12px;border-left:3px solid transparent;transition:background .12s}
.tbl-item:hover{background:#2d4f7c}
.tbl-item.active{background:#4472c4;border-left-color:#adc8f5;font-weight:600}
/* ── Main */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
#toolbar{background:#fff;border-bottom:1px solid #d9dde8;padding:10px 16px;display:flex;align-items:center;gap:10px;flex-shrink:0}
#toolbar h1{font-size:15px;flex:1;font-weight:700;color:#1F3864}
#row-count{font-size:12px;color:#777}
#table-wrap{flex:1;overflow:auto}
/* ── Data table */
table{width:100%;border-collapse:collapse;font-size:12.5px}
thead th{background:#1F3864;color:#fff;padding:9px 10px;text-align:left;position:sticky;top:0;z-index:2;white-space:nowrap;font-weight:600}
tbody tr:nth-child(even){background:#f5f7fb}
tbody tr:hover{background:#e4ebf7}
td{padding:7px 10px;border-bottom:1px solid #e5e8f0;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle}
td.act{white-space:nowrap;width:1%;padding-right:14px}
/* ── Buttons */
.btn{padding:7px 14px;border:none;border-radius:4px;cursor:pointer;font-size:13px;font-weight:500;transition:background .12s}
.btn-primary{background:#4472c4;color:#fff}.btn-primary:hover{background:#2f5496}
.btn-cancel{background:#95a5a6;color:#fff}.btn-cancel:hover{background:#7f8c8d}
.btn-refresh{background:#27ae60;color:#fff}.btn-refresh:hover{background:#1e8449}
.btn-sm{padding:3px 8px;font-size:11px;border-radius:3px}
.btn-edit{background:#e67e22;color:#fff}.btn-edit:hover{background:#ca6f1e}
.btn-del{background:#c0392b;color:#fff}.btn-del:hover{background:#922b21}
.btn-clear{background:#922b21;color:#fff}.btn-clear:hover{background:#6e211a}
.btn-cancel-enroll{background:#8e44ad;color:#fff}.btn-cancel-enroll:hover{background:#7d3c98}
.btn-approve{background:#27ae60;color:#fff}.btn-approve:hover{background:#1e8449}
.btn-reject{background:#c0392b;color:#fff}.btn-reject:hover{background:#922b21}
/* ── Pagination */
#pager{background:#fff;border-top:1px solid #d9dde8;padding:8px 16px;display:flex;align-items:center;gap:10px;font-size:12px;flex-shrink:0}
#pager button{padding:4px 10px;font-size:12px}
/* ── Empty state */
#empty{padding:80px 40px;text-align:center;color:#aaa;font-size:14px}
/* ── Modal */
#mbg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;align-items:center;justify-content:center}
#mbg.open{display:flex}
#modal{background:#fff;border-radius:8px;width:580px;max-width:96vw;max-height:88vh;display:flex;flex-direction:column;box-shadow:0 8px 30px rgba(0,0,0,.25)}
#mhead{padding:14px 18px;border-bottom:1px solid #e5e8f0;display:flex;justify-content:space-between;align-items:center}
#mhead h2{font-size:14px;font-weight:700;color:#1F3864}
#mbody{padding:16px 18px;overflow-y:auto;flex:1}
#mfoot{padding:12px 18px;border-top:1px solid #e5e8f0;display:flex;justify-content:flex-end;gap:8px}
.field{margin-bottom:11px}
.field label{display:block;font-size:11px;font-weight:700;color:#444;margin-bottom:3px;text-transform:uppercase;letter-spacing:.4px}
.field label span{font-weight:400;text-transform:none;color:#999;font-size:10px;letter-spacing:0}
.field input[type=text],.field input[type=number],.field input[type=date],.field select,.field textarea{width:100%;padding:7px 9px;border:1px solid #c5c9d8;border-radius:4px;font-size:13px;font-family:inherit}
.field textarea{min-height:80px;resize:vertical;font-family:monospace;font-size:12px}
.field input:focus,.field textarea:focus{outline:none;border-color:#4472c4;box-shadow:0 0 0 2px rgba(68,114,196,.15)}
.field input:disabled{background:#f3f5fb;color:#888;cursor:not-allowed}
.field.ro label::after{content:" (auto)";font-size:10px;color:#aaa;font-weight:400;text-transform:none}
/* ── Toast */
#toast{position:fixed;bottom:20px;right:20px;background:#27ae60;color:#fff;padding:10px 16px;border-radius:6px;font-size:13px;display:none;z-index:999;box-shadow:0 3px 10px rgba(0,0,0,.2)}
#toast.err{background:#c0392b}
</style>
</head>
<body>
<div id="sidebar">
  <div class="brand">
    <h2>Q&amp;M DB Manager</h2>
    <small>PostgreSQL Admin</small>
  </div>
  <div id="tbl-list"></div>
</div>
<div id="main">
  <div id="toolbar">
    <h1 id="cur-tbl">Select a table</h1>
    <span id="row-count"></span>
    <button class="btn btn-refresh" id="btn-refresh" onclick="loadRows()" style="display:none">&#8635; Refresh</button>
    <button class="btn btn-primary" id="btn-add" onclick="openAdd()" style="display:none">+ Add Row</button>
    <button class="btn btn-clear" id="btn-clear-memory" onclick="clearChatMemory()" style="display:none">Clear Chat Memory</button>
    <button class="btn btn-clear" id="btn-clear-all" onclick="clearAllMemory()">Clear All Memory</button>
  </div>
  <div id="table-wrap">
    <div id="empty">&#8592; Select a table from the sidebar to view and manage data.</div>
    <table id="dtbl" style="display:none"><thead id="thead"></thead><tbody id="tbody"></tbody></table>
  </div>
  <div id="pager" style="display:none">
    <button class="btn" onclick="prevPage()">&#8249; Prev</button>
    <span id="pg-info"></span>
    <button class="btn" onclick="nextPage()">Next &#8250;</button>
    <span style="flex:1"></span>
    <label style="display:flex;align-items:center;gap:6px;font-size:12px">Rows/page:
      <select onchange="state.pageSize=+this.value;state.page=1;loadRows()" style="padding:4px 6px;font-size:12px;border:1px solid #ccc;border-radius:3px">
        <option>25</option><option selected>50</option><option>100</option><option>250</option>
      </select>
    </label>
  </div>
</div>

<div id="mbg" onclick="if(event.target===this)closeModal()">
  <div id="modal">
    <div id="mhead">
      <h2 id="mtitle">Row</h2>
      <button onclick="closeModal()" style="background:none;border:none;font-size:22px;cursor:pointer;color:#666;line-height:1">&times;</button>
    </div>
    <div id="mbody"></div>
    <div id="mfoot">
      <button class="btn btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="saveRow()">Save</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const TABLES = __TABLES_JSON__;
const READ_ONLY = new Set(['id','created_at','updated_at','resolved_at','sent_at','notified_at']);
const state = { table:null, columns:[], pkColumn:'id', rows:[], page:1, pageSize:50, total:0, editId:undefined };

const AUTH = 'Basic ' + btoa('admin:qm-admin');

async function api(path, opts={}) {
  const r = await fetch(path, {
    ...opts,
    headers: { Authorization: AUTH, 'Content-Type': 'application/json', ...(opts.headers||{}) }
  });
  if (!r.ok) {
    let msg;
    try { const e = await r.json(); msg = typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail); }
    catch { msg = r.statusText; }
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

// ── Sidebar init ─────────────────────────────────────────────────────────────
(function init() {
  const list = document.getElementById('tbl-list');
  TABLES.forEach(t => {
    const d = document.createElement('div');
    d.className = 'tbl-item'; d.textContent = t;
    d.onclick = () => selectTable(t);
    list.appendChild(d);
  });
})();

// ── Table selection ───────────────────────────────────────────────────────────
async function selectTable(name) {
  state.table = name; state.page = 1;
  document.querySelectorAll('.tbl-item').forEach(e => e.classList.toggle('active', e.textContent === name));
  document.getElementById('cur-tbl').textContent = name;
  document.getElementById('btn-add').style.display = '';
  document.getElementById('btn-refresh').style.display = '';
  document.getElementById('btn-clear-memory').style.display = name === 'chat_memory' ? '' : 'none';
  try {
    state.columns = await api(`/dbadmin/tables/${name}/columns`);
    // Most tables use 'id' (SERIAL), but conversation_state is keyed on
    // whatsapp_id instead — read the actual PK per table rather than
    // assuming 'id' everywhere (mirrors the backend's _get_pk_column()).
    state.pkColumn = (state.columns.find(c => c.is_pk) || {}).column_name || 'id';
    await loadRows();
  } catch(e) { showToast(e.message, true); }
}

// ── Load & render rows ────────────────────────────────────────────────────────
async function loadRows() {
  if (!state.table) return;
  try {
    const d = await api(`/dbadmin/tables/${state.table}/rows?page=${state.page}&page_size=${state.pageSize}`);
    state.rows = d.rows; state.total = d.total;
    renderTable();
  } catch(e) { showToast(e.message, true); }
}

function renderTable() {
  const {columns: cols, rows, total, page, pageSize} = state;
  document.getElementById('dtbl').style.display = '';
  document.getElementById('empty').style.display = 'none';
  document.getElementById('pager').style.display = '';
  document.getElementById('row-count').textContent = `${total} rows`;

  document.getElementById('thead').innerHTML =
    '<tr>' + cols.map(c => `<th>${h(c.column_name)}</th>`).join('') + '<th>Actions</th></tr>';

  document.getElementById('tbody').innerHTML = rows.length
    ? rows.map(row => {
        const cells = cols.map(c => {
          let v = row[c.column_name];
          if (v === null || v === undefined) return '<td><span style="color:#bbb">null</span></td>';
          if (typeof v === 'object') v = JSON.stringify(v);
          else v = String(v);
          const short = v.length > 55 ? v.slice(0,55)+'…' : v;
          return `<td title="${h(v)}">${h(short)}</td>`;
        }).join('');
        return `<tr>${cells}<td class="act">${rowActions(row)}</td></tr>`;
      }).join('')
    : `<tr><td colspan="99" style="text-align:center;padding:40px;color:#aaa">No rows in this table</td></tr>`;

  const pages = Math.max(1, Math.ceil(total / pageSize));
  document.getElementById('pg-info').textContent = `Page ${page} of ${pages} (${total} total)`;
}

function prevPage() { if (state.page > 1) { state.page--; loadRows(); } }
function nextPage() { if (state.page * state.pageSize < state.total) { state.page++; loadRows(); } }

// ── Modal helpers ─────────────────────────────────────────────────────────────
function fieldHtml(col, val) {
  const n = col.column_name, t = col.data_type, ro = READ_ONLY.has(n);
  const label = `<label class="">${h(n)} <span>(${h(t)}${col.is_nullable==='NO'?' · required':''})</span></label>`;
  let inp;
  if (ro) {
    inp = `<input type="text" name="${n}" value="${h(val??'')}" disabled>`;
  } else if (t === 'boolean') {
    const chk = (val === true || val === 't' || val === 'true') ? 'checked' : '';
    inp = `<label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:400;text-transform:none;font-size:13px">
      <input type="checkbox" name="${n}" ${chk} style="width:16px;height:16px;accent-color:#4472c4"> Enabled</label>`;
  } else if (t === 'jsonb' || t === 'json') {
    const s = val && typeof val === 'object' ? JSON.stringify(val, null, 2) : (val ?? '');
    inp = `<textarea name="${n}" rows="5">${h(String(s))}</textarea>`;
  } else if (t.includes('int')) {
    inp = `<input type="number" step="1" name="${n}" value="${h(String(val??''))}">`;
  } else if (t === 'numeric' || t === 'real' || t.includes('double')) {
    inp = `<input type="number" step="any" name="${n}" value="${h(String(val??''))}">`;
  } else if (t === 'date') {
    inp = `<input type="date" name="${n}" value="${h(val?String(val).slice(0,10):'')}">`;
  } else {
    inp = `<input type="text" name="${n}" value="${h(String(val??''))}">`;
  }
  return `<div class="field${ro?' ro':''}">${label}${inp}</div>`;
}

function openEdit(id) {
  const row = state.rows.find(r => String(r[state.pkColumn]) === String(id));
  if (!row) { showToast('Row not on current page', true); return; }
  state.editId = id;
  document.getElementById('mtitle').textContent = `Edit — ${state.table} #${id}`;
  document.getElementById('mbody').innerHTML = state.columns.map(c => fieldHtml(c, row[c.column_name])).join('');
  document.getElementById('mbg').classList.add('open');
}

function openAdd() {
  state.editId = null;
  document.getElementById('mtitle').textContent = `Add Row — ${state.table}`;
  document.getElementById('mbody').innerHTML =
    state.columns.filter(c => !READ_ONLY.has(c.column_name)).map(c => fieldHtml(c, null)).join('');
  document.getElementById('mbg').classList.add('open');
}

function closeModal() { document.getElementById('mbg').classList.remove('open'); }

async function saveRow() {
  const body = {};
  let validationError = null;

  document.getElementById('mbody').querySelectorAll('input:not([disabled]),textarea,select').forEach(el => {
    if (validationError) return;
    const col = state.columns.find(c => c.column_name === el.name);
    if (!col) return;
    let v;
    if (el.type === 'checkbox') {
      v = el.checked;
    } else if (el.value === '') {
      v = null;
    } else if (col.data_type === 'jsonb' || col.data_type === 'json') {
      try { v = JSON.parse(el.value); }
      catch { validationError = `Invalid JSON in field "${el.name}"`; return; }
    } else if (col.data_type.includes('int')) {
      v = parseInt(el.value, 10);
    } else if (col.data_type === 'numeric' || col.data_type === 'real' || col.data_type.includes('double')) {
      v = parseFloat(el.value);
    } else {
      v = el.value;
    }
    body[el.name] = v;
  });

  if (validationError) { showToast(validationError, true); return; }

  try {
    if (state.editId !== null && state.editId !== undefined) {
      await api(`/dbadmin/tables/${state.table}/rows/${encodeURIComponent(state.editId)}`, { method:'PUT', body:JSON.stringify(body) });
      showToast('Row updated');
    } else {
      await api(`/dbadmin/tables/${state.table}/rows`, { method:'POST', body:JSON.stringify(body) });
      showToast('Row created');
    }
    closeModal(); loadRows();
  } catch(e) { showToast(e.message, true); }
}

async function delRow(id) {
  if (!confirm(`Delete ${state.pkColumn}=${id} from "${state.table}"?\n\nThis cannot be undone.`)) return;
  try {
    await api(`/dbadmin/tables/${state.table}/rows/${encodeURIComponent(id)}`, { method:'DELETE' });
    showToast('Row deleted'); loadRows();
  } catch(e) { showToast(e.message, true); }
}

// ── Per-row action buttons (standard + table-specific) ───────────────────────
function rowActions(row) {
  // JSON.stringify (not a bare template value) so a text PK like
  // whatsapp_id ('6591234561@c.us') embeds as a properly quoted/escaped JS
  // string literal here, not raw identifier syntax — only 'id' (SERIAL)
  // PKs happened to work unquoted before conversation_state existed.
  const pkArg = JSON.stringify(row[state.pkColumn]);
  let btns = `<button class="btn btn-sm btn-edit" onclick="openEdit(${pkArg})">Edit</button> `
            + `<button class="btn btn-sm btn-del"  onclick="delRow(${pkArg})">Del</button>`;

  if (state.table === 'enrollments' && row.status !== 'cancelled') {
    const name = h(row.full_name || String(row.id));
    btns += ` <button class="btn btn-sm btn-cancel-enroll"
                onclick="cancelEnrollment(${row.id},'${name}')">Cancel</button>`;
  }

  if (state.table === 'credit_notes' && row.status === 'requested') {
    btns += ` <button class="btn btn-sm btn-approve" onclick="approveCreditNote(${row.id})">Approve</button>`
           + ` <button class="btn btn-sm btn-reject"  onclick="rejectCreditNote(${row.id})">Reject</button>`;
  }

  return btns;
}

// ── Business actions ──────────────────────────────────────────────────────────
async function cancelEnrollment(id, name) {
  const reason = prompt(`Cancel enrollment #${id} — ${name}\n\nReason:`, 'Cancellation');
  if (reason === null) return;
  try {
    const res = await api(`/dbadmin/enrollments/${id}/cancel?reason=${encodeURIComponent(reason)}`, {method:'POST'});
    const cn = res.credit_note;
    showToast(`Credit note ${cn?.credit_note_no || ''} created. Enrollment cancelled. Accounts notified.`);
    loadRows();
  } catch(e) { showToast(e.message, true); }
}

async function approveCreditNote(id) {
  if (!confirm(`Approve credit note #${id}?\n\nThe participant will be emailed the PDF shortly (a background job, not immediate).`)) return;
  try {
    await api(`/dbadmin/credit-notes/${id}/approve`, {method:'POST'});
    showToast('Approved — the participant will be emailed shortly.');
    loadRows();
  } catch(e) { showToast(e.message, true); }
}

async function clearChatMemory() {
  if (!confirm(`Delete ALL chat_memory rows (${state.total} total)?\n\n`
             + `This wipes conversation history/context for every user. This cannot be undone.`)) return;
  try {
    const res = await api('/dbadmin/chat-memory/all', { method: 'DELETE' });
    showToast(`Deleted ${res.deleted} chat_memory rows.`);
    state.page = 1; loadRows();
  } catch(e) { showToast(e.message, true); }
}

const WIPE_TABLES = TABLES.filter(t => t !== 'courses' && t !== 'course_schedules');

async function clearAllMemory() {
  const list = WIPE_TABLES.join(', ');
  if (!confirm(`Wipe ALL data from every table EXCEPT the course catalog?\n\n`
             + `Tables: ${list}\n\n`
             + `This deletes every customer, lead, reminder, enrollment, payment, chat history, `
             + `staff queue item, and credit note — for ALL participants. courses/course_schedules `
             + `are left untouched. This cannot be undone.`)) return;
  if (!confirm(`Really sure? This is a full reset of all participant/transactional data.`)) return;
  try {
    const res = await api('/dbadmin/all-memory', { method: 'DELETE' });
    showToast(`Wiped ${res.total} rows across ${WIPE_TABLES.length} tables.`);
    state.page = 1; loadRows();
  } catch(e) { showToast(e.message, true); }
}

async function rejectCreditNote(id) {
  if (!confirm(`Reject credit note #${id}?\n\nNo email will be sent — contact the participant manually.`)) return;
  try {
    await api(`/dbadmin/credit-notes/${id}/reject`, {method:'POST'});
    showToast('Credit note rejected.');
    loadRows();
  } catch(e) { showToast(e.message, true); }
}

function h(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

let _tt;
function showToast(msg, isErr=false) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = isErr ? 'err' : '';
  t.style.display = 'block';
  clearTimeout(_tt); _tt = setTimeout(() => t.style.display='none', 3500);
}
</script>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
def dashboard(_: str = Depends(_auth)) -> str:
    return _HTML.replace("__TABLES_JSON__", json.dumps(MANAGED_TABLES))
