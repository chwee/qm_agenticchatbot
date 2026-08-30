# Migrating the database from `supabase_export.sql` to Supabase

Step-by-step guide for pushing the local Postgres schema + data onto a hosted
Supabase project, and switching the backend to use it. Written from the exact
steps (and mistakes) that worked when this was done for this project.

Related: [`backend/db/supabase_export.sql`](../backend/db/supabase_export.sql)
(the generated export file), [`backend/db/schema.sql`](../backend/db/schema.sql)
(source of truth for the schema half of the export).

---

## 1. Regenerate `supabase_export.sql` from the local database

`backend/db/supabase_export.sql` is not hand-written — it's schema.sql's
content plus a fresh data dump of whatever's currently in the local Docker
Postgres, concatenated together. Regenerate it before every migration so
Supabase ends up with current data, not a stale snapshot.

```powershell
docker exec qm_postgres pg_dump -U qm_user -d qm_enrollment `
  --data-only --no-owner --column-inserts > data_only.sql
```

Two flags matter here, both learned the hard way (see §4):

- **`--column-inserts`** — without it, `pg_dump` emits `COPY ... FROM stdin`
  blocks. That format only works through the `psql` client, which
  understands the special copy-data protocol; it is **not valid SQL** and
  fails immediately if pasted into the Supabase SQL Editor. `--column-inserts`
  makes `pg_dump` emit plain `INSERT INTO table (col1, col2, ...) VALUES (...)`
  statements instead, which run anywhere.
- **No `--disable-triggers`** — this flag looks like the "safe" choice for a
  data-only dump (it wraps each table in `ALTER TABLE ... DISABLE/ENABLE
  TRIGGER ALL` so rows can load out of foreign-key order), but Supabase's SQL
  Editor connects as a non-superuser role that isn't allowed to touch the
  internal FK-constraint triggers a table's `REFERENCES` clauses create. Using
  it causes `permission denied: "RI_ConstraintTrigger_..." is a system
  trigger`. Leave it off and rely on `pg_dump`'s natural table ordering
  instead — see step 2.

Then strip the `\restrict` / `\unrestrict` lines newer `pg_dump` versions add
(another psql-only meta-command, invalid outside `psql`):

```powershell
findstr /v /b "\restrict \unrestrict" data_only.sql > data_only_clean.sql
```

(On the Bash tool used during this project's own migration, the equivalent
was `grep -v -F '\restrict' | grep -v -F '\unrestrict'`.)

Finally, concatenate the schema (idempotent `CREATE TABLE IF NOT EXISTS`
throughout) with the cleaned data dump into the export file:

```
backend/db/supabase_export.sql =
    header comment block
  + full contents of backend/db/schema.sql
  + "-- Data" section header
  + data_only_clean.sql
```

## 2. Verify the data section's table order is foreign-key-safe

Since triggers aren't disabled, tables must be inserted in an order where
every row's foreign keys already exist. `pg_dump`'s default ordering already
satisfies this for this schema — confirm it rather than assume it, since a
schema change (a new `REFERENCES` clause) could break the ordering later:

```
chat_memory          (no FK)
customers            (no FK)
conversation_state   (FK → customers, already defined above)  ✓
courses               (no FK)
course_schedules     (FK → courses, already defined above)     ✓
enrollments          (FK → courses, course_schedules)          ✓
credit_notes         (FK → enrollments)                        ✓
leads                (no FK)
payments             (FK → enrollments)                        ✓
reminders            (FK → customers)                          ✓
staff_queue          (no FK)
```

If a future schema change adds tables/FKs, re-check this order.

## 3. Create the Supabase project (skip if you already have one)

1. Go to [supabase.com/dashboard](https://supabase.com/dashboard) → **New
   Project**. Pick a name, region, and database password.
2. Wait ~2 minutes for provisioning.

## 4. Run the export against Supabase

Two options:

**Option A — Supabase SQL Editor (used for this project):**

1. Open the project → **SQL Editor**.
2. Paste the full contents of `backend/db/supabase_export.sql`.
3. Run.

**Option B — `psql` from the command line** (works too, and additionally
tolerates the raw `pg_dump` COPY-format output if you ever skip
`--column-inserts`, since `psql` natively understands it):

```powershell
psql "postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres" `
  -f backend/db/supabase_export.sql
```

### If it errors partway through

Schema statements (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`)
are idempotent — re-running them is harmless. Data `INSERT` statements are
**not** idempotent; if a run fails partway through, some rows may already be
committed. Before re-running, clear whatever partially loaded:

```sql
TRUNCATE TABLE chat_memory, customers, conversation_state, courses,
  course_schedules, enrollments, credit_notes, leads, payments,
  reminders, staff_queue RESTART IDENTITY CASCADE;
```

Then paste and run the export again.

### Errors actually hit while building this guide, and their fixes

| Error | Cause | Fix |
|---|---|---|
| `syntax error at or near "1"` on a raw tab-separated data line | `pg_dump`'s default `COPY ... FROM stdin` format pasted into the SQL Editor, which only executes plain SQL | Regenerate with `--column-inserts` (§1) |
| `permission denied: "RI_ConstraintTrigger_..." is a system trigger` | `--disable-triggers` tries to disable FK-constraint triggers; Supabase's SQL Editor role isn't a superuser | Drop `--disable-triggers`; verify insert order is FK-safe instead (§2) |

## 5. Get the Supabase connection string

1. **Project Settings** (gear icon) → **Database** → **Connection string**.
2. Choose **Session pooler** (recommended for this app — a long-lived
   connection pool, not per-request serverless).
3. Replace `[YOUR-PASSWORD]` with your actual database password (or use
   **Reset database password** on the same page if forgotten).
4. Append `?sslmode=require` — Supabase's pooler enforces TLS, and psycopg
   needs this explicit in the URL.

Result looks like:

```
postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

If the password contains `@`, `:`, `/`, or `#`, percent-encode it (or copy
the string directly from Supabase's dashboard, which already encodes it).

## 6. Point the backend at Supabase

Edit `backend/.env`:

```
DB_PROVIDER=supabase
DATABASE_URL_SUPABASE=postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

`DB_PROVIDER` is what actually switches the backend over — filling in
`DATABASE_URL_SUPABASE` alone doesn't activate it (`DATABASE_URL_DOCKER` stays
saved for switching back later). See `backend/app/config.py`'s
`resolved_database_url` property for the exact precedence rules.

## 7. Restart and verify

```powershell
cd backend
.venv\Scripts\python run.py
```

```powershell
curl http://localhost:8000/health
```

Expect:

```json
{"status": "ok", "db": true, "db_provider": "supabase", "openai_configured": true}
```

Then spot-check the data landed correctly, either via `/dbadmin/` (`admin` /
`qm-admin`) or directly:

```sql
select 'courses', count(*) from courses
union all select 'customers', count(*) from customers
union all select 'course_schedules', count(*) from course_schedules
union all select 'chat_memory', count(*) from chat_memory;
```

Row counts should match whatever the local database had at export time.

## 8. Switching back to Docker later

Nothing to undo — both connection strings stay saved in `.env`. Just flip:

```
DB_PROVIDER=docker
```

and restart the backend (with `docker compose up -d` run first if the
container isn't already up).
