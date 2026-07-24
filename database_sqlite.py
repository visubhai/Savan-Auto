"""
Database layer using SQLite.
Single file: savan.db
Tables: users, settings, templates, customers, messages, batches, scheduled_jobs
"""
import sqlite3
import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "savan.db"


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def hash_password(password, salt=None):
    """Hash password with salt using SHA-256 (good enough for localhost)."""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${h}"


def verify_password(password, hashed):
    """Verify password against stored hash."""
    try:
        salt, _ = hashed.split("$", 1)
        return hash_password(password, salt) == hashed
    except (ValueError, AttributeError):
        return False


def init_db():
    """Create all tables. Safe to run multiple times."""
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'member',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            language TEXT NOT NULL DEFAULT 'en',
            category TEXT NOT NULL DEFAULT 'UTILITY',
            body TEXT NOT NULL,
            variable_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'unknown',
            is_default INTEGER NOT NULL DEFAULT 0,
            synced_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes'))
        );

        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE NOT NULL,
            name TEXT,
            last_route TEXT,
            last_platform TEXT,
            total_messages INTEGER NOT NULL DEFAULT 0,
            last_messaged_at TEXT,
            opted_out INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes'))
        );

        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            template_name TEXT,
            total_count INTEGER NOT NULL DEFAULT 0,
            sent_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            started_by INTEGER,
            started_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes')),
            completed_at TEXT,
            FOREIGN KEY (started_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,
            customer_phone TEXT NOT NULL,
            customer_name TEXT,
            route TEXT,
            platform TEXT,
            template_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            error_message TEXT,
            wa_message_id TEXT,
            sent_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes')),
            FOREIGN KEY (batch_id) REFERENCES batches(id)
        );

        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            csv_data TEXT NOT NULL,
            template_name TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_by INTEGER,
            batch_id INTEGER,
            var_overrides TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes')),
            FOREIGN KEY (created_by) REFERENCES users(id),
            FOREIGN KEY (batch_id) REFERENCES batches(id)
        );

        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            template_name TEXT NOT NULL,
            variables TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes')),
            last_used_at TEXT,
            header_media_id TEXT,
            header_image_uploaded_at TEXT,
            header_image_size INTEGER,
            header_image_mime TEXT
        );

        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            customer_name TEXT,
            direction TEXT NOT NULL,
            message_type TEXT NOT NULL DEFAULT 'text',
            content TEXT NOT NULL,
            wa_message_id TEXT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes')),
            read INTEGER NOT NULL DEFAULT 0,
            status TEXT,
            media_path TEXT,
            mime_type TEXT,
            filename TEXT,
            storage_kind TEXT DEFAULT 'local'
        );

        CREATE TABLE IF NOT EXISTS auto_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enabled INTEGER NOT NULL DEFAULT 1,
            trigger_label TEXT NOT NULL,
            keywords TEXT NOT NULL DEFAULT '',
            response_text TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            show_in_menu INTEGER NOT NULL DEFAULT 0,
            menu_order INTEGER NOT NULL DEFAULT 0,
            flow_kind TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes'))
        );

        -- Bus routes (origin → destination pairs)
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes'))
        );

        -- A bus departure on a route (route + time + bus type + fare)
        CREATE TABLE IF NOT EXISTS bus_trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id INTEGER NOT NULL,
            departure_time TEXT NOT NULL,         -- 'HH:MM' 24-hour
            bus_type TEXT NOT NULL DEFAULT '',    -- 'AC Sleeper', 'Semi-Sleeper', etc.
            fare INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes')),
            FOREIGN KEY (route_id) REFERENCES routes(id)
        );

        -- In-progress booking conversations. One row per (phone, active session).
        -- Expires after 1 hour of inactivity so abandoned sessions don't stick.
        CREATE TABLE IF NOT EXISTS booking_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            state TEXT NOT NULL,                  -- 'ask_origin' | 'ask_destination' | 'ask_date' | 'ask_trip' | 'ask_seats' | 'confirm'
            origin TEXT,
            destination TEXT,
            route_id INTEGER,
            trip_id INTEGER,
            travel_date TEXT,                     -- 'YYYY-MM-DD'
            seats INTEGER,
            customer_name TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes'))
        );
        CREATE INDEX IF NOT EXISTS idx_booking_sessions_phone ON booking_sessions(phone);

        -- Final booking requests. Operator confirms or cancels via /bookings.
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pnr TEXT NOT NULL UNIQUE,
            phone TEXT NOT NULL,
            customer_name TEXT,
            route_id INTEGER,
            origin TEXT,                          -- snapshot — route may change later
            destination TEXT,
            trip_id INTEGER,
            departure_time TEXT,                  -- snapshot
            bus_type TEXT,                        -- snapshot
            travel_date TEXT,                     -- 'YYYY-MM-DD'
            seats INTEGER NOT NULL DEFAULT 1,
            fare_per_seat INTEGER NOT NULL DEFAULT 0,
            total_fare INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'confirmed' | 'cancelled'
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', '+330 minutes'))
        );
        CREATE INDEX IF NOT EXISTS idx_bookings_phone ON bookings(phone);
        CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);

        CREATE INDEX IF NOT EXISTS idx_messages_batch ON messages(batch_id);
        CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(customer_phone);
        CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
        CREATE INDEX IF NOT EXISTS idx_messages_sent ON messages(sent_at);
        CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone);
        CREATE INDEX IF NOT EXISTS idx_chats_phone ON chats(phone);
        CREATE INDEX IF NOT EXISTS idx_chats_ts ON chats(timestamp);
        """)

    # Lightweight migration: add var_overrides column to older scheduled_jobs tables
    with get_db() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(scheduled_jobs)").fetchall()}
        if "var_overrides" not in cols:
            db.execute("ALTER TABLE scheduled_jobs ADD COLUMN var_overrides TEXT DEFAULT ''")

    # Migration: add media columns to older chats tables
    with get_db() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(chats)").fetchall()}
        for col in ("media_path", "mime_type", "filename"):
            if col not in cols:
                db.execute(f"ALTER TABLE chats ADD COLUMN {col} TEXT")
        if "storage_kind" not in cols:
            db.execute("ALTER TABLE chats ADD COLUMN storage_kind TEXT DEFAULT 'local'")
        if "raw_payload" not in cols:
            db.execute("ALTER TABLE chats ADD COLUMN raw_payload TEXT")

    # Migration: add header columns to older templates tables
    with get_db() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(templates)").fetchall()}
        for col in ("header_type", "header_example", "header_media_id", "buttons",
                    "header_image_uploaded_at", "header_image_mime"):
            if col not in cols:
                db.execute(f"ALTER TABLE templates ADD COLUMN {col} TEXT")
        if "header_image_is_custom" not in cols:
            db.execute("ALTER TABLE templates ADD COLUMN header_image_is_custom INTEGER DEFAULT 0")
        if "header_image_size" not in cols:
            db.execute("ALTER TABLE templates ADD COLUMN header_image_size INTEGER")

    # Migration: add per-campaign header-image override columns. One approved
    # WhatsApp template can serve many use-cases (Diwali, Monsoon, Holiday…);
    # each campaign can store its own banner media_id which overrides the
    # template's default at send time.
    with get_db() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(campaigns)").fetchall()}
        for col in ("header_media_id", "header_image_uploaded_at", "header_image_mime"):
            if col not in cols:
                db.execute(f"ALTER TABLE campaigns ADD COLUMN {col} TEXT")
        if "header_image_size" not in cols:
            db.execute("ALTER TABLE campaigns ADD COLUMN header_image_size INTEGER")

    # Migration: add description column to auto_replies (shown under each row
    # in WhatsApp list messages — 4-10 menu items render as a list, not buttons).
    with get_db() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(auto_replies)").fetchall()}
        if "description" not in cols:
            db.execute("ALTER TABLE auto_replies ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        # flow_kind marks the menu item as a multi-step conversation starter
        # rather than a static text reply. Empty = behave as before.
        if "flow_kind" not in cols:
            db.execute("ALTER TABLE auto_replies ADD COLUMN flow_kind TEXT NOT NULL DEFAULT ''")

    # Seed default admin if no users exist
    with get_db() as db:
        cur = db.execute("SELECT COUNT(*) as c FROM users")
        if cur.fetchone()["c"] == 0:
            db.execute(
                "INSERT INTO users (username, password_hash, display_name, role) VALUES (?,?,?,?)",
                ("admin", hash_password("savan123"), "Hitesh (Admin)", "admin"),
            )

    # Seed default settings if missing
    defaults = {
        "access_token": "",
        "phone_number_id": "1157447707452254",
        "waba_id": "2016713718952871",
        "api_version": "v19.0",
        "delay_between_messages": "1.5",
        "max_retries": "3",
        "cost_per_message": "0.12",
        "wapsolution_monthly_cost": "2000",
        "business_name": "Savan Travels",
        "webhook_verify_token": secrets.token_hex(16),
    }
    with get_db() as db:
        for k, v in defaults.items():
            db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )

    # Seed default template if missing
    with get_db() as db:
        cur = db.execute("SELECT COUNT(*) as c FROM templates")
        if cur.fetchone()["c"] == 0:
            db.execute(
                """INSERT INTO templates (name, language, category, body, variable_count, status, is_default)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "journey_reminder",
                    "en",
                    "UTILITY",
                    "Hello {{1}},\n\nMain Savan Travels se bol raha hoon. Aapne {{2}} travel kiya tha.\n\nAapko {{3}} ki taraf se rating/review link prapt hua hoga. Kripya apna feedback share karein.\n\nReview submit karne ke baad screenshot bhejne ki bhi kripya karein.\n\nDhanyavaad 🙏",
                    3,
                    "approved",
                    1,
                ),
            )


# ---------------- Settings helpers ----------------
def get_setting(key, default=""):
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_all_settings():
    with get_db() as db:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


# ---------------- User helpers ----------------
def get_user_by_username(username):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()


def get_user_by_id(user_id):
    with get_db() as db:
        return db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def create_user(username, password, display_name, role="member"):
    with get_db() as db:
        db.execute(
            "INSERT INTO users (username, password_hash, display_name, role) VALUES (?,?,?,?)",
            (username, hash_password(password), display_name, role),
        )


def list_users():
    with get_db() as db:
        return db.execute(
            "SELECT id, username, display_name, role, created_at FROM users ORDER BY id"
        ).fetchall()


# ---------------- Template helpers ----------------
def get_templates():
    with get_db() as db:
        return db.execute(
            "SELECT * FROM templates ORDER BY is_default DESC, name"
        ).fetchall()


def get_default_template():
    with get_db() as db:
        return db.execute(
            "SELECT * FROM templates WHERE is_default=1 LIMIT 1"
        ).fetchone()


def get_template_by_name(name):
    with get_db() as db:
        return db.execute("SELECT * FROM templates WHERE name=?", (name,)).fetchone()


def upsert_template(name, language, category, body, variable_count, status,
                     header_type=None, header_example=None, header_media_id=None,
                     buttons=None):
    with get_db() as db:
        existing = db.execute("SELECT id FROM templates WHERE name=?", (name,)).fetchone()
        if existing:
            db.execute(
                """UPDATE templates SET language=?, category=?, body=?,
                   variable_count=?, status=?,
                   header_type=?, header_example=?, header_media_id=?, buttons=?,
                   synced_at=datetime('now', '+330 minutes')
                   WHERE name=?""",
                (language, category, body, variable_count, status,
                 header_type, header_example, header_media_id, buttons, name),
            )
        else:
            db.execute(
                """INSERT INTO templates (name, language, category, body,
                   variable_count, status, header_type, header_example,
                   header_media_id, buttons, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           datetime('now', '+330 minutes'))""",
                (name, language, category, body, variable_count, status,
                 header_type, header_example, header_media_id, buttons),
            )


def update_template_meta(name, **fields):
    """Update arbitrary columns on a template row. Silently ignores
    fields that don't exist as columns yet — keeps the SQLite path
    forward-compatible with MongoDB-only fields."""
    clean = {k: v for k, v in fields.items() if v is not None}
    if not clean:
        return
    with get_db() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(templates)").fetchall()}
        valid = {k: v for k, v in clean.items() if k in cols}
        if not valid:
            return
        sets   = ", ".join(f"{k}=?" for k in valid)
        params = list(valid.values()) + [name]
        db.execute(f"UPDATE templates SET {sets} WHERE name=?", params)


def set_default_template(name):
    with get_db() as db:
        db.execute("UPDATE templates SET is_default=0")
        db.execute("UPDATE templates SET is_default=1 WHERE name=?", (name,))


# ---------------- Customer helpers ----------------
def upsert_customer(phone, name, route, platform):
    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM customers WHERE phone=?", (phone,)
        ).fetchone()
        if existing:
            db.execute(
                """UPDATE customers SET name=COALESCE(?, name),
                   last_route=?, last_platform=?,
                   total_messages=total_messages+1,
                   last_messaged_at=datetime('now', '+330 minutes')
                   WHERE phone=?""",
                (name, route, platform, phone),
            )
        else:
            db.execute(
                """INSERT INTO customers (phone, name, last_route, last_platform,
                   total_messages, last_messaged_at)
                   VALUES (?, ?, ?, ?, 1, datetime('now', '+330 minutes'))""",
                (phone, name, route, platform),
            )


def search_customers(query="", opted_out=None, limit=100, offset=0):
    sql = "SELECT * FROM customers WHERE 1=1"
    params = []
    if query:
        sql += " AND (name LIKE ? OR phone LIKE ?)"
        params += [f"%{query}%", f"%{query}%"]
    if opted_out is not None:
        sql += " AND opted_out = ?"
        params.append(1 if opted_out else 0)
    sql += " ORDER BY last_messaged_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_db() as db:
        return db.execute(sql, params).fetchall()


def count_customers(query="", opted_out=None):
    sql = "SELECT COUNT(*) as c FROM customers WHERE 1=1"
    params = []
    if query:
        sql += " AND (name LIKE ? OR phone LIKE ?)"
        params += [f"%{query}%", f"%{query}%"]
    if opted_out is not None:
        sql += " AND opted_out = ?"
        params.append(1 if opted_out else 0)
    with get_db() as db:
        return db.execute(sql, params).fetchone()["c"]


def toggle_opt_out(phone, opted_out):
    with get_db() as db:
        db.execute(
            "UPDATE customers SET opted_out=? WHERE phone=?",
            (1 if opted_out else 0, phone),
        )


# ---------------- Batch & message helpers ----------------
def create_batch(name, template_name, total_count, started_by):
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO batches (name, template_name, total_count, started_by, status)
               VALUES (?, ?, ?, ?, 'running')""",
            (name, template_name, total_count, started_by),
        )
        return cur.lastrowid


def update_batch_counts(batch_id, sent=None, failed=None):
    with get_db() as db:
        if sent is not None:
            db.execute(
                "UPDATE batches SET sent_count = sent_count + ? WHERE id=?",
                (sent, batch_id),
            )
        if failed is not None:
            db.execute(
                "UPDATE batches SET failed_count = failed_count + ? WHERE id=?",
                (failed, batch_id),
            )


def complete_batch(batch_id):
    with get_db() as db:
        db.execute(
            "UPDATE batches SET status='completed', completed_at=datetime('now', '+330 minutes') WHERE id=?",
            (batch_id,),
        )


def get_batch(batch_id):
    with get_db() as db:
        return db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()


def list_batches(limit=50):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM batches ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()


def log_message(batch_id, phone, name, route, platform, template_name,
                status, error=None, wa_msg_id=None):
    with get_db() as db:
        db.execute(
            """INSERT INTO messages (batch_id, customer_phone, customer_name,
               route, platform, template_name, status, error_message,
               wa_message_id, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+330 minutes'))""",
            (batch_id, phone, name, route, platform, template_name,
             status, error, wa_msg_id),
        )


def search_messages(query="", status=None, days=None, limit=100, offset=0):
    sql = "SELECT * FROM messages WHERE 1=1"
    params = []
    if query:
        sql += " AND (customer_name LIKE ? OR customer_phone LIKE ?)"
        params += [f"%{query}%", f"%{query}%"]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if days:
        sql += f" AND sent_at >= datetime('now', '+330 minutes', '-{int(days)} days')"
    sql += " ORDER BY sent_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_db() as db:
        return db.execute(sql, params).fetchall()


def count_messages(query="", status=None, days=None):
    sql = "SELECT COUNT(*) as c FROM messages WHERE 1=1"
    params = []
    if query:
        sql += " AND (customer_name LIKE ? OR customer_phone LIKE ?)"
        params += [f"%{query}%", f"%{query}%"]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if days:
        sql += f" AND sent_at >= datetime('now', '+330 minutes', '-{int(days)} days')"
    with get_db() as db:
        return db.execute(sql, params).fetchone()["c"]


def get_batch_messages(batch_id):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM messages WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()


def get_failed_messages(batch_id):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM messages WHERE batch_id=? AND status='failed' ORDER BY id",
            (batch_id,),
        ).fetchall()


def get_phone_messages(phone, limit=200):
    """Bulk/template messages sent to this phone, oldest first.
    Used in the inbox to show template sends alongside chat messages.
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM messages WHERE customer_phone=? ORDER BY sent_at DESC LIMIT ?",
            (phone, limit),
        ).fetchall()
        return rows[::-1]


def get_today_conversations():
    """Count of UNIQUE customer phones we successfully sent to today (IST)."""
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(DISTINCT customer_phone) AS c FROM messages "
            "WHERE status='sent' "
            "AND date(sent_at)=date('now', '+330 minutes')"
        ).fetchone()
        return row["c"] if row else 0


def get_unique_conversations(days=7):
    """Count of UNIQUE customer phones we sent to in the last `days` days.

    Meta upgrades the tier when this reaches the next tier's size with
    quality staying GREEN.
    """
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(DISTINCT customer_phone) AS c FROM messages "
            "WHERE status='sent' "
            "AND sent_at >= datetime('now', '+330 minutes', ?)",
            (f"-{int(days)} days",),
        ).fetchone()
        return row["c"] if row else 0


def get_recent_recipients(days=30, template_name=None, status="sent"):
    """One row per unique phone we've successfully sent to in the last
    `days` days, with the latest message's metadata.
    """
    sql = (
        "SELECT customer_phone AS phone, "
        "       MAX(sent_at)     AS sent_at, "
        "       customer_name    AS name, "
        "       route, platform, template_name "
        "FROM messages "
        "WHERE sent_at >= datetime('now', '+330 minutes', ?) "
    )
    params = [f"-{int(days)} days"]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if template_name:
        sql += " AND template_name = ?"
        params.append(template_name)
    sql += " GROUP BY customer_phone ORDER BY sent_at DESC"
    with get_db() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


# ---------------- Dashboard stats ----------------
def get_today_stats():
    with get_db() as db:
        sent = db.execute(
            "SELECT COUNT(*) as c FROM messages WHERE status='sent' AND date(sent_at)=date('now', '+330 minutes')"
        ).fetchone()["c"]
        failed = db.execute(
            "SELECT COUNT(*) as c FROM messages WHERE status='failed' AND date(sent_at)=date('now', '+330 minutes')"
        ).fetchone()["c"]
    cost_per = float(get_setting("cost_per_message", "0.12"))
    return {"sent": sent, "failed": failed, "cost": round(sent * cost_per, 2)}


def get_month_stats():
    with get_db() as db:
        sent = db.execute(
            "SELECT COUNT(*) as c FROM messages WHERE status='sent' "
            "AND strftime('%Y-%m', sent_at) = strftime('%Y-%m', 'now', '+330 minutes')"
        ).fetchone()["c"]
        failed = db.execute(
            "SELECT COUNT(*) as c FROM messages WHERE status='failed' "
            "AND strftime('%Y-%m', sent_at) = strftime('%Y-%m', 'now', '+330 minutes')"
        ).fetchone()["c"]
    cost_per = float(get_setting("cost_per_message", "0.12"))
    wap_cost = float(get_setting("wapsolution_monthly_cost", "2000"))
    cost = round(sent * cost_per, 2)
    return {
        "sent": sent,
        "failed": failed,
        "cost": cost,
        "saved": round(wap_cost - cost, 2),
        "wap_cost": wap_cost,
    }


def get_chart_data(days=7):
    """Return per-day counts for the last N days for chart."""
    with get_db() as db:
        rows = db.execute(
            f"""SELECT date(sent_at) as day,
                       SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) as sent,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
                FROM messages
                WHERE sent_at >= date('now', '+330 minutes', '-{int(days)} days')
                GROUP BY date(sent_at)
                ORDER BY day"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_sends(limit=10):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def get_top_routes(limit=5):
    with get_db() as db:
        return db.execute(
            """SELECT route, COUNT(*) as count FROM messages
               WHERE status='sent' AND route IS NOT NULL
               GROUP BY route ORDER BY count DESC LIMIT ?""",
            (limit,),
        ).fetchall()


# ---------------- Scheduled jobs ----------------
def create_scheduled_job(name, csv_data, template_name, scheduled_for, created_by, var_overrides=None):
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO scheduled_jobs (name, csv_data, template_name,
               scheduled_for, created_by, var_overrides)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, csv_data, template_name, scheduled_for, created_by, var_overrides or ""),
        )
        return cur.lastrowid


def list_scheduled_jobs(status=None):
    sql = "SELECT * FROM scheduled_jobs WHERE 1=1"
    params = []
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY scheduled_for"
    with get_db() as db:
        return db.execute(sql, params).fetchall()


def get_due_jobs():
    with get_db() as db:
        return db.execute(
            "SELECT * FROM scheduled_jobs WHERE status='pending' "
            "AND datetime(scheduled_for) <= datetime('now', '+330 minutes') ORDER BY scheduled_for"
        ).fetchall()


def update_scheduled_job(job_id, status, batch_id=None):
    with get_db() as db:
        if batch_id:
            db.execute(
                "UPDATE scheduled_jobs SET status=?, batch_id=? WHERE id=?",
                (status, batch_id, job_id),
            )
        else:
            db.execute(
                "UPDATE scheduled_jobs SET status=? WHERE id=?", (status, job_id)
            )


def delete_scheduled_job(job_id):
    with get_db() as db:
        db.execute("DELETE FROM scheduled_jobs WHERE id=?", (job_id,))


# ---------------- Chat / Inbox ----------------

def save_chat_message(phone, customer_name, direction, content,
                       wa_message_id=None, message_type="text", status=None,
                       media_path=None, mime_type=None, filename=None,
                       storage_kind=None, raw_payload=None):
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO chats (phone, customer_name, direction, content,
               wa_message_id, message_type, read, status,
               media_path, mime_type, filename, storage_kind, raw_payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (phone, customer_name, direction, content,
             wa_message_id, message_type, 1 if direction == "out" else 0, status,
             media_path, mime_type, filename,
             storage_kind or ("local" if media_path else None),
             raw_payload),
        )
        return cur.lastrowid


def get_conversation(phone, limit=100):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM chats WHERE phone=? ORDER BY timestamp DESC LIMIT ?",
            (phone, limit),
        ).fetchall()
        return rows[::-1]


def list_conversations(limit=50):
    """One row per phone: latest message, unread count, customer name."""
    with get_db() as db:
        return db.execute(
            """SELECT c.phone, c.customer_name, c.content, c.direction,
                      c.timestamp, c.message_type,
                      SUM(CASE WHEN c.read=0 AND c.direction='in' THEN 1 ELSE 0 END) as unread
               FROM chats c
               INNER JOIN (
                   SELECT phone, MAX(timestamp) as max_ts FROM chats GROUP BY phone
               ) latest ON c.phone = latest.phone AND c.timestamp = latest.max_ts
               GROUP BY c.phone
               ORDER BY c.timestamp DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


def mark_read(phone):
    with get_db() as db:
        db.execute(
            "UPDATE chats SET read=1 WHERE phone=? AND direction='in'", (phone,)
        )


def delete_conversation(phone):
    """Delete all chat messages for one phone number (entire conversation).
    Returns the number of rows removed so the UI can show a confirmation."""
    with get_db() as db:
        cur = db.execute("DELETE FROM chats WHERE phone=?", (phone,))
        return cur.rowcount


def count_recent_outbound(phone, hours):
    """How many outbound messages were sent to this phone within the last
    `hours`? Used by the auto-reply throttle: if we've already sent anything
    (auto-reply, manual reply, template…) recently, don't re-fire the welcome
    menu — the customer is mid-conversation."""
    with get_db() as db:
        row = db.execute(
            """SELECT COUNT(*) AS c FROM chats
                WHERE phone = ? AND direction = 'out'
                  AND timestamp >= datetime('now', '+330 minutes', ?)""",
            (phone, f"-{int(hours)} hours"),
        ).fetchone()
        return int(row["c"]) if row else 0


def get_unread_count():
    with get_db() as db:
        return db.execute(
            "SELECT COUNT(*) as c FROM chats WHERE read=0 AND direction='in'"
        ).fetchone()["c"]


def update_chat_status(wa_message_id, status):
    with get_db() as db:
        db.execute(
            "UPDATE chats SET status=? WHERE wa_message_id=?",
            (status, wa_message_id),
        )


def get_new_messages(phone, after_id):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM chats WHERE phone=? AND id>? ORDER BY timestamp ASC",
            (phone, after_id),
        ).fetchall()


# ---------------- Bulk Campaigns ----------------
def _campaign_to_dict(row):
    """Return a campaign as a plain dict with variables parsed to a list."""
    if row is None:
        return None
    d = dict(row)
    try:
        d["variables"] = json.loads(d.get("variables") or "[]")
    except Exception:
        d["variables"] = []
    return d


def create_campaign(name, template_name, variables):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO campaigns (name, template_name, variables) VALUES (?, ?, ?)",
            (name, template_name, json.dumps(variables or [])),
        )
        return cur.lastrowid


def list_campaigns():
    with get_db() as db:
        rows = db.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
        return [_campaign_to_dict(r) for r in rows]


def get_campaign(campaign_id):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM campaigns WHERE id=?", (int(campaign_id),)
        ).fetchone()
        return _campaign_to_dict(row)


def update_campaign(campaign_id, name, template_name, variables):
    with get_db() as db:
        db.execute(
            "UPDATE campaigns SET name=?, template_name=?, variables=? WHERE id=?",
            (name, template_name, json.dumps(variables or []), int(campaign_id)),
        )


def update_campaign_last_used(campaign_id):
    with get_db() as db:
        db.execute(
            "UPDATE campaigns SET last_used_at=datetime('now', '+330 minutes') WHERE id=?",
            (int(campaign_id),),
        )


def delete_campaign(campaign_id):
    with get_db() as db:
        db.execute("DELETE FROM campaigns WHERE id=?", (int(campaign_id),))


def update_campaign_image(campaign_id, media_id, mime_type, size_bytes):
    """Attach a per-campaign banner image (Meta media_id) that overrides
    the template's default header image at send time. Idempotent."""
    with get_db() as db:
        db.execute(
            """UPDATE campaigns
                  SET header_media_id          = ?,
                      header_image_mime        = ?,
                      header_image_size        = ?,
                      header_image_uploaded_at = datetime('now', '+330 minutes')
                WHERE id = ?""",
            (media_id, mime_type, int(size_bytes), int(campaign_id)),
        )


def clear_campaign_image(campaign_id):
    """Remove the per-campaign image override; campaign reverts to using
    the template's default header image."""
    with get_db() as db:
        db.execute(
            """UPDATE campaigns
                  SET header_media_id          = NULL,
                      header_image_mime        = NULL,
                      header_image_size        = NULL,
                      header_image_uploaded_at = NULL
                WHERE id = ?""",
            (int(campaign_id),),
        )


# ---------------- User management ----------------
def change_user_password(user_id, new_hash):
    with get_db() as db:
        db.execute(
            "UPDATE users SET password_hash=? WHERE id=?", (new_hash, int(user_id))
        )


def delete_user(user_id):
    with get_db() as db:
        db.execute("DELETE FROM users WHERE id=?", (int(user_id),))


# ---------------- Auto-replies ----------------
#
# Each row is one configurable customer-facing auto-reply.
#   trigger_label : the human-readable name (also used as button title)
#   keywords      : comma-separated text triggers (e.g. "office,number,phone")
#   response_text : what we send back to the customer
#   show_in_menu  : when True (and rank within top 3), appears as a tap-button
#                   in the welcome menu we send on each inbound message
def create_auto_reply(label, keywords, response_text, show_in_menu=False,
                       menu_order=0, description=""):
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO auto_replies (trigger_label, keywords, response_text,
                                          description, show_in_menu, menu_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (label.strip(), (keywords or "").strip(), response_text,
             (description or "").strip(),
             1 if show_in_menu else 0, int(menu_order or 0)),
        )
        return cur.lastrowid


def list_auto_replies():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM auto_replies ORDER BY show_in_menu DESC, menu_order ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_auto_reply(reply_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM auto_replies WHERE id=?", (int(reply_id),)).fetchone()
        return dict(row) if row else None


def update_auto_reply(reply_id, label, keywords, response_text, show_in_menu,
                       menu_order, description=""):
    with get_db() as db:
        db.execute(
            """UPDATE auto_replies
                  SET trigger_label = ?,
                      keywords      = ?,
                      response_text = ?,
                      description   = ?,
                      show_in_menu  = ?,
                      menu_order    = ?,
                      updated_at    = datetime('now', '+330 minutes')
                WHERE id = ?""",
            (label.strip(), (keywords or "").strip(), response_text,
             (description or "").strip(),
             1 if show_in_menu else 0, int(menu_order or 0), int(reply_id)),
        )


def delete_auto_reply(reply_id):
    with get_db() as db:
        db.execute("DELETE FROM auto_replies WHERE id=?", (int(reply_id),))


def set_auto_reply_enabled(reply_id, enabled):
    with get_db() as db:
        db.execute(
            "UPDATE auto_replies SET enabled=?, updated_at=datetime('now', '+330 minutes') WHERE id=?",
            (1 if enabled else 0, int(reply_id)),
        )


def get_menu_button_replies():
    """Return up to 10 enabled auto-replies flagged for the welcome menu.

    The dispatcher picks the render style based on count:
      1–3 items → interactive buttons (single tap, snappier UX)
      4–10 items → interactive list (modal with a CTA button, two taps)
    WhatsApp Cloud API caps button messages at 3 and list messages at 10."""
    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM auto_replies
                WHERE enabled = 1 AND show_in_menu = 1
                ORDER BY menu_order ASC, id ASC
                LIMIT 10"""
        ).fetchall()
        return [dict(r) for r in rows]


def find_auto_reply_by_text(text):
    """Find the first enabled auto-reply whose trigger_label matches the text
    exactly (case-insensitive — used for button taps) or whose comma-separated
    keywords contain a word found in the text (case-insensitive substring)."""
    if not text:
        return None
    needle = text.strip().lower()
    if not needle:
        return None
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM auto_replies WHERE enabled=1 ORDER BY show_in_menu DESC, menu_order ASC, id ASC"
        ).fetchall()
    for r in rows:
        if (r["trigger_label"] or "").strip().lower() == needle:
            return dict(r)
    for r in rows:
        for kw in (r["keywords"] or "").split(","):
            kw = kw.strip().lower()
            if kw and kw in needle:
                return dict(r)
    return None


# ---------------- Routes & bus trips ----------------

def create_route(origin, destination):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO routes (origin, destination) VALUES (?, ?)",
            (origin.strip(), destination.strip()),
        )
        return cur.lastrowid


def list_routes(active_only=False):
    sql = "SELECT * FROM routes"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY origin ASC, destination ASC, id ASC"
    with get_db() as db:
        return [dict(r) for r in db.execute(sql).fetchall()]


def get_route(route_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM routes WHERE id=?", (int(route_id),)).fetchone()
        return dict(row) if row else None


def update_route(route_id, origin, destination, active):
    with get_db() as db:
        db.execute(
            "UPDATE routes SET origin=?, destination=?, active=? WHERE id=?",
            (origin.strip(), destination.strip(),
             1 if active else 0, int(route_id)),
        )


def delete_route(route_id):
    with get_db() as db:
        # Trips are cascaded since they reference this route.
        db.execute("DELETE FROM bus_trips WHERE route_id=?", (int(route_id),))
        db.execute("DELETE FROM routes WHERE id=?", (int(route_id),))


def list_distinct_origins():
    """Distinct origin cities across enabled routes (for the booking flow)."""
    with get_db() as db:
        rows = db.execute(
            "SELECT DISTINCT origin FROM routes WHERE active=1 ORDER BY origin ASC"
        ).fetchall()
        return [r["origin"] for r in rows]


def list_destinations_from(origin):
    """Enabled routes leaving the given origin."""
    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM routes
                WHERE active=1 AND origin=?
                ORDER BY destination ASC""",
            (origin,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_trip(route_id, departure_time, bus_type, fare):
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO bus_trips (route_id, departure_time, bus_type, fare)
               VALUES (?, ?, ?, ?)""",
            (int(route_id), departure_time.strip(),
             (bus_type or "").strip(), int(fare or 0)),
        )
        return cur.lastrowid


def list_trips_for_route(route_id, active_only=True):
    sql = "SELECT * FROM bus_trips WHERE route_id=?"
    params = [int(route_id)]
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY departure_time ASC, id ASC"
    with get_db() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def get_trip(trip_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM bus_trips WHERE id=?", (int(trip_id),)).fetchone()
        return dict(row) if row else None


def update_trip(trip_id, departure_time, bus_type, fare, active):
    with get_db() as db:
        db.execute(
            """UPDATE bus_trips
                  SET departure_time=?, bus_type=?, fare=?, active=?
                WHERE id=?""",
            (departure_time.strip(), (bus_type or "").strip(),
             int(fare or 0), 1 if active else 0, int(trip_id)),
        )


def delete_trip(trip_id):
    with get_db() as db:
        db.execute("DELETE FROM bus_trips WHERE id=?", (int(trip_id),))


# ---------------- Booking sessions (in-progress conversation state) ----------------

def get_active_booking_session(phone):
    """Return the unfinished session for this phone, or None.
    Expired sessions (>1 hour idle) are auto-cleaned."""
    with get_db() as db:
        db.execute(
            "DELETE FROM booking_sessions WHERE expires_at < datetime('now', '+330 minutes')"
        )
        row = db.execute(
            """SELECT * FROM booking_sessions
                WHERE phone = ?
                ORDER BY id DESC LIMIT 1""",
            (phone,),
        ).fetchone()
        return dict(row) if row else None


def create_booking_session(phone, state="ask_origin"):
    """Start a fresh session, replacing any prior one for this phone."""
    with get_db() as db:
        db.execute("DELETE FROM booking_sessions WHERE phone=?", (phone,))
        cur = db.execute(
            """INSERT INTO booking_sessions (phone, state, expires_at)
               VALUES (?, ?, datetime('now', '+330 minutes', '+1 hour'))""",
            (phone, state),
        )
        return cur.lastrowid


def update_booking_session(session_id, **fields):
    """Update arbitrary session fields. Always bumps updated_at + expires_at."""
    allowed = {"state","origin","destination","route_id","trip_id",
               "travel_date","seats","customer_name"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    sets.append("updated_at=datetime('now', '+330 minutes')")
    sets.append("expires_at=datetime('now', '+330 minutes', '+1 hour')")
    vals.append(int(session_id))
    with get_db() as db:
        db.execute(f"UPDATE booking_sessions SET {', '.join(sets)} WHERE id=?", vals)


def close_booking_session(phone):
    with get_db() as db:
        db.execute("DELETE FROM booking_sessions WHERE phone=?", (phone,))


# ---------------- Bookings (the final saved booking request) ----------------

def _new_pnr():
    """Generate a unique PNR like 'SVN8A1B2C'."""
    import secrets, string
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(20):
        candidate = "SVN" + "".join(secrets.choice(alphabet) for _ in range(6))
        with get_db() as db:
            hit = db.execute("SELECT 1 FROM bookings WHERE pnr=?", (candidate,)).fetchone()
            if not hit:
                return candidate
    raise RuntimeError("could not allocate unique PNR")


def create_booking(*, phone, customer_name, route_id, origin, destination,
                    trip_id, departure_time, bus_type,
                    travel_date, seats, fare_per_seat):
    pnr = _new_pnr()
    total = int(fare_per_seat or 0) * int(seats or 1)
    with get_db() as db:
        db.execute(
            """INSERT INTO bookings
                 (pnr, phone, customer_name, route_id, origin, destination,
                  trip_id, departure_time, bus_type, travel_date,
                  seats, fare_per_seat, total_fare, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (pnr, phone, customer_name, route_id, origin, destination,
             trip_id, departure_time, bus_type, travel_date,
             int(seats or 1), int(fare_per_seat or 0), total),
        )
    return pnr


def list_bookings(status=None, limit=200):
    sql = "SELECT * FROM bookings"
    params = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with get_db() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def get_booking(booking_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM bookings WHERE id=?", (int(booking_id),)).fetchone()
        return dict(row) if row else None


def get_booking_by_pnr(pnr):
    with get_db() as db:
        row = db.execute("SELECT * FROM bookings WHERE pnr=?", (pnr,)).fetchone()
        return dict(row) if row else None


def update_booking_status(booking_id, status, notes=None):
    with get_db() as db:
        if notes is None:
            db.execute(
                "UPDATE bookings SET status=?, updated_at=datetime('now', '+330 minutes') WHERE id=?",
                (status, int(booking_id)),
            )
        else:
            db.execute(
                """UPDATE bookings SET status=?, notes=?,
                       updated_at=datetime('now', '+330 minutes')
                   WHERE id=?""",
                (status, notes, int(booking_id)),
            )


if __name__ == "__main__":
    init_db()
    print("✅ Database initialized at", DB_PATH)
