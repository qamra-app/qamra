import os
import io
import json
import threading
import hashlib
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError
from flask import Flask, request, send_file, jsonify
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image, ImageOps

app = Flask(__name__)

# Persistent HTTP session with connection pooling — avoids TCP handshake on every Wassenger call
from requests.adapters import HTTPAdapter as _HTTPAdapter
_session = requests.Session()
_http_adapter = _HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=1)
_session.mount("https://", _http_adapter)
_session.mount("http://", _http_adapter)

# ── In-memory log ring buffer ─────────────────────────────────────────────────
import collections, sys
_LOG_BUF = collections.deque(maxlen=200)

class _LogCapture:
    def __init__(self, orig): self._orig = orig
    def write(self, s):
        if s.strip(): _LOG_BUF.append(s.rstrip())
        self._orig.write(s)
    def flush(self): self._orig.flush()

sys.stdout = _LogCapture(sys.stdout)
sys.stderr = _LogCapture(sys.stderr)

# ── Config ────────────────────────────────────────────────────────────────────
WASSENGER_API_KEY  = os.environ.get("WASSENGER_API_KEY", "")
WASSENGER_API_URL  = "https://api.wassenger.com/v1/messages"

AWS_ACCESS_KEY_ID       = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET_ACCESS_KEY   = os.environ["AWS_SECRET_ACCESS_KEY"]
AWS_REGION              = os.environ.get("AWS_REGION", "us-east-1")
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS"]
APP_URL                 = os.environ.get("APP_URL", "https://qamra-production.up.railway.app")
ADMIN_TOKEN             = os.environ.get("ADMIN_TOKEN", "qamra-admin-2026")
OWNER_PHONE             = os.environ.get("OWNER_PHONE", "97470263297")

MATCH_CONF  = 80
MEDIA_DIR   = "/tmp/qamra_media"
EVENTS_FILE = "/tmp/qamra_events.json"   # ephemeral; backed up to Drive
EVENTS_DRIVE_NAME = "_qamra_events_.json"

os.makedirs(MEDIA_DIR, exist_ok=True)

# ── Message sender ────────────────────────────────────────────────────────────
def send_msg(to, body, media_url=None):
    payload = {"phone": to, "message": body}
    if media_url:
        payload["media"] = {"url": media_url}
    try:
        r = _session.post(WASSENGER_API_URL, json=payload,
                         headers={"Authorization": WASSENGER_API_KEY,
                                  "Content-Type": "application/json"},
                         timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[WASSENGER] Send error to {to}: {e}", flush=True)


def send_buttons(to, body, buttons):
    """Send WhatsApp interactive button message (max 3). Falls back to numbered text."""
    payload = {
        "phone": to,
        "message": body,
        "buttons": [{"text": b} for b in buttons[:3]],
    }
    try:
        r = _session.post(WASSENGER_API_URL, json=payload,
                         headers={"Authorization": WASSENGER_API_KEY,
                                  "Content-Type": "application/json"},
                         timeout=20)
        if r.status_code in (200, 201):
            return
        # If Wassenger rejected buttons, fall through to plain text
        print(f"[BUTTONS] Fell back to text (status {r.status_code})", flush=True)
    except Exception as e:
        print(f"[BUTTONS] Error: {e}", flush=True)
    # Fallback: numbered plain-text list
    numbered = "\n".join(f"*{i+1}* — {b}" for i, b in enumerate(buttons))
    send_msg(to, f"{body}\n\n{numbered}")


# ── Human agent session tracking ──────────────────────────────────────────────
# Maps user_phone → { "owner": owner_phone, "ts": float }
_agent_sessions: dict = {}
_AGENT_TTL = 7200  # 2 hours


def _get_agent_session(user_phone):
    s = _agent_sessions.get(user_phone)
    if s and (time.time() - s["ts"]) < _AGENT_TTL:
        return s
    _agent_sessions.pop(user_phone, None)
    return None


def _start_agent_session(user_phone, owner_phone):
    _agent_sessions[user_phone] = {"owner": owner_phone, "ts": time.time()}


def _end_agent_session(user_phone):
    _agent_sessions.pop(user_phone, None)


def _find_user_for_owner(owner_phone):
    """Return the user_phone currently connected to this owner, or None."""
    now = time.time()
    for user, s in list(_agent_sessions.items()):
        if s["owner"] == owner_phone and (now - s["ts"]) < _AGENT_TTL:
            return user
    return None


rek = boto3.client(
    "rekognition",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
)

# ── Google Drive ──────────────────────────────────────────────────────────────
def _drive(write=False):
    scope = ("https://www.googleapis.com/auth/drive" if write
             else "https://www.googleapis.com/auth/drive.readonly")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS_JSON), scopes=[scope])
    return build("drive", "v3", credentials=creds)

def download_file(file_id):
    svc = _drive()
    req = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl  = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()

# ── Last webhook debug cache ──────────────────────────────────────────────────
_last_webhook = {}

# ── Events registry ───────────────────────────────────────────────────────────
# Structure: { "event_code": { "name", "collection_id", "gdrive_folder_id" }, ... }
_events      = {}
_events_lock = threading.Lock()

def _events_drive_file_id():
    """Find the Drive file ID for _qamra_events_.json, or None."""
    try:
        svc  = _drive()
        resp = svc.files().list(
            q=f"name='{EVENTS_DRIVE_NAME}' and trashed=false",
            fields="files(id)", pageSize=1,
        ).execute()
        files = resp.get("files", [])
        return files[0]["id"] if files else None
    except Exception as e:
        print(f"[EVENTS] Drive lookup error: {e}", flush=True)
        return None

def _save_events_to_drive(data):
    try:
        svc     = _drive(write=True)
        content = json.dumps(data, ensure_ascii=False, indent=2).encode()
        fid     = _events_drive_file_id()
        from googleapiclient.http import MediaInMemoryUpload
        media = MediaInMemoryUpload(content, mimetype="application/json")
        if fid:
            svc.files().update(fileId=fid, media_body=media).execute()
        else:
            svc.files().create(
                body={"name": EVENTS_DRIVE_NAME},
                media_body=media, fields="id",
            ).execute()
        print(f"[EVENTS] Saved {len(data)} events to Drive", flush=True)
    except Exception as e:
        print(f"[EVENTS] Drive save error: {e}", flush=True)

def load_events():
    global _events
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE) as f:
                _events = json.load(f)
            print(f"[EVENTS] Loaded {len(_events)} events from local file", flush=True)
            return
        except Exception:
            pass
    try:
        fid = _events_drive_file_id()
        if fid:
            raw = download_file(fid)
            _events = json.loads(raw)
            with open(EVENTS_FILE, "w") as f:
                json.dump(_events, f)
            print(f"[EVENTS] Loaded {len(_events)} events from Drive", flush=True)
        else:
            print("[EVENTS] No events config found — start fresh.", flush=True)
    except Exception as e:
        print(f"[EVENTS] Load error: {e}", flush=True)

def save_events():
    with open(EVENTS_FILE, "w") as f:
        json.dump(_events, f, ensure_ascii=False)
    threading.Thread(target=_save_events_to_drive, args=(_events.copy(),), daemon=True).start()

def get_event(code):
    return _events.get(code.upper().strip())

def get_todays_events():
    """Return list of (code, event) tuples for weddings happening today."""
    from datetime import date
    today = date.today().isoformat()
    return [(c, e) for c, e in _events.items() if e.get("date", "") == today]

load_events()

# Auto-register default event from env vars if no events exist yet
_DEFAULT_GDRIVE = os.environ.get("GDRIVE_FOLDER_ID", "")
if not _events and _DEFAULT_GDRIVE:
    _events["DEFAULT"] = {
        "name":             os.environ.get("EVENT_NAME", "الحفل"),
        "collection_id":    os.environ.get("COLLECTION_ID", "qamra-wedding"),
        "gdrive_folder_id": _DEFAULT_GDRIVE,
        "drive_url":        f"https://drive.google.com/drive/folders/{_DEFAULT_GDRIVE}",
        "kiosk_url":        "",
        "date":             "",
    }
    print("[EVENTS] Auto-registered DEFAULT event from env vars", flush=True)

# ── Per-event state ───────────────────────────────────────────────────────────
def _state_file(event_code):
    return f"/tmp/qamra_state_{event_code}.json"

def _state_drive_name(event_code):
    return f"_qamra_state_{event_code}_.json"

def _save_state_to_drive(event_code, data):
    global _drive_ok
    if not _drive_ok:
        return
    try:
        svc     = _drive(write=True)
        name    = _state_drive_name(event_code)
        content = json.dumps(data, ensure_ascii=False).encode()
        from googleapiclient.http import MediaInMemoryUpload
        media = MediaInMemoryUpload(content, mimetype="application/json")
        resp  = svc.files().list(
            q=f"name='{name}' and trashed=false",
            fields="files(id)", pageSize=1,
        ).execute()
        files = resp.get("files", [])
        if files:
            svc.files().update(fileId=files[0]["id"], media_body=media).execute()
        else:
            svc.files().create(body={"name": name}, media_body=media, fields="id").execute()
        print(f"[STATE] Saved {event_code} state to Drive ({len(data.get('indexed_ids',[]))} ids)", flush=True)
    except Exception as e:
        print(f"[STATE] Drive save error: {e}", flush=True)
        _drive_ok = False

S3_BUCKET = os.environ.get("S3_BUCKET", "qamra-state-backup")
_s3_ok    = True
_drive_ok = True

def _s3_key(event_code):
    return f"qamra_state_{event_code}.json"

def _s3_client():
    return boto3.client("s3", region_name=AWS_REGION,
                        aws_access_key_id=AWS_ACCESS_KEY_ID,
                        aws_secret_access_key=AWS_SECRET_ACCESS_KEY)

def _ensure_s3_bucket():
    global _s3_ok
    if not S3_BUCKET:
        return
    try:
        s3 = _s3_client()
        if AWS_REGION == "us-east-1":
            s3.create_bucket(Bucket=S3_BUCKET)
        else:
            s3.create_bucket(Bucket=S3_BUCKET,
                             CreateBucketConfiguration={"LocationConstraint": AWS_REGION})
        print(f"[S3] Bucket {S3_BUCKET} created", flush=True)
    except Exception as e:
        if "BucketAlreadyOwnedByYou" in str(e) or "BucketAlreadyExists" in str(e):
            pass
        else:
            print(f"[S3] Bucket ensure error: {e}", flush=True)
            _s3_ok = False

def _save_state_to_s3(event_code, data):
    global _s3_ok
    if not S3_BUCKET or not _s3_ok:
        return
    try:
        s3 = _s3_client()
        s3.put_object(Bucket=S3_BUCKET, Key=_s3_key(event_code),
                      Body=json.dumps(data, ensure_ascii=False).encode(),
                      ContentType="application/json")
        print(f"[STATE] Saved {event_code} to S3 ({len(data.get('indexed_ids',[]))} ids)", flush=True)
    except Exception as e:
        print(f"[STATE] S3 save error: {e}", flush=True)
        _s3_ok = False

def _load_state_from_s3(event_code):
    global _s3_ok
    if not S3_BUCKET or not _s3_ok:
        return None
    try:
        s3  = _s3_client()
        obj = s3.get_object(Bucket=S3_BUCKET, Key=_s3_key(event_code))
        s   = json.loads(obj["Body"].read())
        print(f"[STATE] Loaded {event_code} from S3 ({len(s.get('indexed_ids',[]))} ids)", flush=True)
        return s
    except Exception as e:
        print(f"[STATE] S3 load error: {e}", flush=True)
        _s3_ok = False
        return None

def load_state(event_code):
    path = _state_file(event_code)
    try:
        with open(path) as f:
            s = json.load(f)
            if s.get("indexed_ids") is not None:
                return s
    except Exception:
        pass
    s = _load_state_from_s3(event_code)
    if s is not None:
        with open(path, "w") as f:
            json.dump(s, f)
        return s
    try:
        name = _state_drive_name(event_code)
        svc  = _drive()
        resp = svc.files().list(
            q=f"name='{name}' and trashed=false",
            fields="files(id)", pageSize=1,
        ).execute()
        files = resp.get("files", [])
        if files:
            raw = download_file(files[0]["id"])
            s   = json.loads(raw)
            with open(path, "w") as f:
                json.dump(s, f)
            print(f"[STATE] Loaded {event_code} from Drive ({len(s.get('indexed_ids',[]))} ids)", flush=True)
            return s
    except Exception as e:
        print(f"[STATE] Drive load error: {e}", flush=True)
    try:
        event = get_event(event_code)
        if event and event.get("collection_id"):
            cid = event["collection_id"]
            face_ids = set()
            paginator = rek.get_paginator("list_faces")
            for page in paginator.paginate(CollectionId=cid):
                for face in page.get("Faces", []):
                    eid = face.get("ExternalImageId")
                    if eid:
                        face_ids.add(eid)
            if face_ids:
                s = {"indexed_ids": list(face_ids), "file_map": {}}
                with open(path, "w") as f:
                    json.dump(s, f)
                print(f"[STATE] Rebuilt {event_code} from Rekognition ({len(face_ids)} ids)", flush=True)
                return s
    except Exception as e:
        print(f"[STATE] Rekognition rebuild error: {e}", flush=True)
    return {"indexed_ids": [], "file_map": {}}

def save_state(event_code, state):
    with open(_state_file(event_code), "w") as f:
        json.dump(state, f)
    threading.Thread(target=_save_state_to_s3,   args=(event_code, state.copy()), daemon=True).start()
    threading.Thread(target=_save_state_to_drive, args=(event_code, state.copy()), daemon=True).start()

# ── Conversation state ────────────────────────────────────────────────────────
# { phone: { "state": str, "event_code": str|None, "ts": float } }
_conv     = {}
_CONV_TTL = 3600

def _get_conv(phone):
    e = _conv.get(phone)
    if e and (time.time() - e["ts"]) < _CONV_TTL:
        return e
    return {"state": "new", "event_code": None}

def _set_conv(phone, state, event_code=None):
    prev = _get_conv(phone)
    _conv[phone] = {
        "state":      state,
        "event_code": event_code if event_code is not None else prev.get("event_code"),
        "ts":         time.time(),
    }

def _clear_conv(phone):
    _conv.pop(phone, None)

# ── Kiosk folder cache ────────────────────────────────────────────────────────
_folder_cache = {}  # session_id -> folder_url | None (building) | "" (failed)

# ── Rekognition helpers ───────────────────────────────────────────────────────
def resize_for_rekognition(image_bytes):
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        if img.width > 800 or img.height > 800:
            img.thumbnail((800, 800), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=80)
        return out.getvalue()
    except Exception as e:
        print(f"[RESIZE] {e}", flush=True)
        return image_bytes

def ensure_collection(collection_id):
    try:
        rek.create_collection(CollectionId=collection_id)
        print(f"[COLLECTION] Created: {collection_id}", flush=True)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

def index_face(image_bytes, file_id, collection_id):
    image_bytes = resize_for_rekognition(image_bytes)
    try:
        resp    = rek.index_faces(
            CollectionId=collection_id,
            Image={"Bytes": image_bytes},
            ExternalImageId=file_id,
            DetectionAttributes=[],
            QualityFilter="AUTO",
        )
        return len(resp.get("FaceRecords", []))
    except Exception as e:
        print(f"[INDEX] Error: {e}", flush=True)
        return 0

def search_by_selfie(selfie_bytes, collection_id):
    selfie_bytes = resize_for_rekognition(selfie_bytes)
    try:
        resp    = rek.search_faces_by_image(
            CollectionId=collection_id,
            Image={"Bytes": selfie_bytes},
            MaxFaces=4096,
            FaceMatchThreshold=MATCH_CONF,
        )
        matches = resp.get("FaceMatches", [])
        print(f"[SEARCH] {len(matches)} matches in {collection_id}", flush=True)
        return matches
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "InvalidParameterException":
            print("[SEARCH] No face detected", flush=True)
        else:
            print(f"[SEARCH] Error: {e}", flush=True)
        return []
    except Exception as e:
        print(f"[SEARCH] Error: {e}", flush=True)
        return []

def save_jpeg(image_bytes, output_path):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        if img.width > 1920:
            ratio = 1920 / img.width
            img = img.resize((1920, int(img.height * ratio)), Image.LANCZOS)
        img.save(output_path, format="JPEG", quality=85)
        return True
    except Exception as e:
        print(f"[JPEG] Error: {e}", flush=True)
        return False

def list_drive_photos(gdrive_folder_id):
    svc = _drive()
    results, pt = [], None
    while True:
        resp = svc.files().list(
            q=f"'{gdrive_folder_id}' in parents and mimeType contains 'image/' and trashed=false",
            fields="nextPageToken, files(id, name, webViewLink)",
            pageSize=200, pageToken=pt,
        ).execute()
        results.extend(resp.get("files", []))
        pt = resp.get("nextPageToken")
        if not pt:
            break
    return results

# ── Indexing ──────────────────────────────────────────────────────────────────
_index_locks = {}

def run_index(event_code):
    event = get_event(event_code)
    if not event:
        print(f"[INDEX] Unknown event: {event_code}", flush=True)
        return 0

    collection_id    = event["collection_id"]
    gdrive_folder_id = event["gdrive_folder_id"]

    lock = _index_locks.setdefault(event_code, threading.Lock())
    if not lock.acquire(blocking=False):
        print(f"[INDEX] Already running for {event_code}", flush=True)
        return 0
    try:
        ensure_collection(collection_id)
        state       = load_state(event_code)
        indexed_ids = set(state.get("indexed_ids", []))
        file_map    = state.get("file_map", {})

        photos = list_drive_photos(gdrive_folder_id)
        print(f"[INDEX] {event_code}: {len(photos)} photos, {len(indexed_ids)} indexed", flush=True)

        new_count = 0
        for i, photo in enumerate(photos):
            if photo["id"] in indexed_ids:
                continue
            try:
                img_bytes = download_file(photo["id"])
                n = index_face(img_bytes, photo["id"], collection_id)
                if n > 0:
                    indexed_ids.add(photo["id"])
                    file_map[photo["id"]] = {"name": photo["name"], "link": photo["webViewLink"]}
                    new_count += n
                else:
                    print(f"[INDEX] No face: {photo['name']}", flush=True)
            except Exception as e:
                print(f"[INDEX] Error {photo['name']}: {e}", flush=True)

            if (i + 1) % 20 == 0:
                state["indexed_ids"] = list(indexed_ids)
                state["file_map"]    = file_map
                save_state(event_code, state)
                print(f"[INDEX] {event_code}: {i+1}/{len(photos)}, {new_count} new", flush=True)

        state["indexed_ids"] = list(indexed_ids)
        state["file_map"]    = file_map
        save_state(event_code, state)
        print(f"[INDEX] {event_code}: done. {len(indexed_ids)} total, {new_count} new", flush=True)
        return len(indexed_ids)
    finally:
        lock.release()

# ── Auto-index all events ─────────────────────────────────────────────────────
def _auto_index_loop():
    time.sleep(5)
    while True:
        with _events_lock:
            codes = list(_events.keys())
        for code in codes:
            try:
                run_index(code)
            except Exception as e:
                print(f"[AUTO-INDEX] {code} error: {e}", flush=True)
        time.sleep(30)

threading.Thread(target=_auto_index_loop, daemon=True).start()

# ── Keep-alive: ping self every 4 min to prevent Railway cold starts ──────────
def _keepalive_loop():
    time.sleep(60)
    while True:
        try:
            requests.get(f"{APP_URL}/", timeout=10)
        except Exception:
            pass
        time.sleep(240)

threading.Thread(target=_keepalive_loop, daemon=True).start()

# ── Guest folder creation ─────────────────────────────────────────────────────
def create_guest_folder(sender_label, file_ids, event_name):
    svc = _drive(write=True)
    folder = svc.files().create(body={
        "name": f"صورك من {event_name} 🌙 — {sender_label}",
        "mimeType": "application/vnd.google-apps.folder",
    }, fields="id").execute()
    folder_id = folder["id"]
    svc.permissions().create(
        fileId=folder_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    errors = []
    def _batch_cb(request_id, response, exception):
        if exception:
            errors.append(str(exception))

    for chunk_start in range(0, len(file_ids), 100):
        batch = svc.new_batch_http_request(callback=_batch_cb)
        for fid in file_ids[chunk_start:chunk_start + 100]:
            batch.add(svc.files().create(
                body={
                    "name": fid,
                    "mimeType": "application/vnd.google-apps.shortcut",
                    "shortcutDetails": {"targetId": fid},
                    "parents": [folder_id],
                },
                fields="id"
            ))
        batch.execute()

    if errors:
        print(f"[FOLDER] {len(errors)} shortcut errors", flush=True)

    link = f"https://drive.google.com/drive/folders/{folder_id}"
    print(f"[FOLDER] Created: {link} ({len(file_ids)} shortcuts)", flush=True)
    return link

# ── WhatsApp search + send ────────────────────────────────────────────────────
def search_and_send(selfie_bytes, sender, event_code):
    print(f"[SEARCH] start sender={sender} event={event_code} selfie_size={len(selfie_bytes)}", flush=True)
    event    = get_event(event_code)
    if not event:
        send_msg(sender, "⚠️ الحفل غير موجود. تأكد من الكود وحاول مرة ثانية.")
        return

    state    = load_state(event_code)
    file_map = state.get("file_map", {})
    print(f"[SEARCH] indexed={len(state.get('indexed_ids',[]))} collection={event.get('collection_id')}", flush=True)

    if not state.get("indexed_ids"):
        print(f"[SEARCH] local state empty, attempting Rekognition search anyway", flush=True)

    matches = search_by_selfie(selfie_bytes, event["collection_id"])
    print(f"[SEARCH] rekognition matches={len(matches) if matches else 0}", flush=True)
    if not matches:
        send_msg(sender, "😕 ما لقيت وجه في الصورة أو ما لقيت صورك — أرسل سيلفي واضح وحاول مرة ثانية")
        return

    seen_ids, file_ids = set(), []
    for m in matches:
        file_id = m["Face"]["ExternalImageId"]
        if file_id not in seen_ids:
            seen_ids.add(file_id)
            file_ids.append(file_id)

    count = len(file_ids)

    try:
        phone_label = sender.replace("whatsapp:", "").replace("+", "")
        folder_link = create_guest_folder(phone_label, file_ids, event["name"])
        send_msg(sender,
            f"✅ وجدت *{count}* صورة لك من *{event['name']}* 🎉\n\n"
            f"📂 جميع صورك في مجلدك الخاص:\n{folder_link}\n\n"
            "شكراً لاستخدامك قمرة 🌙 نتمنى أن الصور عجبتك ✨"
        )
    except Exception as e:
        print(f"[REPLY] ERROR folder: {e}", flush=True)
        send_msg(sender, f"✅ وجدت *{count}* صورة لك من *{event['name']}* 🎉 — تواصل مع المصور لاستلامها.")

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    summary = {code: len(load_state(code).get("indexed_ids", [])) for code in _events}
    lines   = [f"قمرة 🌙 — {len(_events)} events"] + [f"  {k}: {v} photos" for k, v in summary.items()]
    return "\n".join(lines), 200

@app.route("/media/<filename>", methods=["GET"])
def serve_media(filename):
    filepath = os.path.join(MEDIA_DIR, filename)
    if not os.path.exists(filepath):
        return "Not found", 404
    mime = "image/jpeg" if filename.endswith((".jpg", ".jpeg")) else "image/png"
    return send_file(filepath, mimetype=mime)

@app.route("/photo/<file_id>", methods=["GET"])
def serve_photo(file_id):
    if not all(c.isalnum() or c in "-_" for c in file_id):
        return "invalid id", 400
    cached = os.path.join(MEDIA_DIR, f"cache_{file_id}.jpg")
    if os.path.exists(cached):
        return send_file(cached, mimetype="image/jpeg")
    try:
        raw = download_file(file_id)
        if save_jpeg(raw, cached):
            return send_file(cached, mimetype="image/jpeg")
        return "download failed", 500
    except Exception as e:
        print(f"[PHOTO] Error {file_id}: {e}", flush=True)
        return str(e), 500

@app.route("/folder-status/<session_id>", methods=["GET"])
def folder_status(session_id):
    if session_id not in _folder_cache:
        return jsonify({"status": "not_found"}), 404
    url = _folder_cache[session_id]
    if url is None:
        return jsonify({"status": "building"}), 202
    return jsonify({"status": "ready", "folder_url": url}), 200

@app.route("/match", methods=["POST"])
def match_api():
    """Kiosk face search — returns results instantly, folder built in background."""
    event_code = request.form.get("event_code", "").upper().strip()
    phone      = request.form.get("phone", "").strip()
    event      = get_event(event_code)

    if not event:
        todays = get_todays_events()
        if len(todays) == 1:
            event_code, event = todays[0]
        elif len(_events) == 1:
            event_code = next(iter(_events))
            event = _events[event_code]
        else:
            return jsonify({"error": f"No active event found. Register events via /admin"}), 404

    selfie_bytes = None
    if "photo" in request.files:
        selfie_bytes = request.files["photo"].read()
    elif request.is_json and request.json.get("image_url"):
        try:
            r = requests.get(request.json["image_url"], timeout=15)
            if r.status_code == 200:
                selfie_bytes = r.content
        except Exception as e:
            return jsonify({"error": f"Could not fetch image: {e}"}), 400

    if not selfie_bytes:
        return jsonify({"error": "Send photo via 'photo' field"}), 400

    state    = load_state(event_code)
    file_map = state.get("file_map", {})
    if not state.get("indexed_ids"):
        return jsonify({"error": "Photos not indexed yet — call /index first"}), 503

    matches = search_by_selfie(selfie_bytes, event["collection_id"])
    if not matches:
        return jsonify({"matches": [], "message": "No face found or no matches"}), 200

    seen, results, file_ids = set(), [], []
    for m in matches:
        file_id = m["Face"]["ExternalImageId"]
        conf    = m["Similarity"]
        if file_id in seen:
            continue
        seen.add(file_id)
        file_ids.append(file_id)
        entry = file_map.get(file_id, {})
        results.append({
            "url":        f"{APP_URL}/photo/{file_id}",
            "confidence": round(conf, 1),
            "name":       entry.get("name", ""),
            "drive_link": entry.get("link", ""),
        })

    session_id = hashlib.md5(f"{time.time()}{phone}".encode()).hexdigest()[:16]
    _folder_cache[session_id] = None

    def _build_folder(sid, fid_list, ph, evt):
        try:
            label = ph.replace("+", "") if ph else sid[:8]
            url   = create_guest_folder(label, fid_list, evt["name"])
            _folder_cache[sid] = url
        except Exception as e:
            _folder_cache[sid] = ""
            print(f"[FOLDER] Error: {e}", flush=True)

    threading.Thread(target=_build_folder, args=(session_id, file_ids, phone, event), daemon=True).start()

    return jsonify({"matches": results, "session_id": session_id}), 200

@app.route("/index", methods=["POST"])
def index_photos():
    event_code = request.args.get("event", "").upper().strip()
    if not event_code or not get_event(event_code):
        return jsonify({"error": f"Unknown event '{event_code}'. Register first via /admin/event"}), 404
    threading.Thread(target=run_index, args=(event_code,), daemon=True).start()
    return jsonify({"status": "indexing started", "event": event_code}), 202

# ── Admin endpoints ───────────────────────────────────────────────────────────
def _check_admin():
    token = request.headers.get("X-Admin-Token") or request.args.get("token")
    return token == ADMIN_TOKEN

@app.route("/admin", methods=["GET"])
def admin_ui():
    token = request.args.get("token", "")
    if token != ADMIN_TOKEN:
        return """<!DOCTYPE html><html lang="ar" dir="rtl">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>قمرة — لوحة التحكم</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{background:#0E0B08;color:#FAF6EC;font-family:-apple-system,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}
.box{background:rgba(250,246,236,.06);border:1px solid rgba(250,246,236,.1);border-radius:20px;padding:40px;max-width:360px;width:100%;text-align:center}
h2{margin-bottom:24px;font-size:20px}input{width:100%;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);border-radius:10px;padding:12px 16px;color:#FAF6EC;font-size:15px;margin-bottom:16px;outline:none}
button{width:100%;background:#C9A96E;border:none;border-radius:10px;padding:14px;color:#0E0B08;font-size:15px;font-weight:700;cursor:pointer}
</style></head><body><div class="box"><h2>🌙 قمرة — تسجيل الدخول</h2>
<form method="get"><input type="password" name="token" placeholder="كلمة المرور"><button type="submit">دخول</button></form></div></body></html>""", 401

    events_rows = ""
    for code, ev in _events.items():
        state   = load_state(code)
        count   = len(state.get("indexed_ids", []))
        landing = f"{APP_URL}/event/{code}"
        events_rows += f"""
        <tr>
          <td><strong>{code}</strong></td>
          <td>{ev['name']}</td>
          <td>{count} صورة</td>
          <td><a href="{landing}" target="_blank" style="color:#C9A96E">صفحة الحفل ↗</a></td>
          <td>
            <a href="/index?event={code}" onclick="fetch('/index?event={code}',{{method:'POST'}});this.textContent='⏳';return false"
               style="color:#aaa;font-size:13px">إعادة فهرسة</a>
            &nbsp;
            <a href="/admin/event/{code}?token={token}" onclick="if(!confirm('حذف {code}؟'))return false;fetch('/admin/event/{code}',{{method:'DELETE',headers:{{'X-Admin-Token':'{token}'}}}}).then(()=>location.reload())"
               style="color:#e55;font-size:13px">حذف</a>
          </td>
        </tr>"""

    if not events_rows:
        events_rows = "<tr><td colspan='5' style='text-align:center;color:#666;padding:24px'>لا يوجد أحداث — أضف أول حفل أدناه</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>قمرة — لوحة التحكم</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0E0B08;color:#FAF6EC;font-family:-apple-system,'Segoe UI',sans-serif;padding:24px;min-height:100vh}}
h1{{font-size:24px;margin-bottom:4px}}
.sub{{color:#888;font-size:13px;margin-bottom:32px}}
.card{{background:rgba(250,246,236,.05);border:1px solid rgba(250,246,236,.1);border-radius:16px;padding:24px;margin-bottom:24px}}
h2{{font-size:16px;margin-bottom:16px;color:#C9A96E}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th{{text-align:right;padding:10px 12px;border-bottom:1px solid rgba(255,255,255,.1);color:#888;font-weight:500}}
td{{padding:12px;border-bottom:1px solid rgba(255,255,255,.06)}}
.form-row{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}
.form-row.full{{grid-template-columns:1fr}}
label{{display:block;font-size:12px;color:#888;margin-bottom:4px}}
input{{width:100%;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:10px 14px;color:#FAF6EC;font-size:14px;outline:none}}
input:focus{{border-color:#C9A96E}}
.btn{{background:#C9A96E;border:none;border-radius:10px;padding:12px 24px;color:#0E0B08;font-size:14px;font-weight:700;cursor:pointer;margin-top:8px}}
.success{{color:#4caf50;font-size:13px;margin-top:8px;display:none}}
</style>
</head>
<body>
<h1>🌙 قمرة</h1>
<p class="sub">لوحة إدارة الأحداث</p>

<div class="card">
  <h2>الأحداث المسجلة</h2>
  <table>
    <thead><tr><th>الكود</th><th>الاسم</th><th>الصور</th><th>الصفحة</th><th>إجراءات</th></tr></thead>
    <tbody>{events_rows}</tbody>
  </table>
</div>

<div class="card">
  <h2>إضافة حفل جديد</h2>
  <form id="addForm">
    <div class="form-row">
      <div><label>كود الحفل (بالإنجليزي)</label><input name="code" placeholder="مثال: AHMED2026" required></div>
      <div><label>اسم الحفل</label><input name="name" placeholder="حفل أحمد ومريم" required></div>
    </div>
    <div class="form-row">
      <div><label>Rekognition Collection ID</label><input name="collection_id" placeholder="qamra-ahmed2026" required></div>
      <div>
        <label>مجلد Google Drive <span id="folderLoading" style="color:#C9A96E;font-size:11px">⏳ جاري التحميل...</span></label>
        <select name="gdrive_folder_id" id="folderSelect" required style="width:100%;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:10px 14px;color:#FAF6EC;font-size:14px;outline:none">
          <option value="">-- اختر المجلد --</option>
        </select>
      </div>
    </div>
    <div class="form-row">
      <div><label>تاريخ الحفل</label><input name="date" type="date" required></div>
      <div><label>رابط ألبوم Drive العام (اختياري)</label><input name="drive_url" id="driveUrl" placeholder="سيُملأ تلقائياً عند اختيار المجلد"></div>
    </div>
    <div class="form-row full">
      <div><label>رابط الكشك (اختياري)</label><input name="kiosk_url" placeholder="http://192.168.1.10:5000?event=AHMED2026"></div>
    </div>
    <button type="submit" class="btn">إضافة الحفل وبدء الفهرسة ＋</button>
    <div class="success" id="successMsg">✅ تمت الإضافة! جاري الفهرسة في الخلفية.</div>
  </form>
</div>

<script>
(async () => {{
  try {{
    const r = await fetch('/admin/drive-folders?token={token}');
    const folders = await r.json();
    const sel = document.getElementById('folderSelect');
    document.getElementById('folderLoading').textContent = '';
    folders.forEach(f => {{
      const opt = document.createElement('option');
      opt.value = f.id;
      opt.textContent = f.name;
      sel.appendChild(opt);
    }});
    sel.addEventListener('change', () => {{
      const fid = sel.value;
      if (fid) document.getElementById('driveUrl').value = 'https://drive.google.com/drive/folders/' + fid;
    }});
  }} catch(e) {{
    document.getElementById('folderLoading').textContent = '(تعذر التحميل)';
  }}
}})();

document.getElementById('addForm').addEventListener('submit', async e => {{
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = Object.fromEntries(fd.entries());
  body.code = body.code.toUpperCase().trim();
  const r = await fetch('/admin/event', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json','X-Admin-Token':'{token}'}},
    body: JSON.stringify(body)
  }});
  if (r.ok) {{
    document.getElementById('successMsg').style.display = 'block';
    setTimeout(() => location.reload(), 2000);
  }} else {{
    const err = await r.json();
    alert('خطأ: ' + (err.error || 'unknown'));
  }}
}});
</script>
</body></html>""", 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/admin/last-webhook", methods=["GET"])
def admin_last_webhook():
    if not _check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(_last_webhook), 200

@app.route("/admin/test-media", methods=["GET"])
def admin_test_media():
    if not _check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    msg_data  = _last_webhook.get("data", {})
    msg_id    = request.args.get("msg_id") or msg_data.get("id") or ""
    device_id = request.args.get("device_id") or _last_webhook.get("device", {}).get("id") or ""
    msg_link  = request.args.get("msg_link") or (msg_data.get("links") or {}).get("message") or ""
    media_obj = msg_data.get("media") or {}
    media_status = media_obj.get("status", "")
    results = {"msg_id": msg_id, "device_id": device_id, "msg_link": msg_link, "media_status": media_status}
    if not WASSENGER_API_KEY:
        return jsonify({"error": "No API key set", **results}), 400
    if not msg_id:
        return jsonify({"error": "No message cached — send a selfie first then retry", **results}), 400
    hdrs = {"Authorization": WASSENGER_API_KEY}
    BASE = "https://api.wassenger.com"
    if msg_link:
        try:
            r = requests.get(f"{BASE}{msg_link}", headers=hdrs, timeout=15)
            try:
                body = r.json()
                results["msg_lookup"] = {"status": r.status_code, "body": body}
                mo = body.get("media") or {}
                api_dl = (mo.get("url") or (mo.get("links") or {}).get("download") or
                          mo.get("link") or mo.get("downloadUrl") or "")
                if api_dl:
                    results["api_media_url"] = api_dl
                    abs_dl = (BASE + api_dl) if api_dl.startswith("/") else api_dl
                    try:
                        r2 = requests.get(abs_dl, headers=hdrs, timeout=20)
                        ct2 = r2.headers.get("Content-Type", "")
                        results["api_media_download"] = {"url": abs_dl, "status": r2.status_code, "content_type": ct2, "size": len(r2.content)}
                        if r2.status_code == 200 and len(r2.content) > 1000:
                            results["SUCCESS"] = abs_dl
                    except Exception as e2:
                        results["api_media_download"] = {"url": abs_dl, "error": str(e2)}
            except Exception:
                results["msg_lookup"] = {"status": r.status_code, "body_raw": r.text[:500]}
        except Exception as e:
            results["msg_lookup"] = {"error": str(e)}
    paths_to_try = []
    if msg_link:
        paths_to_try.append(f"{msg_link}/download")
        paths_to_try.append(f"{msg_link}/media")
    if device_id and msg_id:
        paths_to_try += [
            f"/v1/devices/{device_id}/messages/{msg_id}/download",
            f"/v1/devices/{device_id}/messages/{msg_id}/media",
        ]
    paths_to_try += [
        f"/v1/messages/{msg_id}/download",
        f"/v1/messages/{msg_id}/media",
    ]
    for p in paths_to_try:
        try:
            r = requests.get(f"{BASE}{p}", headers=hdrs, timeout=15)
            ct = r.headers.get("Content-Type", "")
            results[p] = {"status": r.status_code, "content_type": ct, "size": len(r.content),
                          "body_snippet": r.text[:200] if r.status_code != 200 else ""}
            if r.status_code == 200 and len(r.content) > 1000:
                results["SUCCESS"] = p
                break
        except Exception as e:
            results[p] = {"error": str(e)}
    return jsonify(results), 200

@app.route("/admin/drive-folders", methods=["GET"])
def admin_drive_folders():
    if not _check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        svc  = _drive()
        resp = svc.files().list(
            q="mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id, name)",
            orderBy="name",
            pageSize=100,
        ).execute()
        folders = [{"id": f["id"], "name": f["name"]} for f in resp.get("files", [])]
        return jsonify(folders), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin/events", methods=["GET"])
def admin_list_events():
    if not _check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    result = {}
    for code, ev in _events.items():
        state = load_state(code)
        result[code] = {**ev, "indexed_photos": len(state.get("indexed_ids", []))}
    return jsonify(result), 200

@app.route("/admin/event", methods=["POST"])
def admin_add_event():
    if not _check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    code = data.get("code", "").upper().strip()
    if not code:
        return jsonify({"error": "code is required"}), 400
    if not data.get("name") or not data.get("collection_id") or not data.get("gdrive_folder_id"):
        return jsonify({"error": "name, collection_id, gdrive_folder_id are required"}), 400

    with _events_lock:
        _events[code] = {
            "name":             data["name"],
            "collection_id":    data["collection_id"],
            "gdrive_folder_id": data["gdrive_folder_id"],
            "drive_url":        data.get("drive_url", ""),
            "kiosk_url":        data.get("kiosk_url", ""),
            "date":             data.get("date", ""),
        }
        save_events()

    threading.Thread(target=run_index, args=(code,), daemon=True).start()
    return jsonify({"status": "created", "event": code, "indexing": "started", "landing": f"{APP_URL}/event/{code}"}), 201

@app.route("/admin/event/<code>", methods=["DELETE"])
def admin_delete_event(code):
    if not _check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    code = code.upper()
    with _events_lock:
        if code not in _events:
            return jsonify({"error": "Not found"}), 404
        del _events[code]
        save_events()
    return jsonify({"status": "deleted", "event": code}), 200

@app.route("/admin/logs", methods=["GET"])
def admin_logs():
    token = request.args.get("token", "")
    if token != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"logs": list(_LOG_BUF)}), 200

@app.route("/admin/wassenger", methods=["GET"])
def admin_wassenger():
    token = request.args.get("token", "")
    if token != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    headers = {"Authorization": WASSENGER_API_KEY, "Content-Type": "application/json"}
    try:
        devices_r = requests.get("https://api.wassenger.com/v1/devices", headers=headers, timeout=15)
        devices = devices_r.json()
        if not devices or not isinstance(devices, list):
            return jsonify({"error": "no devices", "raw": devices}), 200
        device = devices[0]
        device_id = device.get("id") or device.get("_id")

        webhook_paths = [
            f"https://api.wassenger.com/v1/devices/{device_id}/webhooks",
            f"https://api.wassenger.com/v1/webhooks",
            f"https://api.wassenger.com/v1/account/webhooks",
        ]
        webhooks_results = {}
        for path in webhook_paths:
            r = requests.get(path, headers=headers, timeout=10)
            webhooks_results[path] = {"status": r.status_code, "body": r.json()}

        return jsonify({
            "device_id": device_id,
            "alias": device.get("alias"),
            "healthy": device.get("healthy"),
            "status": device.get("status"),
            "sendMessageStatus": device.get("sendMessageStatus"),
            "connector": device.get("connector"),
            "webhook_probe": webhooks_results,
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin/wassenger/reset-webhook", methods=["POST"])
def admin_reset_webhook():
    token = request.args.get("token", "")
    if token != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    headers = {"Authorization": WASSENGER_API_KEY, "Content-Type": "application/json"}
    webhook_url = f"{APP_URL}/whatsapp"
    results = {}
    try:
        devices = requests.get("https://api.wassenger.com/v1/devices", headers=headers, timeout=15).json()
        device_id = (devices[0].get("id") or devices[0].get("_id")) if devices else None
        if not device_id:
            return jsonify({"error": "no device"}), 500

        for path in [
            f"https://api.wassenger.com/v1/devices/{device_id}/webhooks",
            "https://api.wassenger.com/v1/webhooks",
        ]:
            list_r = requests.get(path, headers=headers, timeout=10)
            if list_r.status_code == 200:
                hooks = list_r.json() if isinstance(list_r.json(), list) else []
                for h in hooks:
                    hid = h.get("id") or h.get("_id")
                    if hid:
                        del_r = requests.delete(f"{path}/{hid}", headers=headers, timeout=10)
                        results[f"deleted_{hid}"] = del_r.status_code

                create_r = requests.post(path, json={
                    "name": "qamra-webhook",
                    "url": webhook_url,
                    "events": ["message:in:new"],
                }, headers=headers, timeout=10)
                results["created"] = {"status": create_r.status_code, "body": create_r.json()}
                return jsonify(results), 200

        return jsonify({"error": "no working webhook endpoint found", "tried": results}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── WhatsApp webhook ──────────────────────────────────────────────────────────
@app.route("/event/<code>", methods=["GET"])
def event_landing(code):
    event = get_event(code.upper())
    if not event:
        return "حفل غير موجود", 404

    wa_number  = OWNER_PHONE
    wa_link    = f"https://wa.me/{wa_number}?text={code.upper()}"
    drive_url  = event.get("drive_url", "")
    kiosk_url  = event.get("kiosk_url", "")
    name       = event["name"]

    cards = ""
    if drive_url:
        cards += f"""
        <a href="{drive_url}" target="_blank" class="card">
          <div class="icon">🖼️</div>
          <div class="label">شاهد جميع صور الحفل</div>
          <div class="sub">Google Drive — ألبوم الحفل كاملاً</div>
        </a>"""

    cards += f"""
        <a href="{wa_link}" target="_blank" class="card highlight">
          <div class="icon">📸</div>
          <div class="label">ابحث عن صورك</div>
          <div class="sub">أرسل سيلفي عبر واتساب وسنجد صورك خلال ثوانٍ</div>
        </a>"""

    if kiosk_url:
        cards += f"""
        <a href="{kiosk_url}" target="_blank" class="card">
          <div class="icon">🖥️</div>
          <div class="label">استخدم الكشك</div>
          <div class="sub">ابحث عن صورك عبر الكشك الذكي في الحفل</div>
        </a>"""

    html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>قمرة — {name}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #0E0B08;
    color: #FAF6EC;
    font-family: -apple-system, 'Segoe UI', sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 24px 16px;
  }}
  .logo {{ font-size: 36px; margin-bottom: 8px; }}
  .brand {{ font-size: 13px; color: #C9A96E; letter-spacing: 3px; text-transform: uppercase; margin-bottom: 32px; }}
  h1 {{ font-size: 22px; font-weight: 600; text-align: center; margin-bottom: 8px; line-height: 1.4; }}
  .sub {{ font-size: 13px; color: #999; margin-bottom: 36px; text-align: center; }}
  .cards {{ width: 100%; max-width: 400px; display: flex; flex-direction: column; gap: 14px; }}
  .card {{
    display: flex; align-items: center; gap: 16px;
    background: rgba(250,246,236,0.06);
    border: 1px solid rgba(250,246,236,0.1);
    border-radius: 16px;
    padding: 20px;
    text-decoration: none;
    color: #FAF6EC;
    transition: background 0.2s;
  }}
  .card:active {{ background: rgba(250,246,236,0.12); }}
  .card.highlight {{
    background: rgba(201,169,110,0.15);
    border-color: rgba(201,169,110,0.4);
  }}
  .icon {{ font-size: 32px; flex-shrink: 0; }}
  .label {{ font-size: 16px; font-weight: 600; margin-bottom: 3px; }}
  .sub {{ font-size: 12px; color: #aaa; margin: 0; text-align: right; }}
  .footer {{ margin-top: 40px; font-size: 11px; color: #555; }}
</style>
</head>
<body>
  <div class="logo">🌙</div>
  <div class="brand">QAMRA</div>
  <h1>{name}</h1>
  <p class="sub">اختر كيف تريد الوصول لصورك</p>
  <div class="cards">{cards}</div>
  <div class="footer">Powered by QAMRA 🌙</div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/whatsapp", methods=["POST"], strict_slashes=False)
def whatsapp_webhook():
    try:
        return _handle_whatsapp()
    except Exception as exc:
        print(f"[WH] unhandled error: {exc}", flush=True)
        return "", 200

def _handle_whatsapp():
    data      = request.get_json(silent=True) or {}
    global _last_webhook
    _last_webhook = data
    if data.get("event") != "message:in:new":
        return "", 200
    msg_data  = data.get("data", {})
    sender    = (msg_data.get("fromNumber") or msg_data.get("phone") or
                 msg_data.get("from") or msg_data.get("sender") or "").replace("+", "")
    body_text = (msg_data.get("body") or "").strip()
    msg_type  = (msg_data.get("type") or msg_data.get("messageType") or "").lower()
    has_media = (msg_type in ("image", "video", "audio", "document", "sticker", "photo")
                 or msg_data.get("hasMedia") or msg_data.get("isMedia")
                 or bool(msg_data.get("media")) or bool(msg_data.get("attachment")))
    print(f"[WH_FULL] {json.dumps({k:v for k,v in msg_data.items() if k not in ('thumbnail',) or v})[:1500]}", flush=True)
    msg_id    = msg_data.get("id") or msg_data.get("_id") or msg_data.get("messageId") or ""
    waba_id   = msg_data.get("wabaId") or msg_data.get("wamid") or ""
    device_id = data.get("device", {}).get("id") or ""
    msg_link  = (msg_data.get("links") or {}).get("message") or ""
    media_obj = msg_data.get("media") or msg_data.get("attachment") or {}
    media_url = (media_obj.get("url") or media_obj.get("link") or
                 media_obj.get("mediaUrl") or media_obj.get("downloadUrl") or
                 ((media_obj.get("links") or {}).get("download") or ""))
    media_wid = (media_obj.get("id") or media_obj.get("_id") or
                 media_obj.get("mediaId") or "")
    print(f"[MEDIA] msg_id={msg_id!r} device_id={device_id!r} msg_link={msg_link!r} media_wid={media_wid!r} media_url={media_url!r} has_media={has_media}", flush=True)
    print(f"[WH] sender={sender} type={msg_type} has_media={has_media} media_url={media_url!r}", flush=True)

    def _reply(text, murl=None):
        threading.Thread(target=send_msg, args=(sender, text), kwargs={"media_url": murl}, daemon=True).start()
        return "", 200

    if not sender:
        return "", 200

    # ── Owner acting as human agent ───────────────────────────────────────────
    clean_owner = OWNER_PHONE.lstrip("+")
    if sender == clean_owner:
        user_phone = _find_user_for_owner(sender)
        if user_phone:
            if body_text.strip() == "#end":
                _end_agent_session(user_phone)
                _clear_conv(user_phone)
                send_msg(user_phone,
                    "✅ انتهت المحادثة مع فريق الدعم.\n\n"
                    "إذا احتجت مساعدة مرة أخرى أرسل *مرحبا* 🌙"
                )
                return _reply("✅ تم إنهاء الجلسة.")
            else:
                send_msg(user_phone, f"👤 *فريق قمرة:*\n{body_text}")
                _agent_sessions[user_phone]["ts"] = time.time()
                return "", 200
        return "", 200

    conv  = _get_conv(sender)
    state = conv["state"]

    # ── Selfie received — ACK instantly, download + search in background ─────
    if has_media:
        event_code = conv.get("event_code")
        if not event_code or not get_event(event_code):
            todays = get_todays_events()
            if len(todays) == 1:
                event_code = todays[0][0]
                _set_conv(sender, "awaiting_selfie", event_code=event_code)
            elif len(_events) == 1:
                event_code = next(iter(_events))
                _set_conv(sender, "awaiting_selfie", event_code=event_code)
            elif len(todays) > 1:
                options = "\n".join(f"*{i+1}* — {e['name']}" for i, (_, e) in enumerate(todays))
                _set_conv(sender, "choosing_event")
                _conv[sender]["today_events"] = [c for c, _ in todays]
                return _reply(f"🌙 فيه أكثر من حفل اليوم، اختر الحفل الذي أنت فيه:\n\n{options}")
            else:
                return _reply("⚠️ ما فيه حفل مسجل اليوم. تواصل مع المنظم.")

        event_name = get_event(event_code)["name"]
        _clear_conv(sender)
        _sender, _event_code = sender, event_code

        # Reply immediately — user sees this before we even start downloading
        send_msg(_sender, f"🔍 جاري البحث في {event_name}... سأرسل لك النتيجة خلال ثوانٍ ⏳")

        _cap_msg_link  = msg_link
        _cap_msg_id    = msg_id
        _cap_device_id = device_id
        _cap_media_url = media_url
        _cap_media_wid = media_wid

        def run():
            try:
                selfie_bytes = None

                if WASSENGER_API_KEY:
                    hdrs = {"Authorization": WASSENGER_API_KEY}
                    BASE = "https://api.wassenger.com"

                    def _abs(u):
                        return (BASE + u) if u and u.startswith("/") else u

                    _mu = _cap_media_url
                    _mw = _cap_media_wid

                    if not _mu:
                        lks = [l for l in [
                            _cap_msg_link,
                            f"/v1/devices/{_cap_device_id}/messages/{_cap_msg_id}" if _cap_device_id and _cap_msg_id else "",
                            f"/v1/messages/{_cap_msg_id}",
                        ] if l and not l.endswith("/")]

                        def _lk(lk):
                            try:
                                r = _session.get(f"{BASE}{lk}", headers=hdrs, timeout=10)
                                if r.status_code == 200:
                                    mo = r.json().get("media") or {}
                                    dl = ((mo.get("links") or {}).get("download") or
                                          mo.get("url") or mo.get("link") or mo.get("downloadUrl") or "")
                                    return _abs(dl) if dl else None, mo.get("id") or ""
                            except Exception as e:
                                print(f"[MEDIA] GET {lk} error: {e}", flush=True)
                            return None, ""

                        if lks:
                            with ThreadPoolExecutor(max_workers=len(lks)) as ex:
                                for fut in as_completed([ex.submit(_lk, l) for l in lks]):
                                    u, w = fut.result()
                                    if u and not _mu:
                                        _mu = u
                                        print(f"[MEDIA] found URL: {_mu}", flush=True)
                                    if w and not _mw:
                                        _mw = w

                    if _mu:
                        try:
                            r = _session.get(_mu, headers=hdrs, timeout=30, allow_redirects=True)
                            if r.status_code == 200 and len(r.content) > 500:
                                selfie_bytes = r.content
                        except Exception as e:
                            print(f"[MEDIA] download error: {e}", flush=True)

                    if not selfie_bytes and _mw and _cap_device_id:
                        try:
                            r = _session.get(f"{BASE}/v1/chat/{_cap_device_id}/files/{_mw}/download",
                                             headers=hdrs, timeout=20)
                            if r.status_code == 200 and len(r.content) > 500:
                                selfie_bytes = r.content
                        except Exception as e:
                            print(f"[MEDIA] fallback error: {e}", flush=True)

                if not selfie_bytes:
                    send_msg(_sender, "⚠️ ما قدرت أحمل الصورة. جرب مرة ثانية.")
                    return

                search_and_send(selfie_bytes, _sender, _event_code)
            except Exception as e:
                print(f"[ERROR] {e}", flush=True)
                send_msg(_sender, f"⚠️ {str(e)}")

        threading.Thread(target=run, daemon=True).start()
        return "", 200

    # ── Text received ─────────────────────────────────────────────────────────
    upper = body_text.upper().strip()

    if state == "choosing_event":
        today_list = conv.get("today_events", [])
        try:
            idx = int(body_text.strip()) - 1
            if 0 <= idx < len(today_list):
                chosen = today_list[idx]
                event  = get_event(chosen)
                _set_conv(sender, "awaiting_selfie", event_code=chosen)
                return _reply(f"✨ اخترت *{event['name']}*!\n\nأرسل لي *سيلفي واضح* لوجهك وسأجد صورك 📸")
        except (ValueError, TypeError):
            pass
        options = "\n".join(f"*{i+1}* — {get_event(c)['name']}" for i, c in enumerate(today_list))
        return _reply(f"أرسل رقم الحفل:\n\n{options}")

    if upper in _events:
        event = _events[upper]
        _set_conv(sender, "awaiting_selfie", event_code=upper)
        return _reply(
            f"🌙 أهلاً بك في *{event['name']}*!\n\n"
            "أرسل لي *سيلفي واضح* لوجهك وسأجد لك جميع صورك من الحفل 🎉📸"
        )

    if any(w in body_text for w in ("مرحبا", "هلا", "hi", "hello", "start", "مرحبً")):
        _clear_conv(sender)
        _end_agent_session(sender)
        state = "new"

    if state == "new":
        _set_conv(sender, "routing")
        # If the message is already a valid menu selection, skip the greeting
        # and fall through directly to the routing handler below.
        # This prevents returning users from having to press 1/2 twice.
        _already_selected = (
            body_text in ("1", "١", "📸 ابحث عن صوري", "2", "٢", "💬 استفسار وتواصل")
            or any(w in body_text for w in ("صور", "ضيف", "صورة", "حفل", "ابحث",
                                             "استفسار", "سؤال", "تواصل"))
        )
        if not _already_selected:
            threading.Thread(target=send_buttons, args=(sender, "🌙 أهلاً وسهلاً! كيف أقدر أساعدك؟", ["📸 ابحث عن صوري", "💬 استفسار وتواصل"]), daemon=True).start()
            return "", 200
        state = "routing"  # fall through

    if state == "routing":
        picked_photos  = body_text in ("1", "١", "📸 ابحث عن صوري") or \
                         any(w in body_text for w in ("صور", "ضيف", "صورة", "حفل", "ابحث"))
        picked_inquiry = body_text in ("2", "٢", "💬 استفسار وتواصل") or \
                         any(w in body_text for w in ("استفسار", "سؤال", "تواصل"))

        if picked_photos:
            todays = get_todays_events()
            if len(todays) == 1:
                code, event = todays[0]
                _set_conv(sender, "awaiting_selfie", event_code=code)
                return _reply(f"✨ أهلاً بك في *{event['name']}*!\n\nأرسل لي *سيلفي واضح* لوجهك وسأجد لك جميع صورك 🎉📸")
            elif len(_events) == 1:
                code  = next(iter(_events))
                event = _events[code]
                _set_conv(sender, "awaiting_selfie", event_code=code)
                return _reply(f"✨ أهلاً بك في *{event['name']}*!\n\nأرسل لي *سيلفي واضح* لوجهك وسأجد لك جميع صورك 🎉📸")
            elif len(todays) > 1:
                options = "\n".join(f"*{i+1}* — {e['name']}" for i, (_, e) in enumerate(todays))
                _set_conv(sender, "choosing_event")
                _conv[sender]["today_events"] = [c for c, _ in todays]
                return _reply(f"🌙 فيه أكثر من حفل اليوم، اختر الحفل الذي أنت فيه:\n\n{options}")
            _set_conv(sender, "awaiting_event_code")
            codes = ", ".join(_events.keys()) if _events else "(لا يوجد أحداث مسجلة)"
            return _reply(
                "✨ ممتاز!\n\n"
                f"أرسل لي *كود الحفل* — ستجده على QR الكود في الحفل.\n\n"
                f"الأحداث المتاحة: {codes}"
            )
        elif picked_inquiry:
            _set_conv(sender, "awaiting_inquiry")
            return _reply("بكل سرور! اكتب استفسارك وسنوصله لفريق الدعم 💬")
        else:
            threading.Thread(target=send_buttons, args=(sender, "من فضلك اختر من القائمة:", ["📸 ابحث عن صوري", "💬 استفسار وتواصل"]), daemon=True).start()
            return "", 200

    if state == "awaiting_event_code":
        return _reply(
            f"⚠️ الكود '{body_text}' غير موجود.\n\n"
            "تأكد من الكود وحاول مرة ثانية، أو امسح QR الكود من الحفل مباشرة."
        )

    if state == "awaiting_selfie":
        event_name = get_event(conv.get("event_code", ""))
        name = event_name["name"] if event_name else "الحفل"
        return _reply(f"📸 أرسل لي *سيلفي* لوجهك وسأجد صورك من {name}!")

    if state == "awaiting_inquiry":
        # First message → start bidirectional agent session
        clean_sender = sender.replace("whatsapp:", "")
        owner = OWNER_PHONE.lstrip("+")
        _start_agent_session(sender, owner)
        _set_conv(sender, "with_agent")
        send_msg(f"+{OWNER_PHONE}",
            f"📩 *استفسار جديد* — الضيف: +{clean_sender}\n\n"
            f"{body_text}\n\n"
            "_للرد: أجب على هذه الرسالة وسيصل للضيف تلقائياً_\n"
            "_لإنهاء المحادثة: أرسل *#end*_"
        )
        return _reply(
            "✅ تم تحويلك لفريق الدعم! سيردون عليك قريباً 🌙\n\n"
            "يمكنك الاستمرار في إرسال رسائلك."
        )

    if state == "with_agent":
        # Forward subsequent messages to owner
        clean_sender = sender.replace("whatsapp:", "")
        session = _get_agent_session(sender)
        if session:
            send_msg(f"+{OWNER_PHONE}",
                f"💬 *رسالة من الضيف* (+{clean_sender}):\n{body_text}"
            )
            return "", 200
        else:
            # Session expired
            _clear_conv(sender)
            threading.Thread(target=send_buttons, args=(sender, "🌙 انتهت جلسة الدعم. كيف أقدر أساعدك؟", ["📸 ابحث عن صوري", "💬 استفسار وتواصل"]), daemon=True).start()
            return "", 200

    # Fallback
    _clear_conv(sender)
    threading.Thread(target=send_buttons, args=(sender, "🌙 أهلاً وسهلاً! كيف أقدر أساعدك؟", ["📸 ابحث عن صوري", "💬 استفسار وتواصل"]), daemon=True).start()
    return "", 200


def _ensure_webhook():
    """Register Wassenger inbound webhook on startup if not already present."""
    if not WASSENGER_API_KEY:
        return
    headers = {"Authorization": WASSENGER_API_KEY, "Content-Type": "application/json"}
    webhook_url = f"{APP_URL}/whatsapp"
    try:
        hooks_r = requests.get("https://api.wassenger.com/v1/webhooks", headers=headers, timeout=15)
        hooks = hooks_r.json() if hooks_r.status_code == 200 else []
        already = any(h.get("url") == webhook_url for h in (hooks if isinstance(hooks, list) else []))
        if already:
            print("[WEBHOOK_INIT] Webhook already registered", flush=True)
            return
        r = requests.post("https://api.wassenger.com/v1/webhooks", json={
            "name": "qamra-webhook",
            "url": webhook_url,
            "events": ["message:in:new"],
        }, headers=headers, timeout=15)
        if r.status_code in (200, 201):
            print(f"[WEBHOOK_INIT] Webhook registered: {r.json().get('id')}", flush=True)
        else:
            print(f"[WEBHOOK_INIT] Registration failed {r.status_code}: {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[WEBHOOK_INIT] Error: {e}", flush=True)

threading.Thread(target=_ensure_webhook, daemon=True).start()
threading.Thread(target=_ensure_s3_bucket, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
