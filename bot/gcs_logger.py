"""
Uploads a run's JSONL log to the same GCS bucket set up for the Q3/Q4
tasks and returns a public, wget-able URL for it. Reusing that bucket
means no new infrastructure — it's already in asia-south1 and already
public-readable.

Auth: on a GCE VM, the attached service account is picked up automatically
(no config needed). Anywhere else (Fly.io, Render, your laptop), set
GOOGLE_APPLICATION_CREDENTIALS_JSON to a base64-encoded service account key
— see README for how to create one.
"""
from __future__ import annotations

import base64
import io
import json
import os
import uuid

from google.cloud import storage
from google.oauth2 import service_account

from . import config


def _build_client() -> storage.Client:
    creds_b64 = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if creds_b64:
        info = json.loads(base64.b64decode(creds_b64))
        credentials = service_account.Credentials.from_service_account_info(info)
        return storage.Client(credentials=credentials, project=info.get("project_id"))
    # Falls back to GOOGLE_APPLICATION_CREDENTIALS file, or the GCE metadata
    # server's attached service account, whichever is available.
    return storage.Client()


_client = _build_client()


def upload_log(events: list[dict]) -> str:
    bucket = _client.bucket(config.GCS_BUCKET)
    object_name = f"{config.GCS_LOG_PREFIX}/{uuid.uuid4().hex}.jsonl"
    blob = bucket.blob(object_name)

    buf = io.StringIO()
    for event in events:
        buf.write(json.dumps(event, default=str) + "\n")

    blob.upload_from_string(buf.getvalue(), content_type="application/x-ndjson")

    # Bucket is already public per the Q3/Q4 setup, but make the object
    # explicitly public too in case the bucket uses per-object ACLs.
    try:
        blob.make_public()
    except Exception:
        pass  # uniform bucket-level access already makes this a no-op/error either way

    return f"https://storage.googleapis.com/{config.GCS_BUCKET}/{object_name}"
