"""
Gmail IMAP fetcher for RedBus review CSVs (and any other attachment-based
report you want to pull automatically).

Setup once:
  1. Enable 2-Step Verification on the Gmail account
  2. Visit https://myaccount.google.com/apppasswords and create an
     App Password (16 characters, no spaces)
  3. Save Gmail address + App Password in this app's Settings page

The settings DB keys this module reads:
  - gmail_address          (e.g. savan.reviews@gmail.com)
  - gmail_app_password     (the 16-char App Password)
  - gmail_sender_filter    (e.g. noreply@redbus.com   — optional)
  - gmail_subject_filter   (e.g. Ratings              — optional)
  - gmail_last_uid         (managed by us, do not edit manually)
"""
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT   = 993


def _get_setting(key, default=""):
    from database import get_setting
    return get_setting(key, default)


def _set_setting(key, value):
    from database import set_setting
    set_setting(key, value)


def is_configured():
    """True iff a Gmail address and App Password are saved."""
    return bool(_get_setting("gmail_address") and _get_setting("gmail_app_password"))


# ── Connection ────────────────────────────────────────────────────────────────
def _connect():
    address  = _get_setting("gmail_address", "").strip()
    password = _get_setting("gmail_app_password", "").strip()
    if not address or not password:
        raise RuntimeError("Gmail not configured. Save address and App Password in Settings.")
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(address, password)
    return mail


def test_connection():
    """Return (ok, message). Used by the Settings 'Test Gmail' button."""
    try:
        mail = _connect()
        status, _ = mail.select("INBOX", readonly=True)
        mail.logout()
        if status == "OK":
            return True, f"Connected as {_get_setting('gmail_address')}"
        return False, f"INBOX select failed: {status}"
    except imaplib.IMAP4.error as e:
        return False, f"Login failed — check the App Password ({e})"
    except Exception as e:
        return False, f"Connection error: {e}"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _decode_hdr(value):
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for t, charset in parts:
        if isinstance(t, bytes):
            try:
                t = t.decode(charset or "utf-8", errors="replace")
            except Exception:
                t = t.decode("latin-1", errors="replace")
        out.append(t)
    return "".join(out)


def _build_search(since_days, sender, subject):
    parts = []
    since = (datetime.utcnow() - timedelta(days=int(since_days))).strftime("%d-%b-%Y")
    parts.append(f'SINCE {since}')
    if sender:
        parts.append(f'FROM "{sender}"')
    if subject:
        parts.append(f'SUBJECT "{subject}"')
    return " ".join(parts)


# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_attachments(since_days=7, only_new=True, mark_seen=True):
    """Search Gmail and return matching emails with attachments.

    Args:
      since_days: IMAP SINCE window (default 7 days)
      only_new:   if True, only return UIDs greater than gmail_last_uid
      mark_seen:  if True, mark each fetched message as Seen so the next
                  call returns only newer messages

    Returns: list of {
        uid, from, subject, date, message_id,
        attachments: [ {filename, content_bytes} ]
    } — only messages that actually have attachments.
    """
    sender  = _get_setting("gmail_sender_filter").strip()
    subject = _get_setting("gmail_subject_filter").strip()
    last_uid_raw = _get_setting("gmail_last_uid", "0") or "0"
    try:
        last_uid = int(last_uid_raw)
    except ValueError:
        last_uid = 0

    mail = _connect()
    try:
        mail.select("INBOX")
        status, data = mail.uid("search", None, _build_search(since_days, sender, subject))
        if status != "OK":
            return []
        uids = data[0].split() if data and data[0] else []

        results = []
        max_uid_seen = last_uid
        for uid in uids:
            uid_int = int(uid.decode())
            if only_new and uid_int <= last_uid:
                continue

            status, msg_data = mail.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            attachments = []
            if msg.is_multipart():
                for part in msg.walk():
                    disp = (part.get("Content-Disposition") or "").lower()
                    if "attachment" not in disp:
                        continue
                    fname = _decode_hdr(part.get_filename() or "")
                    payload = part.get_payload(decode=True)
                    if fname and payload:
                        attachments.append({"filename": fname, "content_bytes": payload})

            if attachments:
                results.append({
                    "uid":         uid_int,
                    "from":        _decode_hdr(msg.get("From", "")),
                    "subject":     _decode_hdr(msg.get("Subject", "")),
                    "date":        msg.get("Date", ""),
                    "message_id":  msg.get("Message-ID", ""),
                    "attachments": attachments,
                })
                if mark_seen:
                    try:
                        mail.uid("store", uid, "+FLAGS", "\\Seen")
                    except Exception:
                        pass
                if uid_int > max_uid_seen:
                    max_uid_seen = uid_int

        # Persist watermark so the next run skips what we just processed
        if only_new and max_uid_seen > last_uid:
            _set_setting("gmail_last_uid", str(max_uid_seen))

        return results
    finally:
        try:
            mail.logout()
        except Exception:
            pass
