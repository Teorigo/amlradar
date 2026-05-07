import sqlite3
import datetime
from config import DB_PATH, REGISTRY_COUNTRY


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            registry     TEXT NOT NULL,
            entry_id     TEXT NOT NULL,
            name         TEXT,
            status       TEXT,
            url          TEXT,
            country      TEXT,
            first_seen   TEXT NOT NULL,
            last_seen    TEXT NOT NULL,
            website      TEXT,
            email        TEXT,
            phone        TEXT,
            enriched_at  TEXT,
            PRIMARY KEY (registry, entry_id)
        );

        CREATE TABLE IF NOT EXISTS deltas (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            registry     TEXT NOT NULL,
            entry_id     TEXT NOT NULL,
            entry_name   TEXT,
            change_type  TEXT NOT NULL,
            old_status   TEXT,
            new_status   TEXT,
            old_name     TEXT,
            new_name     TEXT,
            country      TEXT,
            detected_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at         TEXT NOT NULL,
            entries_total  INTEGER DEFAULT 0,
            deltas_found   INTEGER DEFAULT 0,
            errors         TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_deltas_registry ON deltas(registry);
        CREATE INDEX IF NOT EXISTS idx_deltas_detected ON deltas(detected_at);
    """)
    # Add columns to existing DBs gracefully
    for col, typedef in [
        ('country',     'TEXT'),
        ('website',     'TEXT'),
        ('email',       'TEXT'),
        ('phone',       'TEXT'),
        ('enriched_at', 'TEXT'),
    ]:
        try:
            conn.execute(f'ALTER TABLE entries ADD COLUMN {col} {typedef}')
        except Exception:
            pass
    try:
        conn.execute('ALTER TABLE deltas ADD COLUMN country TEXT')
    except Exception:
        pass
    conn.commit()
    conn.close()


def upsert_entries(registry: str, fresh: list[dict]) -> list[dict]:
    """Diff fresh entries against DB snapshot. Returns delta list; updates entries in place."""
    now             = datetime.datetime.utcnow().isoformat()
    default_country = REGISTRY_COUNTRY.get(registry, '')
    conn = get_conn()
    cur  = conn.cursor()

    cur.execute(
        "SELECT entry_id, name, status, url, country FROM entries WHERE registry = ?",
        (registry,)
    )
    existing = {row['entry_id']: dict(row) for row in cur.fetchall()}

    fresh_ids = {e['entry_id'] for e in fresh}
    deltas: list[dict] = []

    for entry in fresh:
        eid     = entry['entry_id']
        name    = entry.get('name', '')
        status  = entry.get('status', '')
        url     = entry.get('url', '')
        country = entry.get('country', default_country)

        if eid not in existing:
            deltas.append({
                'registry': registry, 'entry_id': eid, 'entry_name': name,
                'change_type': 'added', 'old_status': None, 'new_status': status,
                'old_name': None, 'new_name': name, 'country': country,
                'detected_at': now,
            })
            cur.execute(
                "INSERT INTO entries "
                "(registry, entry_id, name, status, url, country, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (registry, eid, name, status, url, country, now, now)
            )
        else:
            old     = existing[eid]
            changed = (old['status'] != status) or (old['name'] != name)
            if changed:
                deltas.append({
                    'registry': registry, 'entry_id': eid, 'entry_name': name,
                    'change_type': 'changed',
                    'old_status': old['status'], 'new_status': status,
                    'old_name': old['name'],      'new_name': name,
                    'country': old.get('country') or country,
                    'detected_at': now,
                })
            cur.execute(
                "UPDATE entries SET name=?, status=?, url=?, country=?, last_seen=? "
                "WHERE registry=? AND entry_id=?",
                (name, status, url, country or old.get('country'), now, registry, eid)
            )

    for eid, old in existing.items():
        if eid not in fresh_ids:
            deltas.append({
                'registry': registry, 'entry_id': eid, 'entry_name': old['name'],
                'change_type': 'removed', 'old_status': old['status'], 'new_status': None,
                'old_name': old['name'], 'new_name': None,
                'country': old.get('country') or default_country,
                'detected_at': now,
            })
            cur.execute(
                "DELETE FROM entries WHERE registry=? AND entry_id=?", (registry, eid)
            )

    for d in deltas:
        cur.execute(
            "INSERT INTO deltas (registry, entry_id, entry_name, change_type, "
            "old_status, new_status, old_name, new_name, country, detected_at) "
            "VALUES (:registry, :entry_id, :entry_name, :change_type, "
            ":old_status, :new_status, :old_name, :new_name, :country, :detected_at)",
            d
        )

    conn.commit()
    conn.close()
    return deltas


def log_run(entries_total: int, deltas_found: int, errors: str = ''):
    conn = get_conn()
    conn.execute(
        "INSERT INTO runs (run_at, entries_total, deltas_found, errors) VALUES (?, ?, ?, ?)",
        (datetime.datetime.utcnow().isoformat(), entries_total, deltas_found, errors)
    )
    conn.commit()
    conn.close()


def get_recent_deltas(hours: int = 25) -> list[dict]:
    since = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).isoformat()
    conn  = get_conn()
    cur   = conn.cursor()
    cur.execute(
        "SELECT * FROM deltas WHERE detected_at >= ? ORDER BY registry, change_type, entry_name",
        (since,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_entry_count() -> int:
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM entries")
    n = cur.fetchone()[0]
    conn.close()
    return n


def update_enrichment(registry: str, entry_id: str, data: dict):
    now  = datetime.datetime.utcnow().isoformat()
    conn = get_conn()
    conn.execute(
        "UPDATE entries SET website=?, email=?, phone=?, enriched_at=? "
        "WHERE registry=? AND entry_id=?",
        (data.get('website'), data.get('email'), data.get('phone'), now, registry, entry_id)
    )
    conn.commit()
    conn.close()


def get_enrichment(registry: str, entry_id: str):
    """Return cached enrichment if enriched within last 30 days, else None."""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT website, email, phone, enriched_at FROM entries "
        "WHERE registry=? AND entry_id=?",
        (registry, entry_id)
    )
    row = cur.fetchone()
    conn.close()
    if not row or not row['enriched_at']:
        return None
    try:
        enriched = datetime.datetime.fromisoformat(row['enriched_at'])
        if (datetime.datetime.utcnow() - enriched).days > 30:
            return None
    except Exception:
        return None
    return dict(row)
