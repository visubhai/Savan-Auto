"""
Background scheduler that polls scheduled_jobs every minute and triggers sending.
Runs in a daemon thread when Flask app starts.
"""
import threading
import time
import json
from database import (
    get_due_jobs, update_scheduled_job, create_batch,
    log_message, update_batch_counts, complete_batch,
    upsert_customer, get_template_by_name, get_setting,
)
from meta_api import WhatsAppAPI


# Track running batches: {batch_id: {'total', 'sent', 'failed', 'current_name', 'status'}}
RUNNING_BATCHES = {}


def send_batch(batch_id, passengers, template_name, user_id=None, fixed_params=None,
               var_overrides=None, header_media_id_override=None):
    """Send a batch of messages. Runs in a background thread.

    var_overrides: optional dict {var_index(str/int): fixed_value}. When a value
    is provided for a variable position, that fixed value is used for ALL
    passengers in the batch (instead of the auto per-passenger value). Useful
    when a template needs variables that aren't present in the CSV.

    header_media_id_override: optional Meta media_id. When set, overrides the
    template's default header image for this batch. Lets one approved template
    power many use-cases (Diwali, Monsoon, Holiday…) each with its own banner.
    """
    template = get_template_by_name(template_name)
    if not template:
        RUNNING_BATCHES[batch_id] = {
            "total": len(passengers), "sent": 0, "failed": len(passengers),
            "current_name": "", "status": "error",
            "error": f"Template '{template_name}' not found in database. Go to Templates → Sync Templates.",
        }
        complete_batch(batch_id)
        return

    if template.get("status", "").lower() not in ("approved", "active"):
        error_msg = (
            f"Template '{template_name}' is not approved (status: {template.get('status', 'unknown')}). "
            f"Go to Templates → Sync Templates, then check Meta Business Manager."
        )
        for p in passengers:
            log_message(
                batch_id, p["phone"], p.get("name"), p.get("route"),
                p.get("platform"), template_name, "failed", error=error_msg,
            )
        update_batch_counts(batch_id, failed=len(passengers))
        complete_batch(batch_id)
        RUNNING_BATCHES[batch_id] = {
            "total": len(passengers), "sent": 0, "failed": len(passengers),
            "current_name": "", "status": "completed",
        }
        return

    language = template["language"]
    var_count = template["variable_count"]

    api = WhatsAppAPI()
    delay = float(get_setting("delay_between_messages", "1.5"))

    RUNNING_BATCHES[batch_id] = {
        "total": len(passengers), "sent": 0, "failed": 0,
        "current_name": "", "status": "running",
    }

    for idx, p in enumerate(passengers):
        # Build parameters — use fixed_params for bulk campaigns, else per-passenger data
        if fixed_params is not None:
            params = list(fixed_params[:var_count])
            # Pad if not enough values provided
            while len(params) < var_count:
                params.append("—")
        else:
            ov = var_overrides or {}
            # Follow-up templates need PLATFORM in {{1}} (e.g. "Aapne RedBus
            # pe review nahi diya..."), while reminder templates need
            # passenger NAME in {{1}}. Detect by template name and pick the
            # right auto-source list per variable position.
            is_followup = bool(template_name and "follow" in template_name.lower())
            if is_followup:
                auto_sources = [
                    lambda p: p.get("platform") or "the platform",
                    lambda p: p.get("name") or "Customer",
                    lambda p: p.get("route") or "your journey",
                ]
            else:
                auto_sources = [
                    lambda p: p.get("name") or "Customer",
                    lambda p: p.get("route") or "your journey",
                    lambda p: p.get("platform") or "the platform",
                ]

            params = []
            for i in range(1, var_count + 1):
                fixed = ov.get(str(i)) or ov.get(i)
                if fixed:
                    # User-supplied fixed value for the whole batch
                    params.append(str(fixed))
                elif i <= len(auto_sources):
                    params.append(auto_sources[i - 1](p))
                else:
                    # No auto source and no fixed value provided
                    params.append("—")

        RUNNING_BATCHES[batch_id]["current_name"] = p.get("name", "")

        success, result = api.send_template(
            p["phone"], template_name, language, params,
            header_type=template.get("header_type"),
            header_example=template.get("header_example"),
            header_media_id=(header_media_id_override or template.get("header_media_id")),
        )

        if success:
            log_message(
                batch_id, p["phone"], p.get("name"), p.get("route"),
                p.get("platform"), template_name, "sent", wa_msg_id=result,
            )
            upsert_customer(p["phone"], p.get("name"),
                            p.get("route"), p.get("platform"))
            update_batch_counts(batch_id, sent=1)
            RUNNING_BATCHES[batch_id]["sent"] += 1
        else:
            log_message(
                batch_id, p["phone"], p.get("name"), p.get("route"),
                p.get("platform"), template_name, "failed", error=result,
            )
            update_batch_counts(batch_id, failed=1)
            RUNNING_BATCHES[batch_id]["failed"] += 1

        # Throttle
        if idx < len(passengers) - 1:
            time.sleep(delay)

    complete_batch(batch_id)
    RUNNING_BATCHES[batch_id]["status"] = "completed"


def start_send_thread(passengers, template_name, batch_name, user_id, fixed_params=None,
                      var_overrides=None, header_media_id_override=None):
    """Create a batch and start a sending thread. Returns batch_id."""
    batch_id = create_batch(batch_name, template_name, len(passengers), user_id)
    thread = threading.Thread(
        target=send_batch,
        args=(batch_id, passengers, template_name, user_id, fixed_params,
              var_overrides, header_media_id_override),
        daemon=True,
    )
    thread.start()
    return batch_id


def scheduler_loop():
    """Check for due scheduled jobs every minute."""
    while True:
        try:
            due = get_due_jobs()
            for job in due:
                try:
                    passengers = json.loads(job["csv_data"])
                    # Optional fixed variable overrides stored with the job.
                    # Works for both MongoDB dicts and sqlite3.Row (no .get()).
                    var_overrides = {}
                    try:
                        raw_ov = job["var_overrides"]
                    except (KeyError, IndexError):
                        raw_ov = None
                    if raw_ov:
                        try:
                            var_overrides = json.loads(raw_ov) if isinstance(raw_ov, str) else raw_ov
                        except Exception:
                            var_overrides = {}
                    batch_id = create_batch(
                        f"Scheduled: {job['name']}", job["template_name"],
                        len(passengers), job["created_by"],
                    )
                    update_scheduled_job(job["id"], "running", batch_id)
                    thread = threading.Thread(
                        target=_run_scheduled,
                        args=(job["id"], batch_id, passengers,
                              job["template_name"], var_overrides),
                        daemon=True,
                    )
                    thread.start()
                except Exception as e:
                    update_scheduled_job(job["id"], "error")
                    print(f"Failed to start scheduled job {job['id']}: {e}")
        except Exception as e:
            print(f"Scheduler error: {e}")
        time.sleep(60)


def _run_scheduled(job_id, batch_id, passengers, template_name, var_overrides=None):
    send_batch(batch_id, passengers, template_name, var_overrides=var_overrides)
    update_scheduled_job(job_id, "completed", batch_id)


def start_scheduler():
    """Start the scheduler in a daemon thread."""
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
