"""Saves each run as JSON. Cloud Storage when BUCKET is set, local folder otherwise."""
import datetime
import json
import os
import re
import uuid

BUCKET = os.environ.get("BUCKET")
DATA_DIR = os.environ.get("DATA_DIR", "data")
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
_bucket = None


def _gcs():
    global _bucket
    if _bucket is None:
        from google.cloud import storage
        _bucket = storage.Client().bucket(BUCKET)
    return _bucket


def new_id() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


def save_run(run: dict) -> None:
    if not ID_RE.match(run["id"]):
        raise ValueError("bad run id")
    data = json.dumps(run, indent=1)
    if BUCKET:
        _gcs().blob(f"runs/{run['id']}.json").upload_from_string(data, content_type="application/json")
    else:
        os.makedirs(f"{DATA_DIR}/runs", exist_ok=True)
        with open(f"{DATA_DIR}/runs/{run['id']}.json", "w") as f:
            f.write(data)


def load_run(run_id: str):
    if not ID_RE.match(run_id):
        return None
    try:
        if BUCKET:
            blob = _gcs().blob(f"runs/{run_id}.json")
            return json.loads(blob.download_as_text()) if blob.exists() else None
        with open(f"{DATA_DIR}/runs/{run_id}.json") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def list_runs(limit: int = 15) -> list:
    if BUCKET:
        names = sorted(b.name for b in _gcs().list_blobs(prefix="runs/"))
        ids = [n[5:-5] for n in names if n.endswith(".json")]
    else:
        folder = f"{DATA_DIR}/runs"
        ids = sorted(f[:-5] for f in os.listdir(folder)) if os.path.isdir(folder) else []
    out = []
    for rid in reversed(ids[-limit:]):
        run = load_run(rid)
        if run:
            out.append(run)
    return out


def save_blob(name: str, data: bytes) -> None:
    """Binary files such as map images. name like maps/<run id>/M1.png."""
    if ".." in name or name.startswith("/"):
        raise ValueError("bad name")
    if BUCKET:
        _gcs().blob(name).upload_from_string(data, content_type="image/png")
    else:
        path = f"{DATA_DIR}/{name}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)


def load_blob(name: str):
    if ".." in name or name.startswith("/"):
        return None
    try:
        if BUCKET:
            blob = _gcs().blob(name)
            return blob.download_as_bytes() if blob.exists() else None
        with open(f"{DATA_DIR}/{name}", "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None
