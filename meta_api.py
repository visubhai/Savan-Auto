"""
WhatsApp Cloud API wrapper.
Uses settings from DB for token, phone_number_id, etc.
"""
import requests
import time
from database import get_setting, upsert_template


class WhatsAppAPI:
    def __init__(self):
        self.token = get_setting("access_token", "")
        self.phone_number_id = get_setting("phone_number_id", "")
        self.waba_id = get_setting("waba_id", "")
        self.api_version = get_setting("api_version", "v19.0")
        self.base_url = f"https://graph.facebook.com/{self.api_version}"

    @property
    def headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def is_configured(self):
        return bool(self.token and self.phone_number_id)

    def test_connection(self):
        """Quick check: fetch phone number details."""
        if not self.is_configured():
            return False, "Token or Phone Number ID not set"
        try:
            r = requests.get(
                f"{self.base_url}/{self.phone_number_id}",
                headers=self.headers,
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                return True, f"Connected as {data.get('display_phone_number', 'unknown')}"
            return False, f"Error {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)

    def send_template(self, to_phone, template_name, language, parameters,
                       header_type=None, header_example=None, header_media_id=None):
        """
        Send a template message.
        to_phone:         E.164 format without + (e.g., '919913191384')
        parameters:       list of strings for {{1}}, {{2}}, etc. in the body
        header_type:      None / "TEXT" / "IMAGE" / "VIDEO" / "DOCUMENT".
                          Media-header templates MUST be sent with a
                          matching header component or Meta returns 132012.
        header_media_id:  PREFERRED — Meta media_id from upload_media().
                          This is the only reliable way to send media
                          headers; the example URL alone usually fails.
        header_example:   Fallback URL (kept for back-compat).
        Returns: (success: bool, message_id_or_error: str)
        """
        url = f"{self.base_url}/{self.phone_number_id}/messages"
        components = []

        # Header (image / video / document) — Meta requires this to match
        # the template's declared format.
        ht = (header_type or "").upper()
        if ht in ("IMAGE", "VIDEO", "DOCUMENT") and (header_media_id or header_example):
            kind = ht.lower()
            if header_media_id:
                media = {"id": header_media_id}
            else:
                # Fallback to URL — will likely fail for scontent.whatsapp.net
                # URLs but kept so the call doesn't 132012 outright.
                media = {"link": header_example}
            if kind == "document":
                # Meta wants a display filename for documents
                media["filename"] = "Savan_Travels.pdf"
            components.append({
                "type": "header",
                "parameters": [{"type": kind, kind: media}],
            })
        # TEXT-header templates with a {{1}} in the header would need a
        # separate text parameter — handle that day we add such a template.

        if parameters:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": str(p)} for p in parameters],
            })
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": components,
            },
        }
        max_retries = int(get_setting("max_retries", "3"))
        for attempt in range(max_retries):
            try:
                r = requests.post(url, headers=self.headers, json=payload, timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    msg_id = data.get("messages", [{}])[0].get("id", "unknown")
                    return True, msg_id
                # Rate-limit retry
                if r.status_code == 429:
                    wait = 2 ** attempt
                    time.sleep(wait)
                    continue
                # Permanent failure
                try:
                    err = r.json().get("error", {})
                    msg = err.get("message", r.text[:200])
                    code = err.get("code", r.status_code)
                    return False, f"[{code}] {msg}"
                except Exception:
                    return False, f"HTTP {r.status_code}: {r.text[:200]}"
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return False, "Request timeout"
            except Exception as e:
                return False, str(e)
        return False, "Max retries exhausted"

    def send_text(self, to_phone, text):
        """Send a free-form text message (only within 24-hour customer service window)."""
        url = f"{self.base_url}/{self.phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone,
            "type": "text",
            "text": {"body": text},
        }
        try:
            r = requests.post(url, headers=self.headers, json=payload, timeout=15)
            if r.status_code == 200:
                msg_id = r.json().get("messages", [{}])[0].get("id", "unknown")
                return True, msg_id
            try:
                err = r.json().get("error", {})
                return False, f"[{err.get('code', r.status_code)}] {err.get('message', r.text[:200])}"
            except Exception:
                return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)

    # In-memory cache for the tier info — Meta's response barely changes
    # within a day, but the dashboard hits this on every load. 5 min TTL.
    _phone_info_cache = {"data": None, "expires_at": 0.0, "token_hash": None}

    def get_phone_info_cached(self, ttl_seconds=300):
        """Same as get_phone_info() but returns a cached value within the TTL.

        The cache is keyed by a short hash of the current access token so it
        invalidates automatically if you rotate tokens in Settings.
        """
        import time, hashlib
        c = WhatsAppAPI._phone_info_cache
        tok_hash = hashlib.md5((self.token or "").encode()).hexdigest()[:8]
        if (c["data"] is not None
                and c["expires_at"] > time.time()
                and c["token_hash"] == tok_hash):
            return c["data"]
        fresh = self.get_phone_info()
        if not (fresh or {}).get("error"):
            WhatsAppAPI._phone_info_cache = {
                "data": fresh,
                "expires_at": time.time() + ttl_seconds,
                "token_hash": tok_hash,
            }
        return fresh

    def get_phone_info(self):
        """Fetch tier + quality info for the configured phone number.

        Returns a dict like:
          {
            "messaging_limit_tier": "TIER_1K",   # one of TIER_50/250/1K/10K/100K/UNLIMITED
            "verified_name": "Savan Travels",
            "display_phone_number": "+91 7567 529300",
            "quality_rating": "GREEN",            # GREEN / YELLOW / RED
            "tier_limit": 1000,                   # numeric, None for unlimited
          }
        or {"error": "..."} on failure.
        """
        if not self.is_configured():
            return {"error": "API not configured"}
        try:
            r = requests.get(
                f"{self.base_url}/{self.phone_number_id}",
                headers=self.headers,
                params={
                    "fields": "messaging_limit_tier,verified_name,display_phone_number,quality_rating,name_status"
                },
                timeout=10,
            )
            if r.status_code != 200:
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
            data = r.json()
            # Map tier string → numeric daily limit for "unique business-initiated
            # conversations" (Meta's quota; service replies don't count here).
            # Meta's current ladder (as shown in Business Manager UI):
            #   250 → 2000 → 10K → 100K → Unlimited
            # Older accounts may still see TIER_50 / TIER_1K from the API.
            tier_map = {
                "TIER_50":         50,
                "TIER_250":        250,
                "TIER_1K":         1_000,
                "TIER_2K":         2_000,
                "TIER_10K":        10_000,
                "TIER_100K":       100_000,
                "TIER_UNLIMITED":  None,
            }
            data["tier_limit"] = tier_map.get(
                (data.get("messaging_limit_tier") or "").upper(), None
            )
            return data
        except Exception as e:
            return {"error": str(e)}

    def upload_media(self, content_bytes, mime_type, filename="media"):
        """Upload bytes to Meta's Media API, return media_id (valid ~30 days).

        Used to re-host template header media: Meta's `header_handle` URLs
        in template metadata (scontent.whatsapp.net/...) cannot be used as
        media sources during send — they're internal CDN URLs. The fix is
        to upload our own copy and reference it via media_id.
        """
        if not self.is_configured() or not content_bytes:
            return None
        url = f"{self.base_url}/{self.phone_number_id}/media"
        # Pick a sensible extension based on the MIME type for the filename
        ext_for = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                   "video/mp4": ".mp4", "application/pdf": ".pdf"}
        fname = filename + ext_for.get((mime_type or "").lower(), "")
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                data={"messaging_product": "whatsapp", "type": mime_type or "image/jpeg"},
                files={"file": (fname, content_bytes, mime_type or "image/jpeg")},
                timeout=30,
            )
            if r.status_code != 200:
                print(f"[upload_media] {r.status_code}: {r.text[:200]}")
                return None
            return (r.json() or {}).get("id")
        except Exception as e:
            print(f"[upload_media] {e}")
            return None

    def download_media(self, media_id):
        """Download an incoming media file by its media_id.

        Two-step: GET /{media_id} → temporary URL, then GET that URL with
        the bearer token. Returns (bytes, mime_type) or (None, None) on
        any failure. Meta's URL expires in ~5 minutes so download must
        happen synchronously during webhook handling.
        """
        if not self.token or not media_id:
            return None, None
        try:
            r = requests.get(
                f"{self.base_url}/{media_id}",
                headers=self.headers, timeout=15,
            )
            if r.status_code != 200:
                return None, None
            meta = r.json()
            url = meta.get("url")
            mime = meta.get("mime_type") or ""
            if not url:
                return None, None
            r2 = requests.get(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=30,
            )
            if r2.status_code != 200:
                return None, None
            return r2.content, mime
        except Exception:
            return None, None

    def fetch_templates(self):
        """Fetch all approved templates from Meta and sync to DB.

        Captures body + variable count AND header metadata
        (TEXT / IMAGE / VIDEO / DOCUMENT) + the example media URL Meta
        returns. This is what lets send_template include the header
        component — without it, Meta rejects with error 132012
        "Parameter format does not match" for any template that has
        a media header.
        """
        if not self.token or not self.waba_id:
            return False, "Token or WABA ID not set"
        url = f"{self.base_url}/{self.waba_id}/message_templates"
        try:
            r = requests.get(url, headers=self.headers,
                             params={"limit": 100}, timeout=15)
            if r.status_code != 200:
                return False, f"Error {r.status_code}: {r.text[:200]}"
            data = r.json().get("data", [])
            import re, json as _json
            synced = 0
            for tpl in data:
                name      = tpl.get("name")
                language  = tpl.get("language", "en")
                category  = tpl.get("category", "UTILITY")
                status    = tpl.get("status", "unknown").lower()

                body            = ""
                var_count       = 0
                header_type     = None    # None / "TEXT" / "IMAGE" / "VIDEO" / "DOCUMENT"
                header_example  = None    # raw Meta preview URL (kept for ref / fallback)
                header_media_id = None    # what we'll use when sending
                buttons         = None    # raw buttons list JSON-encoded

                for comp in tpl.get("components", []):
                    ctype = (comp.get("type") or "").upper()
                    if ctype == "BODY":
                        body = comp.get("text", "") or ""
                        vars_found = re.findall(r"\{\{(\d+)\}\}", body)
                        var_count = len(set(vars_found))
                    elif ctype == "HEADER":
                        header_type = (comp.get("format") or "").upper() or None
                        if header_type in ("IMAGE", "VIDEO", "DOCUMENT"):
                            handles = (comp.get("example") or {}).get("header_handle") or []
                            if handles:
                                header_example = handles[0]
                                # Don't re-host if the user has uploaded a custom image —
                                # preserve their choice across syncs. Lookup the existing
                                # row lazily so a brand-new template still gets a media_id.
                                from database import get_template_by_name
                                existing = get_template_by_name(name)
                                if existing and (existing.get("header_image_is_custom") or 0):
                                    header_media_id = existing.get("header_media_id")
                                else:
                                    try:
                                        img = requests.get(header_example, timeout=30)
                                        if img.status_code == 200 and img.content:
                                            mime = img.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                                            header_media_id = self.upload_media(
                                                img.content, mime, filename=name,
                                            )
                                    except Exception as e:
                                        print(f"[fetch_templates] header re-host failed for {name}: {e}")
                    elif ctype == "BUTTONS":
                        try:
                            buttons = _json.dumps(comp.get("buttons") or [])
                        except Exception:
                            buttons = None

                upsert_template(
                    name, language, category, body, var_count, status,
                    header_type=header_type, header_example=header_example,
                    header_media_id=header_media_id, buttons=buttons,
                )
                synced += 1
            return True, f"Synced {synced} templates"
        except Exception as e:
            return False, str(e)
