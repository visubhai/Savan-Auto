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

    def send_template(self, to_phone, template_name, language, parameters):
        """
        Send a template message.
        to_phone: E.164 format without + (e.g., '919913191384')
        parameters: list of strings for {{1}}, {{2}}, etc.
        Returns: (success: bool, message_id_or_error: str)
        """
        url = f"{self.base_url}/{self.phone_number_id}/messages"
        components = []
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
        """Fetch all approved templates from Meta and sync to DB."""
        if not self.token or not self.waba_id:
            return False, "Token or WABA ID not set"
        url = f"{self.base_url}/{self.waba_id}/message_templates"
        try:
            r = requests.get(url, headers=self.headers,
                             params={"limit": 100}, timeout=15)
            if r.status_code != 200:
                return False, f"Error {r.status_code}: {r.text[:200]}"
            data = r.json().get("data", [])
            synced = 0
            for tpl in data:
                name = tpl.get("name")
                language = tpl.get("language", "en")
                category = tpl.get("category", "UTILITY")
                status = tpl.get("status", "unknown").lower()
                # Extract body and variable count
                body = ""
                var_count = 0
                for comp in tpl.get("components", []):
                    if comp.get("type") == "BODY":
                        body = comp.get("text", "")
                        # Count {{n}} occurrences
                        import re
                        vars_found = re.findall(r"\{\{(\d+)\}\}", body)
                        var_count = len(set(vars_found))
                upsert_template(name, language, category, body, var_count, status)
                synced += 1
            return True, f"Synced {synced} templates"
        except Exception as e:
            return False, str(e)
