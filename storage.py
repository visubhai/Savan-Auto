"""
Persistent media storage with multiple backends.

Order of preference for new uploads:
  1. Cloudflare R2 — set R2_ENDPOINT/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET
  2. MongoDB GridFS — automatic when MONGO_URI is set (uses the same Atlas
     database; free tier is 512 MB total). No card required.
  3. Local disk — uploads/media/ (ephemeral on Render free tier)

Each chat row stores which backend its file lives in (storage_kind) so
serving works correctly even after switching backends.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── R2 (Cloudflare) ──────────────────────────────────────────────────────────
R2_ENDPOINT    = os.environ.get("R2_ENDPOINT", "").strip()
R2_ACCESS_KEY  = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_KEY  = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET      = os.environ.get("R2_BUCKET", "").strip()
R2_URL_EXPIRES = int(os.environ.get("R2_URL_EXPIRES", "3600"))

# ── GridFS (MongoDB) — uses the same Atlas database app already connects to ─
MONGO_URI = os.environ.get("MONGO_URI", "").strip()

# Backend identifiers stored alongside each media row
KIND_R2     = "r2"
KIND_GRIDFS = "gridfs"
KIND_LOCAL  = "local"

_r2_client     = None
_gridfs_bucket = None


# ── R2 helpers ────────────────────────────────────────────────────────────────
def is_r2_configured():
    return bool(R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET_KEY and R2_BUCKET)


def _r2():
    global _r2_client
    if _r2_client is None:
        import boto3
        from botocore.client import Config
        _r2_client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _r2_client


def _r2_upload(key, data, mime_type):
    try:
        extra = {"ContentType": mime_type} if mime_type else {}
        _r2().put_object(Bucket=R2_BUCKET, Key=key, Body=data, **extra)
        return True
    except Exception as e:
        print(f"[storage] R2 upload failed for {key}: {e}")
        return False


def signed_url(key, expires=None, filename=None):
    """Return a short-lived presigned R2 URL, or None on failure / not configured."""
    if not is_r2_configured():
        return None
    try:
        params = {"Bucket": R2_BUCKET, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'inline; filename="{filename}"'
        return _r2().generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires or R2_URL_EXPIRES,
        )
    except Exception as e:
        print(f"[storage] R2 signed_url failed for {key}: {e}")
        return None


# ── GridFS helpers ───────────────────────────────────────────────────────────
def is_gridfs_configured():
    return bool(MONGO_URI)


def _grid():
    """Return a GridFSBucket bound to the same Mongo database the app uses."""
    global _gridfs_bucket
    if _gridfs_bucket is None:
        from gridfs import GridFSBucket
        from database import _db  # MongoDB-only attribute; safe because we
                                  # only get here when MONGO_URI is set
        _gridfs_bucket = GridFSBucket(_db(), bucket_name="media")
    return _gridfs_bucket


def _gridfs_upload(key, data, mime_type):
    import io
    try:
        # Delete any prior file with the same name to avoid accumulating
        # versions on rare retries.
        try:
            for f in _grid().find({"filename": key}):
                _grid().delete(f._id)
        except Exception:
            pass
        _grid().upload_from_stream(
            key, io.BytesIO(data),
            metadata={"mime_type": mime_type or ""},
        )
        return True
    except Exception as e:
        print(f"[storage] GridFS upload failed for {key}: {e}")
        return False


def gridfs_read(key):
    """Return (bytes, mime_type) for a GridFS file, or (None, None) on failure."""
    if not is_gridfs_configured():
        return None, None
    try:
        stream = _grid().open_download_stream_by_name(key)
        meta = stream.metadata or {}
        return stream.read(), meta.get("mime_type") or ""
    except Exception as e:
        print(f"[storage] GridFS read failed for {key}: {e}")
        return None, None


# ── Public API ───────────────────────────────────────────────────────────────
def is_configured():
    """True if any persistent backend is available (caller uploads instead of local disk)."""
    return is_r2_configured() or is_gridfs_configured()


def preferred_kind():
    """Which backend should new uploads use? R2 > GridFS > local."""
    if is_r2_configured():
        return KIND_R2
    if is_gridfs_configured():
        return KIND_GRIDFS
    return KIND_LOCAL


def upload_bytes(key, data, mime_type=None):
    """Upload bytes via the best available backend.

    Returns the storage kind used ('r2' / 'gridfs') on success, or None
    if no persistent backend is configured / all attempts failed.
    """
    if is_r2_configured() and _r2_upload(key, data, mime_type):
        return KIND_R2
    if is_gridfs_configured() and _gridfs_upload(key, data, mime_type):
        return KIND_GRIDFS
    return None
