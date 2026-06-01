"""
Media storage abstraction.

If R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET are
set in the environment, customer-sent media is uploaded to Cloudflare R2
and survives Render redeploys. Otherwise it falls back to the local
uploads/media/ directory (ephemeral on Render's free tier).

R2 is S3-compatible, so we use boto3.
"""
import os
from dotenv import load_dotenv

load_dotenv()

R2_ENDPOINT      = os.environ.get("R2_ENDPOINT", "").strip()
R2_ACCESS_KEY    = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_KEY    = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET        = os.environ.get("R2_BUCKET", "").strip()
# Optional: how many seconds presigned URLs stay valid (default 1 hour)
R2_URL_EXPIRES   = int(os.environ.get("R2_URL_EXPIRES", "3600"))

_client = None


def is_configured():
    return bool(R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET_KEY and R2_BUCKET)


def _client_singleton():
    global _client
    if _client is None:
        # Import boto3 lazily so the app still starts if the library isn't
        # installed yet (e.g. during a partial deploy).
        import boto3
        from botocore.client import Config
        _client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _client


def upload_bytes(key, data, mime_type=None):
    """Upload bytes to R2 under `key`. Returns the key on success, None on failure."""
    if not is_configured():
        return None
    try:
        extra = {}
        if mime_type:
            extra["ContentType"] = mime_type
        _client_singleton().put_object(
            Bucket=R2_BUCKET, Key=key, Body=data, **extra
        )
        return key
    except Exception as e:
        # Caller decides whether to fall back to local; do not raise.
        print(f"[storage] R2 upload failed for {key}: {e}")
        return None


def signed_url(key, expires=None, filename=None):
    """Return a presigned URL valid for `expires` seconds, or None on failure."""
    if not is_configured():
        return None
    try:
        params = {"Bucket": R2_BUCKET, "Key": key}
        if filename:
            # Tell the browser a friendly download name (used for documents)
            params["ResponseContentDisposition"] = f'inline; filename="{filename}"'
        return _client_singleton().generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires or R2_URL_EXPIRES,
        )
    except Exception as e:
        print(f"[storage] R2 signed_url failed for {key}: {e}")
        return None
