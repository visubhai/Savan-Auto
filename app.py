"""
Savan Travels WhatsApp Sender — Flask web application.
Run: python app.py  →  http://localhost:5000
Default login: admin / savan123
"""
import os
import json
import io
import csv
import tempfile
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    jsonify, flash, send_file, abort,
)

import database as db
from parsers import auto_parse, extract_all_phones
from meta_api import WhatsAppAPI
import scheduler

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "savan-travels-localhost-secret-2024")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB

# FIX 1: Use filesystem session storage for large CSVs instead of cookie
# Store pending passengers in a temp file, only store path in session
TEMP_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(TEMP_DIR, exist_ok=True)

# Custom Jinja filters
@app.template_filter("from_json")
def from_json_filter(s):
    try:
        return json.loads(s)
    except Exception:
        return []

# Initialize DB on startup
db.init_db()

# Start background scheduler
scheduler.start_scheduler()


# ---------------- Session helpers (FIX 1: file-based pending store) ----------------
def save_pending(data):
    """Save large pending data to temp file, return file key."""
    key = f"pending_{session.get('user_id', 0)}_{db.now_ist().strftime('%Y%m%d%H%M%S%f')}.json"
    path = os.path.join(TEMP_DIR, key)
    with open(path, "w") as f:
        json.dump(data, f)
    # Clean old pending files for this user
    prefix = f"pending_{session.get('user_id', 0)}_"
    for fn in os.listdir(TEMP_DIR):
        if fn.startswith(prefix) and fn != key:
            try:
                os.remove(os.path.join(TEMP_DIR, fn))
            except Exception:
                pass
    return key


def load_pending(key):
    """Load pending data from temp file."""
    if not key:
        return None
    path = os.path.join(TEMP_DIR, key)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def clear_pending(key):
    if not key:
        return
    path = os.path.join(TEMP_DIR, key)
    try:
        os.remove(path)
    except Exception:
        pass


def _parse_var_overrides(raw):
    """Parse the var_overrides JSON from a form field into a clean dict.

    Returns {var_index_str: fixed_value} keeping only non-empty values.
    e.g. '{"3":"RedBus","4":"Diwali Offer"}' -> {'3':'RedBus','4':'Diwali Offer'}
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    clean = {}
    for k, v in data.items():
        val = str(v).strip() if v is not None else ""
        if val:
            clean[str(k)] = val
    return clean


# ---------------- Auth decorators ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            flash("Admin access required", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_user():
    return {
        "current_user": {
            "id": session.get("user_id"),
            "username": session.get("username"),
            "display_name": session.get("display_name"),
            "role": session.get("role"),
        } if "user_id" in session else None,
        "business_name": db.get_setting("business_name", "Savan Travels"),
    }


# ---------------- Auth routes ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.get_user_by_username(username)
        if user and db.verify_password(password, user["password_hash"]):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["display_name"] = user["display_name"]
            session["role"] = user["role"]
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        flash("Invalid username or password", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    # Clean up pending file
    clear_pending(session.get("pending_key"))
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# ---------------- Dashboard ----------------
@app.route("/dashboard")
@login_required
def dashboard():
    today = db.get_today_stats()
    month = db.get_month_stats()
    chart_data = db.get_chart_data(7)
    recent = db.get_recent_sends(10)
    top_routes = db.get_top_routes(5)

    from datetime import timedelta
    today_date = db.now_ist().date()
    chart_map = {row["day"]: row for row in chart_data}
    days = []
    for i in range(6, -1, -1):
        d = today_date - timedelta(days=i)
        d_str = d.isoformat()
        row = chart_map.get(d_str, {})
        days.append({
            "day": d.strftime("%d %b"),
            "sent": row.get("sent", 0),
            "failed": row.get("failed", 0),
        })

    api = WhatsAppAPI()
    api_configured = api.is_configured()
    batches = db.list_batches(5)

    return render_template("dashboard.html",
                           today=today, month=month, chart=days,
                           recent=recent, top_routes=top_routes,
                           api_configured=api_configured,
                           batches=batches)


# ---------------- Send messages ----------------
ALLOWED_EXTENSIONS = {".csv", ".txt", ".tsv"}

@app.route("/send", methods=["GET", "POST"])
@login_required
def send():
    templates = [dict(t) for t in db.get_templates()]
    default_tpl = db.get_default_template()
    default_tpl = dict(default_tpl) if default_tpl else None

    if request.method == "POST":
        files = request.files.getlist("csv_file")
        files = [f for f in files if f.filename]

        if not files:
            flash("No file uploaded", "error")
            return redirect(url_for("send"))

        all_passengers = []
        seen_phones_global = set()
        all_stats = {"total_rows": 0, "cancelled": 0, "duplicates": 0, "invalid": 0}
        filenames = []

        for f in files:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                flash(f"'{f.filename}' skipped — only .csv/.txt/.tsv files are allowed.", "error")
                continue
            try:
                content = f.read()
                parsed = auto_parse(content, filename=f.filename)
            except Exception as e:
                flash(f"Failed to parse '{f.filename}': {e}", "error")
                continue

            filenames.append(f.filename)
            all_stats["total_rows"] += parsed["total_rows_seen"]
            all_stats["cancelled"] += parsed["cancelled_count"]
            all_stats["duplicates"] += parsed["duplicates_removed"]
            all_stats["invalid"] += parsed["invalid_phones"]

            for p in parsed["passengers"]:
                if p["phone"] not in seen_phones_global:
                    seen_phones_global.add(p["phone"])
                    all_passengers.append(p)
                else:
                    all_stats["duplicates"] += 1

        if not filenames:
            flash("No valid CSV files were uploaded.", "error")
            return redirect(url_for("send"))

        if not all_passengers and all_stats["total_rows"] == 0:
            flash("CSV appears empty or has no recognizable data. Check the file has phone numbers.", "error")
            return redirect(url_for("send"))

        # Filter opted-out customers
        opted_out_rows = db.search_customers(opted_out=True, limit=50000)
        opted_out_phones = {r["phone"] for r in opted_out_rows}
        original_count = len(all_passengers)
        all_passengers = [p for p in all_passengers if p["phone"] not in opted_out_phones]
        opt_out_filtered = original_count - len(all_passengers)

        if len(all_passengers) > 2000:
            delay = float(db.get_setting("delay_between_messages", "1.5"))
            hours = round(len(all_passengers) * delay / 3600, 1)
            flash(f"⚠ Large batch: {len(all_passengers)} messages will take ~{hours} hours to send. Consider splitting.", "info")

        batch_label = " + ".join(filenames) if len(filenames) > 1 else filenames[0]

        pending_data = {
            "passengers": all_passengers,
            "stats": {
                "total_rows": all_stats["total_rows"],
                "cancelled": all_stats["cancelled"],
                "duplicates": all_stats["duplicates"],
                "invalid": all_stats["invalid"],
                "opted_out": opt_out_filtered,
                "ready": len(all_passengers),
            },
            "filename": batch_label,
        }
        key = save_pending(pending_data)
        session["pending_key"] = key
        return redirect(url_for("send_preview"))

    return render_template("send.html", templates=templates, default_tpl=default_tpl)


@app.route("/send/preview")
@login_required
def send_preview():
    key = session.get("pending_key")
    pending = load_pending(key)
    if not pending:
        flash("Session expired or no file uploaded. Please upload again.", "error")
        return redirect(url_for("send"))

    templates = [dict(t) for t in db.get_templates()]
    default_tpl = db.get_default_template()
    default_tpl = dict(default_tpl) if default_tpl else None
    cost_per = float(db.get_setting("cost_per_message", "0.12"))
    estimated_cost = round(len(pending["passengers"]) * cost_per, 2)
    delay = float(db.get_setting("delay_between_messages", "1.5"))
    estimated_mins = round(len(pending["passengers"]) * delay / 60, 1)

    # FIX 7: flag large batches for extra confirmation
    needs_confirm = len(pending["passengers"]) > 200

    return render_template("send_preview.html",
                           pending=pending, templates=templates,
                           default_tpl=default_tpl,
                           estimated_cost=estimated_cost,
                           estimated_mins=estimated_mins,
                           needs_confirm=needs_confirm)


@app.route("/send/start", methods=["POST"])
@login_required
def send_start():
    key = session.get("pending_key")
    pending = load_pending(key)
    if not pending:
        flash("Session expired. Please upload CSV again.", "error")
        return redirect(url_for("send"))

    template_name = request.form.get("template_name") or request.form.get("hiddenTpl")
    if not template_name:
        flash("Please select a template", "error")
        return redirect(url_for("send_preview"))

    # FIX 4: Always read fresh token from DB
    api = WhatsAppAPI()
    if not api.is_configured():
        flash("WhatsApp API token not configured. Go to Settings first.", "error")
        return redirect(url_for("settings"))

    # Use edited passenger list from table if provided
    passengers_json = request.form.get("passengers_json", "").strip()
    if passengers_json:
        try:
            passengers = json.loads(passengers_json)
            # Validate each entry has a phone
            passengers = [p for p in passengers if p.get("phone")]
        except Exception:
            passengers = pending["passengers"]
    else:
        passengers = pending["passengers"]

    if not passengers:
        flash("No passengers to send to. Please check your selection.", "error")
        return redirect(url_for("send_preview"))

    # FIX 7: extra confirmation for large batches
    if len(passengers) > 200:
        confirmed = request.form.get("confirmed") == "yes"
        if not confirmed:
            flash("Please confirm the large batch send.", "error")
            return redirect(url_for("send_preview"))

    # Optional fixed variable values for this batch (for template vars not in CSV)
    var_overrides = _parse_var_overrides(request.form.get("var_overrides", ""))

    batch_name = pending.get("filename", "Manual upload")
    batch_id = scheduler.start_send_thread(
        passengers, template_name, batch_name, session["user_id"],
        var_overrides=var_overrides,
    )
    clear_pending(key)
    session.pop("pending_key", None)
    return redirect(url_for("send_progress", batch_id=batch_id))


@app.route("/send/schedule", methods=["POST"])
@login_required
def send_schedule():
    key = session.get("pending_key")
    pending = load_pending(key)
    if not pending:
        flash("Session expired. Please upload CSV again.", "error")
        return redirect(url_for("send"))

    template_name = request.form.get("template_name")
    scheduled_for = request.form.get("scheduled_for")
    if not template_name or not scheduled_for:
        flash("Template and schedule time are required", "error")
        return redirect(url_for("send_preview"))

    try:
        dt = datetime.fromisoformat(scheduled_for)
        if dt <= db.now_ist():
            flash("Scheduled time must be in the future", "error")
            return redirect(url_for("send_preview"))
        scheduled_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        flash("Invalid schedule time format", "error")
        return redirect(url_for("send_preview"))

    # Use edited passenger list if provided (same as send_start does)
    passengers_json = request.form.get("passengers_json", "").strip()
    if passengers_json:
        try:
            passengers = [p for p in json.loads(passengers_json) if p.get("phone")]
        except Exception:
            passengers = pending["passengers"]
    else:
        passengers = pending["passengers"]

    # Optional fixed variable values for this batch (for template vars not in CSV)
    var_overrides = _parse_var_overrides(request.form.get("var_overrides", ""))

    job_id = db.create_scheduled_job(
        name=pending.get("filename", "Scheduled batch"),
        csv_data=json.dumps(passengers),
        template_name=template_name,
        scheduled_for=scheduled_str,
        created_by=session["user_id"],
        var_overrides=json.dumps(var_overrides) if var_overrides else "",
    )
    clear_pending(key)
    session.pop("pending_key", None)
    flash(f"✓ Scheduled {len(passengers)} messages for {scheduled_str}", "success")
    return redirect(url_for("scheduled"))


@app.route("/send/progress/<int:batch_id>")
@login_required
def send_progress(batch_id):
    batch = db.get_batch(batch_id)
    if not batch:
        flash("Batch not found", "error")
        return redirect(url_for("dashboard"))
    return render_template("send_progress.html", batch=dict(batch))


@app.route("/send/status/<int:batch_id>")
@login_required
def send_status(batch_id):
    batch = db.get_batch(batch_id)
    if not batch:
        return jsonify({"error": "not found"}), 404
    live = scheduler.RUNNING_BATCHES.get(batch_id, {})

    # FIX 3: If batch is completed in DB but not in RUNNING_BATCHES (server restart),
    # return completed status from DB
    status = batch["status"]
    if status == "completed":
        return jsonify({
            "id": batch["id"],
            "total": batch["total_count"],
            "sent": batch["sent_count"],
            "failed": batch["failed_count"],
            "status": "completed",
            "current_name": "",
            "completed_at": batch["completed_at"],
        })

    return jsonify({
        "id": batch["id"],
        "total": batch["total_count"],
        "sent": batch["sent_count"],
        "failed": batch["failed_count"],
        "status": status,
        "current_name": live.get("current_name", ""),
        "completed_at": batch["completed_at"],
    })


@app.route("/send/retry/<int:batch_id>", methods=["POST"])
@login_required
def send_retry(batch_id):
    failed = db.get_failed_messages(batch_id)
    if not failed:
        flash("No failed messages to retry", "info")
        return redirect(url_for("send_progress", batch_id=batch_id))

    passengers = [{
        "phone": m["customer_phone"],
        "name": m["customer_name"] or "Customer",
        "route": m["route"] or "",
        "platform": m["platform"] or "",
    } for m in failed]

    template_name = failed[0]["template_name"] or db.get_setting("default_template", "journey_reminder")
    new_batch_id = scheduler.start_send_thread(
        passengers, template_name, f"Retry of #{batch_id}", session["user_id"],
    )
    return redirect(url_for("send_progress", batch_id=new_batch_id))


# ---------------- History ----------------
@app.route("/history")
@login_required
def history():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip() or None
    days_str = request.args.get("days", "").strip()
    days = int(days_str) if days_str.isdigit() else None
    page = max(1, int(request.args.get("page", "1")))
    per_page = 50
    offset = (page - 1) * per_page

    messages = db.search_messages(q, status, days, per_page, offset)
    total = db.count_messages(q, status, days)
    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template("history.html",
                           messages=messages, q=q, status=status, days=days,
                           page=page, total=total, total_pages=total_pages)


@app.route("/history/export")
@login_required
def history_export():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip() or None
    days_str = request.args.get("days", "").strip()
    days = int(days_str) if days_str.isdigit() else None
    messages = db.search_messages(q, status, days, limit=200000)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["sent_at", "phone", "name", "route", "platform",
                     "template", "status", "error", "wa_message_id"])
    for m in messages:
        writer.writerow([
            m["sent_at"], m["customer_phone"], m["customer_name"],
            m["route"], m["platform"], m["template_name"],
            m["status"], m["error_message"] or "", m["wa_message_id"] or "",
        ])
    output.seek(0)
    filename = f"history_{db.now_ist().strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


# ---------------- Customers ----------------
@app.route("/customers")
@login_required
def customers():
    q = request.args.get("q", "").strip()
    filter_opt = request.args.get("filter", "").strip()
    opted_out = True if filter_opt == "opted_out" else (False if filter_opt == "active" else None)
    page = max(1, int(request.args.get("page", "1")))
    per_page = 50
    offset = (page - 1) * per_page

    rows = db.search_customers(q, opted_out, per_page, offset)
    total = db.count_customers(q, opted_out)
    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template("customers.html",
                           customers=rows, q=q, filter_opt=filter_opt,
                           page=page, total=total, total_pages=total_pages)


@app.route("/customers/toggle/<phone>", methods=["POST"])
@login_required
def customer_toggle(phone):
    opted_out = request.form.get("opted_out") == "1"
    db.toggle_opt_out(phone, opted_out)
    action = "opted out" if opted_out else "restored"
    flash(f"Customer {phone} {action}", "success")
    return redirect(request.referrer or url_for("customers"))


# ---------------- Templates ----------------
@app.route("/templates")
@login_required
def templates_page():
    templates = db.get_templates()
    api = WhatsAppAPI()
    return render_template("templates.html",
                           templates=templates,
                           api_configured=api.is_configured())


@app.route("/templates/sync", methods=["POST"])
@login_required
def templates_sync():
    api = WhatsAppAPI()
    if not api.is_configured():
        flash("WhatsApp API not configured. Add token in Settings first.", "error")
        return redirect(url_for("templates_page"))
    success, msg = api.fetch_templates()
    flash(msg, "success" if success else "error")
    return redirect(url_for("templates_page"))


@app.route("/templates/set-default/<name>", methods=["POST"])
@login_required
def templates_set_default(name):
    db.set_default_template(name)
    flash(f"✓ '{name}' set as default template", "success")
    return redirect(url_for("templates_page"))


# ---------------- Scheduled jobs ----------------
@app.route("/scheduled")
@login_required
def scheduled():
    jobs = db.list_scheduled_jobs()
    return render_template("scheduled.html", jobs=jobs)


@app.route("/scheduled/cancel/<int:job_id>", methods=["POST"])
@login_required
def scheduled_cancel(job_id):
    db.delete_scheduled_job(job_id)
    flash(f"Scheduled job #{job_id} cancelled", "success")
    return redirect(url_for("scheduled"))


# ---------------- Settings ----------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        keys = [
            "access_token", "phone_number_id", "waba_id", "api_version",
            "delay_between_messages", "max_retries", "cost_per_message",
            "wapsolution_monthly_cost", "business_name",
        ]
        for k in keys:
            v = request.form.get(k)
            if v is not None:
                if k == "access_token" and not v.strip():
                    continue  # Don't wipe existing token
                db.set_setting(k, v.strip())
        flash("✓ Settings saved successfully", "success")
        return redirect(url_for("settings"))

    all_settings = db.get_all_settings()
    return render_template("settings.html", settings=all_settings)


@app.route("/settings/test", methods=["POST"])
@login_required
def settings_test():
    # FIX 4: Always create fresh API instance to pick up latest token
    api = WhatsAppAPI()
    success, msg = api.test_connection()
    return jsonify({"success": success, "message": msg})


# ---------------- Change password ----------------
@app.route("/settings/change-password", methods=["POST"])
@login_required
def change_password():
    current = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    user = db.get_user_by_id(session["user_id"])
    if not db.verify_password(current, user["password_hash"]):
        flash("Current password is incorrect", "error")
        return redirect(url_for("settings"))
    if len(new_pw) < 6:
        flash("New password must be at least 6 characters", "error")
        return redirect(url_for("settings"))
    if new_pw != confirm:
        flash("Passwords don't match", "error")
        return redirect(url_for("settings"))

    db.change_user_password(session["user_id"], db.hash_password(new_pw))
    flash("✓ Password changed successfully", "success")
    return redirect(url_for("settings"))


# ---------------- Team (users) ----------------
@app.route("/users")
@admin_required
def users():
    rows = db.list_users()
    return render_template("users.html", users=rows)


@app.route("/users/create", methods=["POST"])
@admin_required
def users_create():
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    display_name = request.form.get("display_name", "").strip()
    role = request.form.get("role", "member")

    if not username or not password or not display_name:
        flash("All fields are required", "error")
        return redirect(url_for("users"))
    if len(password) < 6:
        flash("Password must be at least 6 characters", "error")
        return redirect(url_for("users"))
    try:
        db.create_user(username, password, display_name, role)
        flash(f"✓ User '{username}' created. They can login at http://localhost:5000", "success")
    except Exception as e:
        if "UNIQUE" in str(e):
            flash(f"Username '{username}' already exists", "error")
        else:
            flash(f"Error creating user: {e}", "error")
    return redirect(url_for("users"))


@app.route("/users/delete/<int:user_id>", methods=["POST"])
@admin_required
def users_delete(user_id):
    if user_id == session["user_id"]:
        flash("Cannot delete your own account", "error")
        return redirect(url_for("users"))
    db.delete_user(user_id)
    flash("User deleted", "success")
    return redirect(url_for("users"))


# ---------------- Batch history ----------------
@app.route("/batches")
@login_required
def batches():
    all_batches = db.list_batches(100)
    return render_template("batches.html", batches=all_batches)


# ---------------- Bulk Campaigns ----------------

@app.route("/campaigns")
@login_required
def campaigns():
    camps = db.list_campaigns()
    templates = db.get_templates()
    return render_template("campaigns.html", campaigns=camps, templates=templates)


@app.route("/campaigns/create", methods=["POST"])
@login_required
def campaigns_create():
    name          = request.form.get("name", "").strip()
    template_name = request.form.get("template_name", "").strip()
    # Collect variable values var_1, var_2, var_3 …
    variables = []
    for i in range(1, 6):
        v = request.form.get(f"var_{i}", "").strip()
        if v:
            variables.append(v)
    if not name or not template_name:
        flash("Campaign name and template are required", "error")
        return redirect(url_for("campaigns"))
    db.create_campaign(name, template_name, variables)
    flash(f"✓ Campaign '{name}' saved", "success")
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<int:campaign_id>/edit", methods=["POST"])
@login_required
def campaigns_edit(campaign_id):
    name          = request.form.get("name", "").strip()
    template_name = request.form.get("template_name", "").strip()
    variables = []
    for i in range(1, 6):
        v = request.form.get(f"var_{i}", "").strip()
        if v:
            variables.append(v)
    if not name or not template_name:
        flash("Name and template are required", "error")
    else:
        db.update_campaign(campaign_id, name, template_name, variables)
        flash("✓ Campaign updated", "success")
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<int:campaign_id>/delete", methods=["POST"])
@login_required
def campaigns_delete(campaign_id):
    db.delete_campaign(campaign_id)
    flash("Campaign deleted", "success")
    return redirect(url_for("campaigns"))


@app.route("/campaigns/<int:campaign_id>/send", methods=["GET", "POST"])
@login_required
def campaigns_send(campaign_id):
    camp = db.get_campaign(campaign_id)
    if not camp:
        flash("Campaign not found", "error")
        return redirect(url_for("campaigns"))
    template = db.get_template_by_name(camp["template_name"])

    if request.method == "POST":
        # Read phone list submitted from the editable textarea (as JSON)
        phones_json = request.form.get("phones_json", "").strip()
        try:
            all_phones = json.loads(phones_json) if phones_json else []
        except Exception:
            all_phones = []

        # Clean & deduplicate
        from parsers import clean_phone as _cp
        seen = set()
        cleaned = []
        for p in all_phones:
            c = _cp(str(p).strip())
            if c and c not in seen:
                seen.add(c)
                cleaned.append(c)
        all_phones = cleaned

        if not all_phones:
            flash("No valid phone numbers to send to. Add numbers on the right panel.", "error")
            return render_template("campaign_send.html", campaign=camp, template=template)

        # Filter opted-out
        opted_out_phones = {r["phone"] for r in db.search_customers(opted_out=True, limit=50000)}
        all_phones = [p for p in all_phones if p not in opted_out_phones]

        if not all_phones:
            flash("All numbers are opted out.", "error")
            return render_template("campaign_send.html", campaign=camp, template=template)

        passengers = [{"phone": p, "name": "Customer", "route": "", "platform": ""} for p in all_phones]
        batch_name = f"[Campaign] {camp['name']}"
        batch_id = scheduler.start_send_thread(
            passengers, camp["template_name"], batch_name,
            session["user_id"], fixed_params=camp["variables"],
        )
        db.update_campaign_last_used(campaign_id)
        return redirect(url_for("send_progress", batch_id=batch_id))

    return render_template("campaign_send.html", campaign=camp, template=template)


@app.route("/api/campaigns/extract", methods=["POST"])
@login_required
def campaigns_extract():
    """AJAX: upload file(s) and return count of extracted phone numbers."""
    files = request.files.getlist("phone_file")
    files = [f for f in files if f.filename]
    all_phones = set()
    for f in files:
        content = f.read()
        ext = os.path.splitext(f.filename)[1].lower()
        if ext in (".xlsx", ".xls"):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
                text_parts = []
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        for cell in row:
                            if cell is not None:
                                text_parts.append(str(cell))
                content = " ".join(text_parts).encode("utf-8")
            except Exception:
                continue
        phones = extract_all_phones(content, f.filename)
        all_phones.update(phones)
    return jsonify({"count": len(all_phones), "phones_all": list(all_phones)})


# ---------------- Webhook (incoming WhatsApp messages) ----------------

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """Meta webhook verification handshake."""
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    stored    = db.get_setting("webhook_verify_token", "")
    if mode == "subscribe" and token == stored and stored:
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    """Receive incoming messages and status updates from Meta."""
    data = request.get_json(silent=True) or {}
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # ── Incoming messages ──────────────────────────────────────
                for msg in value.get("messages", []):
                    phone = msg.get("from", "")
                    wa_id = msg.get("id", "")
                    msg_type = msg.get("type", "text")

                    # Extract text content
                    if msg_type == "text":
                        content = msg.get("text", {}).get("body", "")
                    elif msg_type == "image":
                        content = "[Image]"
                    elif msg_type == "audio":
                        content = "[Voice message]"
                    elif msg_type == "video":
                        content = "[Video]"
                    elif msg_type == "document":
                        content = f"[Document: {msg.get('document',{}).get('filename','')}]"
                    elif msg_type == "location":
                        loc = msg.get("location", {})
                        content = f"[Location: {loc.get('latitude')}, {loc.get('longitude')}]"
                    elif msg_type == "sticker":
                        content = "[Sticker]"
                    else:
                        content = f"[{msg_type}]"

                    # Resolve customer name from contacts block
                    contacts = value.get("contacts", [])
                    name = contacts[0].get("profile", {}).get("name", "") if contacts else ""

                    db.save_chat_message(
                        phone, name or None, "in", content,
                        wa_message_id=wa_id, message_type=msg_type,
                    )

                # ── Status updates (sent → delivered → read) ───────────────
                for st in value.get("statuses", []):
                    db.update_chat_status(st.get("id", ""), st.get("status", ""))

    except Exception as e:
        app.logger.error(f"Webhook error: {e}")
    return jsonify({"status": "ok"}), 200


# ---------------- Inbox (two-way chat) ----------------

@app.route("/inbox")
@login_required
def inbox():
    conversations = db.list_conversations(50)
    active_phone  = request.args.get("phone", "")
    messages      = []
    customer_name = ""
    if active_phone:
        messages = [dict(m) for m in db.get_conversation(active_phone, 100)]
        db.mark_read(active_phone)
        # Resolve name
        if messages:
            customer_name = messages[-1].get("customer_name") or active_phone
    conversations = [dict(c) for c in conversations]
    return render_template("inbox.html",
                           conversations=conversations,
                           active_phone=active_phone,
                           messages=messages,
                           customer_name=customer_name)


@app.route("/api/inbox/send", methods=["POST"])
@login_required
def inbox_send():
    data    = request.get_json(silent=True) or {}
    phone   = data.get("phone", "").strip()
    text    = data.get("text", "").strip()
    if not phone or not text:
        return jsonify({"ok": False, "error": "phone and text required"}), 400

    api = WhatsAppAPI()
    if not api.is_configured():
        return jsonify({"ok": False, "error": "WhatsApp API not configured"}), 400

    ok, result = api.send_text(phone, text)
    if ok:
        msg_id = db.save_chat_message(
            phone, None, "out", text, wa_message_id=result, status="sent"
        )
        return jsonify({"ok": True, "id": msg_id})
    return jsonify({"ok": False, "error": result}), 400


@app.route("/api/inbox/messages")
@login_required
def inbox_poll():
    phone    = request.args.get("phone", "")
    after_id = int(request.args.get("after", 0))
    if not phone:
        return jsonify([])
    msgs = [dict(m) for m in db.get_new_messages(phone, after_id)]
    return jsonify(msgs)


@app.route("/api/inbox/unread")
@login_required
def inbox_unread():
    return jsonify({"count": db.get_unread_count()})


# ---------------- Error handlers ----------------
@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page not found"), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message="Something went wrong"), 500

@app.errorhandler(413)
def too_large(e):
    flash("File too large. Maximum 20MB allowed.", "error")
    return redirect(url_for("send"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 55)
    print("🚌 Savan Travels WhatsApp Sender")
    print("=" * 55)
    print(f"📍 Open: http://localhost:{port}")
    print("👤 Login: admin / savan123")
    print("=" * 55)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
