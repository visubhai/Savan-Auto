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
            last_used_at TEXT
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


def upsert_template(name, language, category, body, variable_count, status):
    with get_db() as db:
        existing = db.execute("SELECT id FROM templates WHERE name=?", (name,)).fetchone()
        if existing:
            db.execute(
                """UPDATE templates SET language=?, category=?, body=?,
                   variable_count=?, status=?, synced_at=datetime('now', '+330 minutes')
                   WHERE name=?""",
                (language, category, body, variable_count, status, name),
            )
        else:
            db.execute(
                """INSERT INTO templates (name, language, category, body,
                   variable_count, status, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now', '+330 minutes'))""",
                (name, language, category, body, variable_count, status),
            )


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
        return db.execute(
            "SELECT * FROM messages WHERE customer_phone=? ORDER BY sent_at ASC LIMIT ?",
            (phone, limit),
        ).fetchall()


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
                       storage_kind=None):
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO chats (phone, customer_name, direction, content,
               wa_message_id, message_type, read, status,
               media_path, mime_type, filename, storage_kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (phone, customer_name, direction, content,
             wa_message_id, message_type, 1 if direction == "out" else 0, status,
             media_path, mime_type, filename,
             storage_kind or ("local" if media_path else None)),
        )
        return cur.lastrowid


def get_conversation(phone, limit=100):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM chats WHERE phone=? ORDER BY timestamp ASC LIMIT ?",
            (phone, limit),
        ).fetchall()


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


# ---------------- User management ----------------
def change_user_password(user_id, new_hash):
    with get_db() as db:
        db.execute(
            "UPDATE users SET password_hash=? WHERE id=?", (new_hash, int(user_id))
        )


def delete_user(user_id):
    with get_db() as db:
        db.execute("DELETE FROM users WHERE id=?", (int(user_id),))


if __name__ == "__main__":
    init_db()
    print("✅ Database initialized at", DB_PATH)
