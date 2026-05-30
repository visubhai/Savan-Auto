"""
Database layer — MongoDB backend.
Set MONGO_URI (and optionally MONGO_DB) in your .env file.
Falls back to SQLite (database_sqlite.py) if MONGO_URI is not set.
"""
import os
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

# All app timestamps use India Standard Time (UTC+5:30), regardless of where
# the server runs (e.g. Render runs in UTC).
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    """Current IST time as a naive datetime (clock time in India)."""
    return datetime.now(IST).replace(tzinfo=None)

MONGO_URI = os.environ.get("MONGO_URI", "")
DB_NAME   = os.environ.get("MONGO_DB", "savan_travels")

# ── If no URI configured, use the original SQLite backend transparently ────────
if not MONGO_URI:
    from database_sqlite import *          # noqa: F401,F403
    from database_sqlite import (          # explicit re-exports needed by scheduler
        get_setting, set_setting, get_all_settings,
        get_user_by_username, get_user_by_id, create_user, list_users,
        get_templates, get_default_template, get_template_by_name,
        upsert_template, set_default_template,
        upsert_customer, search_customers, count_customers, toggle_opt_out,
        create_batch, update_batch_counts, complete_batch,
        get_batch, list_batches,
        log_message, search_messages, count_messages,
        get_batch_messages, get_failed_messages, get_phone_messages,
        get_today_stats, get_month_stats, get_chart_data,
        get_recent_sends, get_top_routes,
        create_scheduled_job, list_scheduled_jobs,
        get_due_jobs, update_scheduled_job, delete_scheduled_job,
        create_campaign, list_campaigns, get_campaign, update_campaign,
        update_campaign_last_used, delete_campaign,
        save_chat_message, get_conversation, list_conversations, mark_read,
        get_unread_count, update_chat_status, get_new_messages,
        change_user_password, delete_user,
        init_db, hash_password, verify_password,
    )
else:
    # ── MongoDB implementation ─────────────────────────────────────────────────
    from pymongo import MongoClient, ASCENDING, DESCENDING

    _client = None

    def _db():
        global _client
        if _client is None:
            _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
        return _client[DB_NAME]

    def _now():
        return now_ist().strftime("%Y-%m-%d %H:%M:%S")

    def _today():
        return now_ist().strftime("%Y-%m-%d")

    def _next_id(name):
        """Atomic auto-increment integer ID per collection."""
        result = _db().counters.find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True,
        )
        return result["seq"]

    def _clean(doc):
        """Strip MongoDB _id and return a plain dict."""
        if doc is None:
            return None
        d = dict(doc)
        d.pop("_id", None)
        return d

    def _clean_many(docs):
        return [_clean(d) for d in docs]

    # ── Password helpers ───────────────────────────────────────────────────────

    def hash_password(password, salt=None):
        if salt is None:
            salt = secrets.token_hex(16)
        h = hashlib.sha256((salt + password).encode()).hexdigest()
        return f"{salt}${h}"

    def verify_password(password, hashed):
        try:
            salt, _ = hashed.split("$", 1)
            return hash_password(password, salt) == hashed
        except (ValueError, AttributeError):
            return False

    # ── Init ──────────────────────────────────────────────────────────────────

    def init_db():
        db = _db()
        db.users.create_index("username", unique=True)
        db.templates.create_index("name", unique=True)
        db.customers.create_index("phone", unique=True)
        db.messages.create_index("batch_id")
        db.messages.create_index("customer_phone")
        db.messages.create_index("status")
        db.messages.create_index("sent_at")
        db.customers.create_index([("name", ASCENDING)])
        db.batches.create_index("started_at")

        if db.users.count_documents({}) == 0:
            db.users.insert_one({
                "id": _next_id("users"),
                "username": "admin",
                "password_hash": hash_password("savan123"),
                "display_name": "Hitesh (Admin)",
                "role": "admin",
                "created_at": _now(),
            })

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
        for k, v in defaults.items():
            db.settings.update_one(
                {"key": k},
                {"$setOnInsert": {"key": k, "value": v}},
                upsert=True,
            )

        if db.templates.count_documents({}) == 0:
            db.templates.insert_one({
                "id": _next_id("templates"),
                "name": "journey_reminder",
                "language": "en",
                "category": "UTILITY",
                "body": (
                    "Hello {{1}},\n\nMain Savan Travels se bol raha hoon. "
                    "Aapne {{2}} travel kiya tha.\n\nAapko {{3}} ki taraf se "
                    "rating/review link prapt hua hoga. Kripya apna feedback "
                    "share karein.\n\nReview submit karne ke baad screenshot "
                    "bhejne ki bhi kripya karein.\n\nDhanyavaad 🙏"
                ),
                "variable_count": 3,
                "status": "approved",
                "is_default": 1,
                "synced_at": None,
                "created_at": _now(),
            })

    # ── Settings ──────────────────────────────────────────────────────────────

    def get_setting(key, default=""):
        doc = _db().settings.find_one({"key": key})
        return doc["value"] if doc else default

    def set_setting(key, value):
        _db().settings.update_one(
            {"key": key},
            {"$set": {"key": key, "value": str(value)}},
            upsert=True,
        )

    def get_all_settings():
        return {d["key"]: d["value"] for d in _db().settings.find()}

    # ── Users ─────────────────────────────────────────────────────────────────

    def get_user_by_username(username):
        return _clean(_db().users.find_one({"username": username}))

    def get_user_by_id(user_id):
        return _clean(_db().users.find_one({"id": int(user_id)}))

    def create_user(username, password, display_name, role="member"):
        _db().users.insert_one({
            "id": _next_id("users"),
            "username": username,
            "password_hash": hash_password(password),
            "display_name": display_name,
            "role": role,
            "created_at": _now(),
        })

    def list_users():
        return _clean_many(
            _db().users.find({}, {"password_hash": 0}).sort("id", ASCENDING)
        )

    def change_user_password(user_id, new_hash):
        _db().users.update_one({"id": int(user_id)}, {"$set": {"password_hash": new_hash}})

    def delete_user(user_id):
        _db().users.delete_one({"id": int(user_id)})

    # ── Bulk Campaigns ────────────────────────────────────────────────────────

    def create_campaign(name, template_name, variables):
        campaign_id = _next_id("campaigns")
        _db().campaigns.insert_one({
            "id": campaign_id,
            "name": name,
            "template_name": template_name,
            "variables": variables,
            "created_at": _now(),
            "last_used_at": None,
        })
        return campaign_id

    def list_campaigns():
        return _clean_many(_db().campaigns.find().sort("created_at", DESCENDING))

    def get_campaign(campaign_id):
        return _clean(_db().campaigns.find_one({"id": int(campaign_id)}))

    def update_campaign(campaign_id, name, template_name, variables):
        _db().campaigns.update_one({"id": int(campaign_id)}, {"$set": {
            "name": name, "template_name": template_name, "variables": variables,
        }})

    def update_campaign_last_used(campaign_id):
        _db().campaigns.update_one({"id": int(campaign_id)}, {"$set": {"last_used_at": _now()}})

    def delete_campaign(campaign_id):
        _db().campaigns.delete_one({"id": int(campaign_id)})

    # ── Templates ─────────────────────────────────────────────────────────────

    def get_templates():
        return _clean_many(
            _db().templates.find().sort([("is_default", DESCENDING), ("name", ASCENDING)])
        )

    def get_default_template():
        return _clean(_db().templates.find_one({"is_default": 1}))

    def get_template_by_name(name):
        return _clean(_db().templates.find_one({"name": name}))

    def upsert_template(name, language, category, body, variable_count, status):
        _db().templates.update_one(
            {"name": name},
            {"$set": {
                "language": language,
                "category": category,
                "body": body,
                "variable_count": variable_count,
                "status": status,
                "synced_at": _now(),
            },
             "$setOnInsert": {
                "id": _next_id("templates"),
                "is_default": 0,
                "created_at": _now(),
            }},
            upsert=True,
        )

    def set_default_template(name):
        _db().templates.update_many({}, {"$set": {"is_default": 0}})
        _db().templates.update_one({"name": name}, {"$set": {"is_default": 1}})

    # ── Customers ─────────────────────────────────────────────────────────────

    def upsert_customer(phone, name, route, platform):
        existing = _db().customers.find_one({"phone": phone})
        if existing:
            update = {
                "$set": {
                    "last_route": route,
                    "last_platform": platform,
                    "last_messaged_at": _now(),
                },
                "$inc": {"total_messages": 1},
            }
            if name:
                update["$set"]["name"] = name
            _db().customers.update_one({"phone": phone}, update)
        else:
            _db().customers.insert_one({
                "id": _next_id("customers"),
                "phone": phone,
                "name": name,
                "last_route": route,
                "last_platform": platform,
                "total_messages": 1,
                "last_messaged_at": _now(),
                "opted_out": 0,
                "created_at": _now(),
            })

    def search_customers(query="", opted_out=None, limit=100, offset=0):
        filt = {}
        if query:
            import re
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            filt["$or"] = [{"name": pattern}, {"phone": pattern}]
        if opted_out is not None:
            filt["opted_out"] = 1 if opted_out else 0
        cursor = (
            _db().customers.find(filt)
            .sort("last_messaged_at", DESCENDING)
            .skip(offset)
            .limit(limit)
        )
        return _clean_many(cursor)

    def count_customers(query="", opted_out=None):
        filt = {}
        if query:
            import re
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            filt["$or"] = [{"name": pattern}, {"phone": pattern}]
        if opted_out is not None:
            filt["opted_out"] = 1 if opted_out else 0
        return _db().customers.count_documents(filt)

    def toggle_opt_out(phone, opted_out):
        _db().customers.update_one(
            {"phone": phone},
            {"$set": {"opted_out": 1 if opted_out else 0}},
        )

    # ── Batches ───────────────────────────────────────────────────────────────

    def create_batch(name, template_name, total_count, started_by):
        batch_id = _next_id("batches")
        _db().batches.insert_one({
            "id": batch_id,
            "name": name,
            "template_name": template_name,
            "total_count": total_count,
            "sent_count": 0,
            "failed_count": 0,
            "status": "running",
            "started_by": started_by,
            "started_at": _now(),
            "completed_at": None,
        })
        return batch_id

    def update_batch_counts(batch_id, sent=None, failed=None):
        inc = {}
        if sent is not None:
            inc["sent_count"] = sent
        if failed is not None:
            inc["failed_count"] = failed
        if inc:
            _db().batches.update_one({"id": int(batch_id)}, {"$inc": inc})

    def complete_batch(batch_id):
        _db().batches.update_one(
            {"id": int(batch_id)},
            {"$set": {"status": "completed", "completed_at": _now()}},
        )

    def get_batch(batch_id):
        return _clean(_db().batches.find_one({"id": int(batch_id)}))

    def list_batches(limit=50):
        return _clean_many(
            _db().batches.find().sort("started_at", DESCENDING).limit(limit)
        )

    # ── Messages ──────────────────────────────────────────────────────────────

    def log_message(batch_id, phone, name, route, platform, template_name,
                    status, error=None, wa_msg_id=None):
        _db().messages.insert_one({
            "id": _next_id("messages"),
            "batch_id": int(batch_id),
            "customer_phone": phone,
            "customer_name": name,
            "route": route,
            "platform": platform,
            "template_name": template_name,
            "status": status,
            "error_message": error,
            "wa_message_id": wa_msg_id,
            "sent_at": _now(),
            "created_at": _now(),
        })

    def search_messages(query="", status=None, days=None, limit=100, offset=0):
        filt = {}
        if query:
            import re
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            filt["$or"] = [{"customer_name": pattern}, {"customer_phone": pattern}]
        if status:
            filt["status"] = status
        if days:
            cutoff = (now_ist() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
            filt["sent_at"] = {"$gte": cutoff}
        cursor = (
            _db().messages.find(filt)
            .sort("sent_at", DESCENDING)
            .skip(offset)
            .limit(limit)
        )
        return _clean_many(cursor)

    def count_messages(query="", status=None, days=None):
        filt = {}
        if query:
            import re
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            filt["$or"] = [{"customer_name": pattern}, {"customer_phone": pattern}]
        if status:
            filt["status"] = status
        if days:
            cutoff = (now_ist() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
            filt["sent_at"] = {"$gte": cutoff}
        return _db().messages.count_documents(filt)

    def get_batch_messages(batch_id):
        return _clean_many(
            _db().messages.find({"batch_id": int(batch_id)}).sort("id", ASCENDING)
        )

    def get_failed_messages(batch_id):
        return _clean_many(
            _db().messages.find({"batch_id": int(batch_id), "status": "failed"}).sort("id", ASCENDING)
        )

    def get_phone_messages(phone, limit=200):
        """Bulk/template messages sent to this phone, oldest first.
        Used in the inbox to show template sends alongside chat messages.
        """
        return _clean_many(
            _db().messages.find({"customer_phone": phone})
            .sort("sent_at", ASCENDING)
            .limit(limit)
        )

    # ── Dashboard stats ───────────────────────────────────────────────────────

    def get_today_stats():
        today = _today()
        sent   = _db().messages.count_documents({"status": "sent",   "sent_at": {"$gte": today}})
        failed = _db().messages.count_documents({"status": "failed", "sent_at": {"$gte": today}})
        cost_per = float(get_setting("cost_per_message", "0.12"))
        return {"sent": sent, "failed": failed, "cost": round(sent * cost_per, 2)}

    def get_month_stats():
        month_start = now_ist().strftime("%Y-%m-01")
        sent   = _db().messages.count_documents({"status": "sent",   "sent_at": {"$gte": month_start}})
        failed = _db().messages.count_documents({"status": "failed", "sent_at": {"$gte": month_start}})
        cost_per = float(get_setting("cost_per_message", "0.12"))
        wap_cost = float(get_setting("wapsolution_monthly_cost", "2000"))
        cost = round(sent * cost_per, 2)
        return {
            "sent": sent, "failed": failed, "cost": cost,
            "saved": round(wap_cost - cost, 2), "wap_cost": wap_cost,
        }

    def get_chart_data(days=7):
        cutoff = (now_ist() - timedelta(days=int(days))).strftime("%Y-%m-%d")
        pipeline = [
            {"$match": {"sent_at": {"$gte": cutoff}}},
            {"$group": {
                "_id": {"$substr": ["$sent_at", 0, 10]},
                "sent":   {"$sum": {"$cond": [{"$eq": ["$status", "sent"]},   1, 0]}},
                "failed": {"$sum": {"$cond": [{"$eq": ["$status", "failed"]}, 1, 0]}},
            }},
            {"$sort": {"_id": 1}},
        ]
        return [{"day": r["_id"], "sent": r["sent"], "failed": r["failed"]}
                for r in _db().messages.aggregate(pipeline)]

    def get_recent_sends(limit=10):
        return _clean_many(
            _db().messages.find().sort("id", DESCENDING).limit(limit)
        )

    def get_top_routes(limit=5):
        pipeline = [
            {"$match": {"status": "sent", "route": {"$ne": None}}},
            {"$group": {"_id": "$route", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": limit},
        ]
        return [{"route": r["_id"], "count": r["count"]}
                for r in _db().messages.aggregate(pipeline)]

    # ── Scheduled jobs ────────────────────────────────────────────────────────

    def create_scheduled_job(name, csv_data, template_name, scheduled_for, created_by, var_overrides=None):
        job_id = _next_id("scheduled_jobs")
        _db().scheduled_jobs.insert_one({
            "id": job_id,
            "name": name,
            "csv_data": csv_data,
            "template_name": template_name,
            "scheduled_for": scheduled_for,
            "status": "pending",
            "created_by": created_by,
            "batch_id": None,
            "var_overrides": var_overrides or "",
            "created_at": _now(),
        })
        return job_id

    def list_scheduled_jobs(status=None):
        filt = {}
        if status:
            filt["status"] = status
        return _clean_many(
            _db().scheduled_jobs.find(filt).sort("scheduled_for", ASCENDING)
        )

    def get_due_jobs():
        # scheduled_for is stored in IST (user's browser local time),
        # so compare against IST now.
        now_str = _now()
        return _clean_many(
            _db().scheduled_jobs.find({
                "status": "pending",
                "scheduled_for": {"$lte": now_str},
            }).sort("scheduled_for", ASCENDING)
        )

    def update_scheduled_job(job_id, status, batch_id=None):
        update = {"$set": {"status": status}}
        if batch_id is not None:
            update["$set"]["batch_id"] = batch_id
        _db().scheduled_jobs.update_one({"id": int(job_id)}, update)

    def delete_scheduled_job(job_id):
        _db().scheduled_jobs.delete_one({"id": int(job_id)})

    # ── Chat / Inbox ──────────────────────────────────────────────────────────

    def save_chat_message(phone, customer_name, direction, content,
                           wa_message_id=None, message_type="text", status=None,
                           media_path=None, mime_type=None, filename=None):
        msg_id = _next_id("chats")
        _db().chats.insert_one({
            "id": msg_id,
            "phone": phone,
            "customer_name": customer_name,
            "direction": direction,
            "message_type": message_type,
            "content": content,
            "wa_message_id": wa_message_id,
            "timestamp": _now(),
            "read": 1 if direction == "out" else 0,
            "status": status,
            "media_path": media_path,
            "mime_type": mime_type,
            "filename": filename,
        })
        return msg_id

    def get_conversation(phone, limit=100):
        return _clean_many(
            _db().chats.find({"phone": phone})
            .sort("timestamp", ASCENDING)
            .limit(limit)
        )

    def list_conversations(limit=50):
        pipeline = [
            {"$sort": {"timestamp": DESCENDING}},
            {"$group": {
                "_id": "$phone",
                "phone":         {"$first": "$phone"},
                "customer_name": {"$first": "$customer_name"},
                "content":       {"$first": "$content"},
                "direction":     {"$first": "$direction"},
                "timestamp":     {"$first": "$timestamp"},
                "message_type":  {"$first": "$message_type"},
            }},
            {"$sort": {"timestamp": DESCENDING}},
            {"$limit": limit},
        ]
        convos = list(_db().chats.aggregate(pipeline))

        # Attach unread count per phone
        for c in convos:
            c["unread"] = _db().chats.count_documents({
                "phone": c["_id"], "direction": "in", "read": 0,
            })
            c.pop("_id", None)
        return convos

    def mark_read(phone):
        _db().chats.update_many(
            {"phone": phone, "direction": "in"},
            {"$set": {"read": 1}},
        )

    def get_unread_count():
        return _db().chats.count_documents({"direction": "in", "read": 0})

    def update_chat_status(wa_message_id, status):
        _db().chats.update_one(
            {"wa_message_id": wa_message_id},
            {"$set": {"status": status}},
        )

    def get_new_messages(phone, after_id):
        return _clean_many(
            _db().chats.find({"phone": phone, "id": {"$gt": int(after_id)}})
            .sort("timestamp", ASCENDING)
        )


if __name__ == "__main__":
    if not MONGO_URI:
        print("⚠  MONGO_URI not set — using SQLite fallback.")
    else:
        init_db()
        print("✅ MongoDB database initialised:", DB_NAME)
