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
    get_campaign, update_campaign_image, clear_campaign_image,
    update_template_meta, _now,
)
from meta_api import WhatsAppAPI
import os
import requests
from datetime import datetime, timedelta, timezone

def now_ist():
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)

def refresh_template_media(template_name):
    """Ensure the template's header_media_id is valid (not expired).
    Returns the valid media_id, or None if not applicable/failed.
    """
    template = get_template_by_name(template_name)
    if not template:
        return None
    
    header_type = (template.get("header_type") or "").upper()
    if header_type not in ("IMAGE", "VIDEO", "DOCUMENT"):
        return None
        
    media_id = template.get("header_media_id")
    uploaded_at = template.get("header_image_uploaded_at")
    
    # Check if media_id is expired (older than 25 days) or missing
    need_refresh = False
    if not media_id:
        need_refresh = True
    elif uploaded_at:
        try:
            dt = datetime.strptime(uploaded_at, "%Y-%m-%d %H:%M:%S")
            if (now_ist() - dt).days >= 25:
                need_refresh = True
        except Exception:
            need_refresh = True
    else:
        need_refresh = True
        
    if not need_refresh:
        return media_id

    # Try to re-upload
    local_path = os.path.join("uploads", "media", f"template_{template_name}")
    content = None
    mime = template.get("header_image_mime") or "image/jpeg"
    if os.path.exists(local_path):
        try:
            with open(local_path, "rb") as f:
                content = f.read()
        except Exception as e:
            print(f"[refresh_template_media] Failed to read local template file: {e}")

    if not content and template.get("header_example"):
        try:
            r = requests.get(template["header_example"], timeout=30)
            if r.status_code == 200 and r.content:
                content = r.content
                mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                os.makedirs(os.path.join("uploads", "media"), exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(content)
        except Exception as e:
            print(f"[refresh_template_media] Failed to fetch template header example: {e}")

    if content:
        try:
            api = WhatsAppAPI()
            new_media_id = api.upload_media(content, mime, filename=template_name)
            if new_media_id:
                update_template_meta(
                    template_name,
                    header_media_id=new_media_id,
                    header_image_uploaded_at=_now(),
                    header_image_mime=mime,
                    header_image_size=len(content)
                )
                print(f"[refresh_template_media] Refreshed template {template_name} media_id to {new_media_id}")
                return new_media_id
        except Exception as e:
            print(f"[refresh_template_media] Failed to upload template media: {e}")

    return media_id


def refresh_campaign_media(campaign_id):
    """Ensure the campaign's custom header_media_id is valid.
    Returns the valid media_id, or None if not applicable/failed.
    """
    try:
        camp = get_campaign(int(campaign_id))
    except Exception:
        return None
    if not camp:
        return None

    media_id = camp.get("header_media_id")
    if not media_id:
        return None

    uploaded_at = camp.get("header_image_uploaded_at")
    need_refresh = False
    if uploaded_at:
        try:
            dt = datetime.strptime(uploaded_at, "%Y-%m-%d %H:%M:%S")
            if (now_ist() - dt).days >= 25:
                need_refresh = True
        except Exception:
            need_refresh = True
    else:
        need_refresh = True

    if not need_refresh:
        return media_id

    local_path = os.path.join("uploads", "media", f"campaign_{campaign_id}")
    if os.path.exists(local_path):
        try:
            with open(local_path, "rb") as f:
                content = f.read()
            mime = camp.get("header_image_mime") or "image/jpeg"
            api = WhatsAppAPI()
            new_media_id = api.upload_media(content, mime, filename=f"camp_{campaign_id}")
            if new_media_id:
                update_campaign_image(campaign_id, new_media_id, mime, len(content))
                print(f"[refresh_campaign_media] Refreshed campaign {campaign_id} media_id to {new_media_id}")
                return new_media_id
        except Exception as e:
            print(f"[refresh_campaign_media] Failed to refresh campaign media: {e}")
    else:
        clear_campaign_image(campaign_id)
        print(f"[refresh_campaign_media] Local file for expired campaign {campaign_id} custom image is missing. Cleared override.")
        return None

    return media_id


# Track running batches: {batch_id: {'total', 'sent', 'failed', 'current_name', 'status'}}
RUNNING_BATCHES = {}


def send_batch(batch_id, passengers, template_name, user_id=None, fixed_params=None,
               var_overrides=None, header_media_id_override=None, campaign_id=None):
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

    # Refresh template and campaign media IDs if needed to prevent 30-day expiration issues
    if campaign_id:
        header_media_id_override = refresh_campaign_media(campaign_id)
    
    template_media_id = refresh_template_media(template_name)
    final_header_media_id = header_media_id_override or template_media_id

    api = WhatsAppAPI()
    delay = float(get_setting("delay_between_messages", "1.5"))

    RUNNING_BATCHES[batch_id] = {
        "total": len(passengers), "sent": 0, "failed": 0,
        "current_name": "", "status": "running",
    }

    try:
        for idx, p in enumerate(passengers):
            phone = str(p.get("phone") or "").strip()
            name = p.get("name", "")
            route = p.get("route", "")
            platform = p.get("platform", "")
            if batch_id in RUNNING_BATCHES:
                RUNNING_BATCHES[batch_id]["current_name"] = name or phone or "Customer"

            # Check if message is sendable (phone number check)
            clean_digits = "".join(filter(str.isdigit, phone))
            if not clean_digits or len(clean_digits) < 10:
                error_msg = f"Invalid or unsendeable phone number ('{phone}')"
                try:
                    log_message(
                        batch_id, phone or "unknown", name, route,
                        platform, template_name, "failed", error=error_msg,
                        params=[]
                    )
                    update_batch_counts(batch_id, failed=1)
                except Exception:
                    pass
                if batch_id in RUNNING_BATCHES:
                    RUNNING_BATCHES[batch_id]["failed"] += 1
                if idx < len(passengers) - 1:
                    time.sleep(delay)
                continue

            params = []
            try:
                # Build parameters — use passenger params if available, else fixed_params, else per-passenger data
                if p.get("params") is not None:
                    params = list(p["params"][:var_count])
                    while len(params) < var_count:
                        params.append("—")
                elif fixed_params is not None:
                    params = list(fixed_params[:var_count])
                    while len(params) < var_count:
                        params.append("—")
                else:
                    ov = var_overrides or {}
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

                    for i in range(1, var_count + 1):
                        fixed = ov.get(str(i)) or ov.get(i)
                        if fixed:
                            params.append(str(fixed))
                        elif i <= len(auto_sources):
                            params.append(auto_sources[i - 1](p))
                        else:
                            params.append("—")

                success, result = api.send_template(
                    clean_digits, template_name, language, params,
                    header_type=template.get("header_type"),
                    header_example=template.get("header_example"),
                    header_media_id=final_header_media_id,
                )

                if success:
                    log_message(
                        batch_id, clean_digits, name, route,
                        platform, template_name, "sent", wa_msg_id=result,
                        params=params
                    )
                    try:
                        upsert_customer(clean_digits, name, route, platform)
                    except Exception:
                        pass
                    update_batch_counts(batch_id, sent=1)
                    if batch_id in RUNNING_BATCHES:
                        RUNNING_BATCHES[batch_id]["sent"] += 1
                else:
                    log_message(
                        batch_id, clean_digits, name, route,
                        platform, template_name, "failed", error=str(result or "Message not sendable"),
                        params=params
                    )
                    update_batch_counts(batch_id, failed=1)
                    if batch_id in RUNNING_BATCHES:
                        RUNNING_BATCHES[batch_id]["failed"] += 1

            except Exception as e:
                err_str = f"Error sending message: {str(e)}"
                try:
                    log_message(
                        batch_id, clean_digits or phone or "unknown", name, route,
                        platform, template_name, "failed", error=err_str,
                        params=params
                    )
                    update_batch_counts(batch_id, failed=1)
                except Exception:
                    pass
                if batch_id in RUNNING_BATCHES:
                    RUNNING_BATCHES[batch_id]["failed"] += 1

            # Throttle
            if idx < len(passengers) - 1:
                time.sleep(delay)
    except Exception as outer_err:
        if batch_id in RUNNING_BATCHES:
            RUNNING_BATCHES[batch_id]["error"] = str(outer_err)
    finally:
        complete_batch(batch_id)
        if batch_id in RUNNING_BATCHES:
            RUNNING_BATCHES[batch_id]["status"] = "completed"


def start_send_thread(passengers, template_name, batch_name, user_id, fixed_params=None,
                      var_overrides=None, header_media_id_override=None, campaign_id=None):
    """Create a batch and start a sending thread. Returns batch_id."""
    batch_id = create_batch(batch_name, template_name, len(passengers), user_id)
    thread = threading.Thread(
        target=send_batch,
        args=(batch_id, passengers, template_name, user_id, fixed_params,
              var_overrides, header_media_id_override, campaign_id),
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
