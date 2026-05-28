import os
import io
import json
import threading
import hashlib
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

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

FACE_SERVICE_URL = os.environ.get("FACE_SERVICE_URL", "http://46.62.172.232:5001")
FACE_SERVICE_KEY = os.environ.get("FACE_SERVICE_KEY", "qamra-face-2026")
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS"]
APP_URL                 = os.environ.get("APP_URL", "https://qamra-production.up.railway.app")
ADMIN_TOKEN             = os.environ.get("ADMIN_TOKEN", "qamra-admin-2026")
OWNER_PHONE             = os.environ.get("OWNER_PHONE", "97470263297")
OWNER_EMAIL             = os.environ.get("OWNER_EMAIL", "")
GDRIVE_FOLDER_ID        = os.environ.get("GDRIVE_FOLDER_ID", "")

MATCH_CONF  = int(os.environ.get("MATCH_CONF", "50"))  # InsightFace cosine similarity * 100
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


# ── Duplicate webhook deduplication ──────────────────────────────────────────
_seen_msg_ids: dict = {}  # msg_id -> timestamp
_seen_lock = threading.Lock()

def _is_duplicate_msg(msg_id: str) -> bool:
    if not msg_id:
        return False
    now = time.time()
    with _seen_lock:
        for k in [k for k, t in list(_seen_msg_ids.items()) if now - t > 60]:
            del _seen_msg_ids[k]
        if msg_id in _seen_msg_ids:
            return True
        _seen_msg_ids[msg_id] = now
    return False

# ── Human agent session tracking ──────────────────────────────────────────────
# Maps user_phone → { "owner": owner_phone, "ts": float }
_agent_sessions: dict = {}
_AGENT_TTL = 7200  # 2 hours

# ── Inquiry confirmation timers ────────────────────────────────────────────────
# Maps user_phone → threading.Timer (fires 2 min after last inquiry message)
_inquiry_timers: dict = {}

def _cancel_inquiry_timer(phone):
    t = _inquiry_timers.pop(phone, None)
    if t:
        t.cancel()

def _start_inquiry_timer(phone):
    _cancel_inquiry_timer(phone)
    def _confirm():
        _inquiry_timers.pop(phone, None)
        send_msg(phone, "✅ تم إرسال استفسارك لفريق الدعم وسيتم الرد عليك قريباً 🌙")
    t = threading.Timer(120, _confirm)
    t.daemon = True
    t.start()
    _inquiry_timers[phone] = t


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


# ── Face service auth header ──────────────────────────────────────────────────
def _event_btn_label(name: str) -> str:
    """Last word of the name — fits WhatsApp's 20-char button limit."""
    return name.strip().split()[-1][:20]

def _event_picker_body_and_btns(all_ev):
    """Full names in the message body, short button labels the user can press."""
    full_list = "\n".join(f"• {e['name']}" for _, e in all_ev)
    body = f"🌙 لأي حفل تبحث عن صورك؟\n\n{full_list}"
    btn_labels = [_event_btn_label(e["name"]) for _, e in all_ev[:3]]
    return body, btn_labels

def _face_hdrs():
    return {"X-API-Key": FACE_SERVICE_KEY}

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

def _save_events_to_vps(data):
    try:
        _session.put(
            f"{FACE_SERVICE_URL}/v1/kv/events",
            json={"value": json.dumps(data, ensure_ascii=False)},
            headers=_face_hdrs(),
            timeout=10,
        )
        print(f"[EVENTS] Saved {len(data)} events to VPS", flush=True)
    except Exception as e:
        print(f"[EVENTS] VPS save error: {e}", flush=True)

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
        r = _session.get(
            f"{FACE_SERVICE_URL}/v1/kv/events",
            headers=_face_hdrs(), timeout=10,
        )
        if r.status_code == 200:
            _events = json.loads(r.json()["value"])
            with open(EVENTS_FILE, "w") as f:
                json.dump(_events, f)
            print(f"[EVENTS] Loaded {len(_events)} events from VPS", flush=True)
        else:
            print("[EVENTS] No events config found — start fresh.", flush=True)
    except Exception as e:
        print(f"[EVENTS] VPS load error: {e}", flush=True)

def save_events():
    with open(EVENTS_FILE, "w") as f:
        json.dump(_events, f, ensure_ascii=False)
    threading.Thread(target=_save_events_to_vps, args=(_events.copy(),), daemon=True).start()

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
    threading.Thread(target=_save_events_to_vps, args=(_events.copy(),), daemon=True).start()

# ── Per-event state ───────────────────────────────────────────────────────────
def _state_file(event_code):
    return f"/tmp/qamra_state_{event_code}.json"

def _state_drive_name(event_code):
    return f"_qamra_state_{event_code}_.json"

def _save_state_to_vps(event_code, data):
    try:
        key = f"state_{event_code}"
        _session.put(
            f"{FACE_SERVICE_URL}/v1/kv/{key}",
            json={"value": json.dumps(data, ensure_ascii=False)},
            headers=_face_hdrs(),
            timeout=10,
        )
        print(f"[STATE] Saved {event_code} to VPS ({len(data.get('indexed_ids',[]))} ids)", flush=True)
    except Exception as e:
        print(f"[STATE] VPS save error: {e}", flush=True)

# In-memory state cache — survives loop runs, lost only on Railway restart
# On restart, Drive is the backup. This prevents re-indexing within a session.
_state_cache: dict = {}

def load_state(event_code):
    # 1. Check in-memory cache first — fastest, no API calls
    if event_code in _state_cache:
        return _state_cache[event_code]

    path = _state_file(event_code)
    try:
        with open(path) as f:
            s = json.load(f)
            if s.get("indexed_ids") is not None:
                _state_cache[event_code] = s
                return s
    except Exception:
        pass
    try:
        key = f"state_{event_code}"
        r   = _session.get(
            f"{FACE_SERVICE_URL}/v1/kv/{key}",
            headers=_face_hdrs(), timeout=10,
        )
        if r.status_code == 200:
            s = json.loads(r.json()["value"])
            with open(path, "w") as f:
                json.dump(s, f)
            _state_cache[event_code] = s
            print(f"[STATE] Loaded {event_code} from VPS ({len(s.get('indexed_ids',[]))} ids)", flush=True)
            return s
    except Exception as e:
        print(f"[STATE] VPS load error: {e}", flush=True)
    empty = {"indexed_ids": [], "file_map": {}, "face_map": {}}
    _state_cache[event_code] = empty
    return empty

def save_state(event_code, state):
    _state_cache[event_code] = state  # update memory first — instant, no API calls
    with open(_state_file(event_code), "w") as f:
        json.dump(state, f)
    threading.Thread(target=_save_state_to_vps, args=(event_code, state.copy()), daemon=True).start()

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

# ── Azure Face helpers ────────────────────────────────────────────────────────
def ensure_facelist(face_list_id):
    pass  # InsightFace uses SQLite collections — no setup needed

def train_facelist(face_list_id):
    pass  # not needed

def index_face(image_bytes, file_id, face_list_id):
    """Send photo to VPS face service, returns (face_tokens, count)."""
    try:
        r = _session.post(
            f"{FACE_SERVICE_URL}/v1/index",
            files={"photo": ("photo.jpg", image_bytes, "image/jpeg")},
            data={"file_id": file_id, "collection_id": face_list_id},
            headers=_face_hdrs(),
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[INDEX] Face service error {r.status_code}: {r.text[:200]}", flush=True)
            return [], 0
        tokens = r.json().get("face_tokens", [])
        return tokens, len(tokens)
    except Exception as e:
        print(f"[INDEX] Error: {e}", flush=True)
        return [], 0

def count_faces(image_bytes):
    try:
        r = _session.post(
            f"{FACE_SERVICE_URL}/v1/detect",
            files={"photo": ("photo.jpg", image_bytes, "image/jpeg")},
            headers=_face_hdrs(),
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("count", 1)
        return 1
    except Exception as e:
        print(f"[COUNT] Error: {e}", flush=True)
        return 1

def search_by_selfie(selfie_bytes, face_list_id):
    """Search VPS face service for faces matching the selfie."""
    try:
        r = _session.post(
            f"{FACE_SERVICE_URL}/v1/search",
            files={"photo": ("selfie.jpg", selfie_bytes, "image/jpeg")},
            data={"collection_id": face_list_id},
            headers=_face_hdrs(),
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[SEARCH] Face service error {r.status_code}: {r.text[:200]}", flush=True)
            return []
        results = r.json().get("results", [])
        matches = [
            {"persistedFaceId": m["face_token"], "confidence": m["confidence"] / 100.0}
            for m in results if m.get("confidence", 0) >= MATCH_CONF
        ]
        print(f"[SEARCH] {len(matches)} matches in {face_list_id}", flush=True)
        return matches
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
    """List all images recursively inside gdrive_folder_id (handles camera DCIM subfolders).
    Returns (photos, success) — success=False means a Drive API error occurred."""
    try:
        svc = _drive()
        all_results = []
        folders_to_scan = [gdrive_folder_id]

        while folders_to_scan:
            current = folders_to_scan.pop(0)

            # Find subfolders
            sub_pt = None
            while True:
                sub_resp = svc.files().list(
                    q=f"'{current}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                    fields="nextPageToken, files(id)",
                    pageSize=100, pageToken=sub_pt,
                ).execute()
                for f in sub_resp.get("files", []):
                    folders_to_scan.append(f["id"])
                sub_pt = sub_resp.get("nextPageToken")
                if not sub_pt:
                    break

            # Collect images in current folder
            pt = None
            while True:
                resp = svc.files().list(
                    q=f"'{current}' in parents and mimeType contains 'image/' and trashed=false",
                    fields="nextPageToken, files(id, name, webViewLink)",
                    pageSize=200, pageToken=pt,
                ).execute()
                all_results.extend(resp.get("files", []))
                pt = resp.get("nextPageToken")
                if not pt:
                    break

        return all_results, True
    except Exception as e:
        print(f"[DRIVE] list_drive_photos error: {e}", flush=True)
        return [], False

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
        ensure_facelist(collection_id)
        state       = load_state(event_code)
        indexed_ids = set(state.get("indexed_ids", []))
        file_map    = state.get("file_map", {})
        face_map    = state.get("face_map", {})  # persistedFaceId → drive file_id

        # Full re-index: clear VPS collection so orphan tokens don't accumulate
        if not indexed_ids:
            try:
                _session.delete(
                    f"{FACE_SERVICE_URL}/v1/clear/{collection_id}",
                    headers=_face_hdrs(), timeout=10,
                )
                print(f"[INDEX] Cleared VPS collection {collection_id}", flush=True)
            except Exception as e:
                print(f"[INDEX] VPS clear error (non-fatal): {e}", flush=True)

        photos, drive_ok = list_drive_photos(gdrive_folder_id)
        print(f"[INDEX] {event_code}: {len(photos)} photos, {len(indexed_ids)} indexed", flush=True)

        no_face_ids = set(state.get("no_face_ids", []))

        # Remove files deleted from Drive.
        # Only run if the Drive API call succeeded (drive_ok=True), even if it returned 0 photos,
        # so a genuinely empty folder correctly clears the gallery.
        if drive_ok:
            current_drive_ids = {p["id"] for p in photos}
            deleted_indexed   = indexed_ids - current_drive_ids
            deleted_no_face   = no_face_ids - current_drive_ids
            deleted_ids       = deleted_indexed | deleted_no_face
            if deleted_ids:
                indexed_ids -= deleted_indexed
                no_face_ids -= deleted_no_face
                for fid in deleted_ids:
                    file_map.pop(fid, None)
                face_map = {k: v for k, v in face_map.items() if v not in deleted_ids}
                print(f"[INDEX] {event_code}: removed {len(deleted_ids)} deleted files from index", flush=True)
                # Save immediately so gallery reflects deletion right away,
                # before the (potentially long) new-photo indexing loop runs
                state["indexed_ids"] = list(indexed_ids)
                state["no_face_ids"] = list(no_face_ids)
                state["file_map"]    = file_map
                state["face_map"]    = face_map
                save_state(event_code, state)
                threading.Thread(
                    target=_cleanup_guest_tokens,
                    args=(event_code, deleted_ids),
                    daemon=True,
                ).start()
        new_count = 0
        for i, photo in enumerate(photos):
            if photo["id"] in indexed_ids or photo["id"] in no_face_ids:
                continue
            try:
                img_bytes = download_file(photo["id"])
                persisted_ids, n = index_face(img_bytes, photo["id"], collection_id)
                # Always add to file_map so all photos appear in the full gallery
                file_map[photo["id"]] = {"name": photo["name"], "link": photo["webViewLink"]}
                if n > 0:
                    indexed_ids.add(photo["id"])
                    for pid in persisted_ids:
                        face_map[pid] = photo["id"]
                    new_count += n
                else:
                    no_face_ids.add(photo["id"])
            except Exception as e:
                print(f"[INDEX] Error {photo['name']}: {e}", flush=True)

            if (i + 1) % 20 == 0:
                state["indexed_ids"]  = list(indexed_ids)
                state["no_face_ids"]  = list(no_face_ids)
                state["file_map"]     = file_map
                state["face_map"]     = face_map
                save_state(event_code, state)
                print(f"[INDEX] {event_code}: {i+1}/{len(photos)}, {new_count} new", flush=True)

        state["indexed_ids"]  = list(indexed_ids)
        state["no_face_ids"]  = list(no_face_ids)
        state["file_map"]     = file_map
        state["face_map"]     = face_map
        save_state(event_code, state)

        if new_count > 0:
            train_facelist(collection_id)

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
        time.sleep(10)

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
def get_next_guest_number(event_code):
    key = f"guest_counter_{event_code}"
    try:
        r = _session.get(f"{FACE_SERVICE_URL}/v1/kv/{key}", headers=_face_hdrs(), timeout=5)
        current = int(r.json().get("value", "0")) if r.status_code == 200 else 0
    except Exception:
        current = 0
    nxt = current + 1
    try:
        _session.put(
            f"{FACE_SERVICE_URL}/v1/kv/{key}",
            json={"value": str(nxt)},
            headers=_face_hdrs(), timeout=5,
        )
    except Exception:
        pass
    return nxt

def _add_shortcuts(svc, folder_id, file_ids, file_map=None):
    """Add Drive shortcuts for file_ids into an existing folder. Batched, parallel."""
    if not file_ids:
        return
    batches = []
    for chunk_start in range(0, len(file_ids), 100):
        err_list = []
        def _cb(req_id, resp, exc, _el=err_list):
            if exc: _el.append(str(exc))
        batch = svc.new_batch_http_request(callback=_cb)
        for fid in file_ids[chunk_start:chunk_start + 100]:
            fname = (file_map or {}).get(fid, {}).get("name", fid)
            batch.add(svc.files().create(
                body={
                    "name": fname,
                    "mimeType": "application/vnd.google-apps.shortcut",
                    "shortcutDetails": {"targetId": fid},
                    "parents": [folder_id],
                },
                fields="id"
            ))
        batches.append((batch, err_list))

    total_errors = []
    with ThreadPoolExecutor(max_workers=min(len(batches), 5)) as ex:
        futs = {ex.submit(b.execute): el for b, el in batches}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                total_errors.append(str(e))
            total_errors.extend(futs[fut])

    if total_errors:
        print(f"[FOLDER] {len(total_errors)} shortcut errors", flush=True)


def create_guest_folder(guest_num, file_ids, event_name, file_map=None):
    svc = _drive(write=True)
    folder = svc.files().create(body={
        "name": f"صورك من {event_name} 🌙 — ضيف {guest_num}",
        "mimeType": "application/vnd.google-apps.folder",
    }, fields="id").execute()
    folder_id = folder["id"]
    svc.permissions().create(
        fileId=folder_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    _add_shortcuts(svc, folder_id, file_ids, file_map)

    link = f"https://drive.google.com/drive/folders/{folder_id}"
    print(f"[FOLDER] Created: {link} ({len(file_ids)} shortcuts)", flush=True)
    return link

_SELFIE_TIPS = (
    "\n\n*نصائح للحصول على أفضل نتيجة:*\n"
    "• أنت وحدك في الصورة 🙋\n"
    "• وجهك واضح ومضاء جيداً 💡\n"
    "• الكاميرا في مستوى وجهك 📱\n"
    "• تجنب النظارات الشمسية 🕶️"
)

# ── Ratings ───────────────────────────────────────────────────────────────────
_RATINGS_XLSX = "_qamra_ratings_.xlsx"

def _append_rating_to_excel(event_code, phone, rating, comment, ts):
    import openpyxl
    from googleapiclient.http import MediaIoBaseUpload
    from datetime import datetime, timezone
    svc = _drive(write=True)
    dt  = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Find existing file
    resp = svc.files().list(
        q=f"name='{_RATINGS_XLSX}' and trashed=false",
        fields="files(id)", pageSize=1,
    ).execute()
    files = resp.get("files", [])

    if files:
        file_id = files[0]["id"]
        raw = download_file(file_id)
        wb  = openpyxl.load_workbook(io.BytesIO(raw))
        ws  = wb.active
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Ratings"
        ws.append(["Timestamp", "Phone", "Event", "Rating", "Comment"])
        file_id = None

    ws.append([dt, f"+{phone}", event_code, rating, comment])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    if file_id:
        svc.files().update(
            fileId=file_id,
            media_body=MediaIoBaseUpload(buf, mimetype=mime),
        ).execute()
    else:
        new_file = svc.files().create(
            body={"name": _RATINGS_XLSX},
            media_body=MediaIoBaseUpload(buf, mimetype=mime),
            fields="id",
        ).execute()
        if OWNER_EMAIL:
            svc.permissions().create(
                fileId=new_file["id"],
                body={"type": "user", "role": "writer", "emailAddress": OWNER_EMAIL},
                sendNotificationEmail=False,
            ).execute()

    print(f"[RATING] Excel updated: {phone} {rating}/10 for {event_code}", flush=True)

def save_rating(event_code, phone, rating, comment=""):
    # Save to VPS KV
    key = f"ratings_{event_code}"
    try:
        r = _session.get(f"{FACE_SERVICE_URL}/v1/kv/{key}", headers=_face_hdrs(), timeout=10)
        ratings = json.loads(r.json()["value"]) if r.status_code == 200 else []
    except Exception:
        ratings = []
    ts = time.time()
    ratings.append({"phone": phone, "rating": rating, "comment": comment, "ts": ts})
    try:
        _session.put(
            f"{FACE_SERVICE_URL}/v1/kv/{key}",
            json={"value": json.dumps(ratings, ensure_ascii=False)},
            headers=_face_hdrs(), timeout=10,
        )
    except Exception as e:
        print(f"[RATING] VPS save error: {e}", flush=True)
    # Save to Excel on Drive
    try:
        _append_rating_to_excel(event_code, phone, rating, comment, ts)
    except Exception as e:
        print(f"[RATING] Excel error: {e}", flush=True)

# ── WhatsApp search + send ────────────────────────────────────────────────────
def search_and_send(selfie_bytes, sender, event_code):
    print(f"[SEARCH] start sender={sender} event={event_code} selfie_size={len(selfie_bytes)}", flush=True)
    event    = get_event(event_code)
    if not event:
        send_msg(sender, "⚠️ الحفل غير موجود. تأكد من الكود وحاول مرة ثانية.")
        return

    face_count = count_faces(selfie_bytes)
    if face_count > 1:
        send_msg(sender, "📸 الصورة تحتوي على أكثر من وجه!\n\nأرسل سيلفي لوجهك *منفرداً* حتى أتمكن من إيجاد صورك بدقة 🎯")
        return

    state    = load_state(event_code)
    file_map = state.get("file_map", {})
    face_map = state.get("face_map", {})
    print(f"[SEARCH] indexed={len(state.get('indexed_ids',[]))} facelist={event.get('collection_id')}", flush=True)

    matches = search_by_selfie(selfie_bytes, event["collection_id"])
    print(f"[SEARCH] azure matches={len(matches) if matches else 0}", flush=True)
    if not matches:
        send_msg(sender, "😕 ما لقيت وجه في الصورة أو ما لقيت صورك — أرسل سيلفي واضح وحاول مرة ثانية")
        return

    seen_ids, file_ids = set(), []
    for m in matches:
        file_id = face_map.get(m["persistedFaceId"])
        if file_id and file_id not in seen_ids:
            seen_ids.add(file_id)
            file_ids.append(file_id)

    count = len(file_ids)

    try:
        phone_label = sender.replace("whatsapp:", "").replace("+", "")

        # Send 3 random photos as teasers
        import random
        teaser_ids = random.sample(file_ids, min(3, len(file_ids)))
        send_msg(sender, f"🎉 وجدت *{count}* صورة لك من *{event['name']}*! إليك بعض منها:")
        with ThreadPoolExecutor(max_workers=3) as ex:
            ex.map(lambda fid: send_msg(sender, " ", media_url=f"{APP_URL}/photo/{fid}"), teaser_ids)

        send_msg(sender, "⏳ جاري تجهيز معرض صورك الخاص، لحظة واحدة... 🖼️")

        gallery_token = create_gallery_token(event_code, file_ids, phone=phone_label)
        gallery_link  = f"{APP_URL}/gallery/{event_code}/g/{gallery_token}"
        print(f"[GALLERY] Sending link to {sender}: {gallery_link}", flush=True)
        send_msg(sender,
            f"🖼️ معرض صورك الخاص جاهز:\n{gallery_link}\n\n"
            "اضغط على أي صورة لحفظها بجودتها الأصلية، أو احفظ الكل دفعة واحدة 🌙"
        )

        # Register guest for future new-photo notifications (background)
        threading.Thread(
            target=_register_guest,
            args=(event_code, phone_label, selfie_bytes, file_ids, gallery_token),
            daemon=True,
        ).start()

        time.sleep(1)

        # Ask if they want photos sent directly on WhatsApp
        _set_conv(sender, "awaiting_send_confirm", event_code=event_code)
        _conv[sender]["meta"] = {"file_ids": file_ids, "gallery_link": gallery_link}
        send_buttons(sender,
            "📲 هل تريد أن أرسل لك صورك مباشرة هنا؟\n"
            "⚠️ تنبيه: الصور المرسلة عبر واتساب ستكون بجودة أقل من الأصلية. للجودة الكاملة افتح معرضك الشخصي واحفظ منه.",
            ["نعم، أرسل صوري 📲", "لا، شكراً"],
        )
        print(f"[SEARCH] Flow complete — awaiting send confirm", flush=True)
    except Exception as e:
        print(f"[REPLY] ERROR: {e}", flush=True)
        send_msg(sender, f"✅ وجدت *{count}* صورة لك من *{event['name']}* 🎉 — تواصل مع المصور لاستلامها.")

# ── Short link redirect store ─────────────────────────────────────────────────
_short_links: dict = {}  # code -> drive_folder_url

def make_short_link(folder_url: str) -> str:
    code = hashlib.md5(folder_url.encode()).hexdigest()[:8]
    _short_links[code] = folder_url
    return f"{APP_URL}/f/{code}"

# ── Gallery token store ───────────────────────────────────────────────────────
import secrets, zipfile

GALLERY_TTL = 7 * 24 * 3600  # 7 days

def create_gallery_token(event_code: str, file_ids: list, phone: str = "") -> str:
    token = secrets.token_urlsafe(6)[:8]
    payload = json.dumps({
        "event_code": event_code,
        "file_ids":   file_ids,
        "phone":      phone,
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + GALLERY_TTL,
    }, ensure_ascii=False)
    try:
        _session.put(
            f"{FACE_SERVICE_URL}/v1/kv/gallery_{token}",
            json={"value": payload},
            headers=_face_hdrs(),
            timeout=10,
        )
    except Exception as e:
        print(f"[GALLERY] KV save error: {e}", flush=True)
    return token

def load_gallery_token(token: str):
    try:
        r = _session.get(
            f"{FACE_SERVICE_URL}/v1/kv/gallery_{token}",
            headers=_face_hdrs(), timeout=10,
        )
        if r.status_code != 200:
            return None
        data = json.loads(r.json()["value"])
        if time.time() > data.get("expires_at", 0):
            return None
        return data
    except Exception as e:
        print(f"[GALLERY] KV load error: {e}", flush=True)
        return None

def _update_gallery_token_file_ids(token: str, all_file_ids: list):
    """Overwrite file_ids in an existing gallery token so the same URL shows updated photos."""
    try:
        r = _session.get(f"{FACE_SERVICE_URL}/v1/kv/gallery_{token}", headers=_face_hdrs(), timeout=10)
        if r.status_code != 200:
            return
        data = json.loads(r.json()["value"])
        data["file_ids"] = all_file_ids
        _session.put(
            f"{FACE_SERVICE_URL}/v1/kv/gallery_{token}",
            json={"value": json.dumps(data, ensure_ascii=False)},
            headers=_face_hdrs(), timeout=10,
        )
    except Exception as e:
        print(f"[GALLERY] Token update error: {e}", flush=True)

# ── Guest notification registry ───────────────────────────────────────────────
import base64

NOTIFY_COOLDOWN = 1800  # 30 minutes between notifications per guest

def _register_guest(event_code: str, phone: str, selfie_bytes: bytes, file_ids: list, token: str):
    """Store guest selfie + match state so notification loop can re-search them."""
    try:
        guest_data = {
            "phone":         phone,
            "event_code":    event_code,
            "selfie_b64":    base64.b64encode(selfie_bytes).decode(),
            "file_ids":      file_ids,
            "token":         token,
            "last_notified": int(time.time()),
        }
        _session.put(
            f"{FACE_SERVICE_URL}/v1/kv/guest_{event_code}_{phone}",
            json={"value": json.dumps(guest_data, ensure_ascii=False)},
            headers=_face_hdrs(), timeout=10,
        )
        # Update guest index for this event
        try:
            r = _session.get(f"{FACE_SERVICE_URL}/v1/kv/guest_index_{event_code}", headers=_face_hdrs(), timeout=5)
            index = json.loads(r.json()["value"]) if r.status_code == 200 else []
        except Exception:
            index = []
        if phone not in index:
            index.append(phone)
            _session.put(
                f"{FACE_SERVICE_URL}/v1/kv/guest_index_{event_code}",
                json={"value": json.dumps(index)},
                headers=_face_hdrs(), timeout=10,
            )
        print(f"[NOTIFY] Registered guest {phone} for {event_code}", flush=True)
    except Exception as e:
        print(f"[NOTIFY] Registration error: {e}", flush=True)

def _cleanup_guest_tokens(event_code: str, deleted_ids: set):
    """Remove deleted Drive file IDs from all guest gallery tokens for an event."""
    try:
        r = _session.get(f"{FACE_SERVICE_URL}/v1/kv/guest_index_{event_code}", headers=_face_hdrs(), timeout=10)
        if r.status_code != 200:
            return
        index = json.loads(r.json()["value"])
    except Exception as e:
        print(f"[CLEANUP] Index load error {event_code}: {e}", flush=True)
        return

    for phone in index:
        try:
            r = _session.get(f"{FACE_SERVICE_URL}/v1/kv/guest_{event_code}_{phone}", headers=_face_hdrs(), timeout=10)
            if r.status_code != 200:
                continue
            guest_data = json.loads(r.json()["value"])

            old_ids = guest_data.get("file_ids", [])
            new_ids = [fid for fid in old_ids if fid not in deleted_ids]
            if len(new_ids) == len(old_ids):
                continue

            guest_data["file_ids"] = new_ids
            _session.put(
                f"{FACE_SERVICE_URL}/v1/kv/guest_{event_code}_{phone}",
                json={"value": json.dumps(guest_data, ensure_ascii=False)},
                headers=_face_hdrs(), timeout=10,
            )
            token = guest_data.get("token", "")
            if token:
                _update_gallery_token_file_ids(token, new_ids)
            print(f"[CLEANUP] {phone}: removed {len(old_ids) - len(new_ids)} deleted photos", flush=True)
        except Exception as e:
            print(f"[CLEANUP] Error for {phone}: {e}", flush=True)


def _notify_new_photos_for_event(event_code: str):
    try:
        r = _session.get(f"{FACE_SERVICE_URL}/v1/kv/guest_index_{event_code}", headers=_face_hdrs(), timeout=10)
        if r.status_code != 200:
            return
        index = json.loads(r.json()["value"])
    except Exception as e:
        print(f"[NOTIFY] Index load error {event_code}: {e}", flush=True)
        return

    event = get_event(event_code)
    if not event:
        return

    now = int(time.time())
    state = load_state(event_code)
    face_map = state.get("face_map", {})

    for phone in index:
        try:
            r = _session.get(f"{FACE_SERVICE_URL}/v1/kv/guest_{event_code}_{phone}", headers=_face_hdrs(), timeout=10)
            if r.status_code != 200:
                continue
            guest_data = json.loads(r.json()["value"])

            if now - guest_data.get("last_notified", 0) < NOTIFY_COOLDOWN:
                continue

            selfie_bytes = base64.b64decode(guest_data["selfie_b64"])
            matches = search_by_selfie(selfie_bytes, event["collection_id"])
            if not matches:
                continue

            seen, all_file_ids = set(), []
            for m in matches:
                fid = face_map.get(m["persistedFaceId"])
                if fid and fid not in seen:
                    seen.add(fid)
                    all_file_ids.append(fid)

            old_ids   = set(guest_data.get("file_ids", []))
            new_ids   = [fid for fid in all_file_ids if fid not in old_ids]
            if not new_ids:
                continue

            # Update the existing gallery token in-place — same URL, new photos visible
            token = guest_data.get("token", "")
            if token:
                _update_gallery_token_file_ids(token, all_file_ids)

            gallery_link = f"{APP_URL}/gallery/{event_code}/g/{token}" if token else ""

            # Send new photos directly on WhatsApp
            send_msg(f"+{phone}",
                f"📸 مرحباً! عندنا أخبار حلوة — وجدنا *{len(new_ids)} صور جديدة* لك من *{event['name']}* 🎉\n"
                "إليك صورك الجديدة:"
            )
            for fid in new_ids[:10]:
                send_msg(f"+{phone}", " ", media_url=f"{APP_URL}/photo/{fid}")
                time.sleep(0.5)

            if gallery_link:
                send_msg(f"+{phone}",
                    f"💾 هل أعجبتك الصور؟\n"
                    f"لحفظها بجودتها الأصلية الكاملة — بدون أي ضغط — افتح معرضك الشخصي:\n"
                    f"{gallery_link}\n\n"
                    "أو اضغط *«حفظ الكل»* داخل المعرض لتحميل جميع صورك دفعة واحدة 🌙"
                )

            # Update guest registry
            guest_data["file_ids"]      = all_file_ids
            guest_data["last_notified"] = now
            _session.put(
                f"{FACE_SERVICE_URL}/v1/kv/guest_{event_code}_{phone}",
                json={"value": json.dumps(guest_data, ensure_ascii=False)},
                headers=_face_hdrs(), timeout=10,
            )
            print(f"[NOTIFY] {phone}: {len(new_ids)} new photos sent", flush=True)
        except Exception as e:
            print(f"[NOTIFY] Error for guest {phone}: {e}", flush=True)

def _notification_loop():
    time.sleep(60)  # Initial warm-up delay
    while True:
        with _events_lock:
            codes = list(_events.keys())
        for code in codes:
            try:
                _notify_new_photos_for_event(code)
            except Exception as e:
                print(f"[NOTIFY] Loop error {code}: {e}", flush=True)
        time.sleep(NOTIFY_COOLDOWN)  # 30 minutes

threading.Thread(target=_notification_loop, daemon=True).start()

# ── Gallery HTML template ─────────────────────────────────────────────────────
def _gallery_html(event_name: str, token: str, event_code: str, file_ids: list, file_map: dict) -> str:
    photos_js = json.dumps([
        {"id": fid}
        for fid in file_ids
    ], ensure_ascii=False)

    zip_url = f"{APP_URL}/gallery/{event_code}/g/{token}/zip"

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>قمرة — صورك من {event_name}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter+Tight:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#F2EDE3;
  --bg-alt:#E8E1D2;
  --paper:#FAF6EC;
  --ink:#1A1612;
  --ink-soft:#4A413A;
  --ink-mute:rgba(26,22,18,0.50);
  --ink-faint:rgba(26,22,18,0.18);
  --rule:rgba(26,22,18,0.14);
  --gold:#C9A96E;
  --gold-soft:rgba(201,169,110,0.12);
}}
html,body{{min-height:100%;background:var(--bg);color:var(--ink);
  font-family:'Inter Tight',-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased;direction:rtl}}

/* ── Header ── */
.header{{
  background:var(--paper);
  border-bottom:1px solid var(--rule);
  padding:28px 20px 22px;
  text-align:center;
}}
.header-brand{{
  font-family:'JetBrains Mono',monospace;
  font-size:11px;letter-spacing:.22em;text-transform:uppercase;
  color:var(--ink-mute);margin-bottom:10px;
}}
.header-event{{
  font-family:'Instrument Serif',serif;
  font-size:clamp(22px,6vw,34px);font-weight:400;font-style:italic;
  color:var(--ink);margin-bottom:6px;line-height:1.2;
}}
.header-count{{
  display:inline-flex;align-items:center;gap:8px;
  font-family:'JetBrains Mono',monospace;
  font-size:12px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--gold);margin-top:4px;
}}
.header-count::before{{content:"";display:block;width:16px;height:1px;background:var(--gold);opacity:.6}}
.header-count::after{{content:"";display:block;width:16px;height:1px;background:var(--gold);opacity:.6}}

/* ── Action bar ── */
.action-bar{{
  display:flex;align-items:center;justify-content:space-between;
  padding:12px 16px;
  background:var(--bg-alt);
  border-bottom:1px solid var(--rule);
  gap:12px;
}}
.action-hint{{
  font-family:'JetBrains Mono',monospace;
  font-size:11px;letter-spacing:.08em;color:var(--ink-mute);
}}
.btn-save-all{{
  display:inline-flex;align-items:center;gap:10px;
  background:var(--ink);color:var(--bg);
  border:none;padding:11px 20px;
  font-family:'Inter Tight',sans-serif;
  font-size:13px;font-weight:500;letter-spacing:.02em;
  cursor:pointer;text-decoration:none;
  white-space:nowrap;transition:background .15s;
  flex-shrink:0;
}}
.btn-save-all:active{{background:var(--ink-soft)}}

/* ── Gallery grid ── */
.gallery{{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(160px,1fr));
  gap:3px;padding:3px;
  background:var(--bg-alt);
}}
@media(max-width:480px){{.gallery{{grid-template-columns:repeat(2,1fr)}}}}

.photo-card{{
  position:relative;aspect-ratio:4/3;overflow:hidden;
  background:var(--bg-alt);cursor:pointer;
}}
.photo-card img{{
  width:100%;height:100%;object-fit:contain;display:block;
  background:var(--bg);transition:transform .3s ease;
}}
.photo-card:active img{{transform:scale(1.04)}}
.photo-card .skeleton{{
  position:absolute;inset:0;
  background:linear-gradient(90deg,var(--bg-alt) 25%,var(--rule) 50%,var(--bg-alt) 75%);
  background-size:200% 100%;animation:shimmer 1.4s infinite;
}}
@keyframes shimmer{{0%{{background-position:200% 0}}100%{{background-position:-200% 0}}}}
.photo-card .overlay{{
  position:absolute;bottom:0;left:0;right:0;
  background:linear-gradient(to top,rgba(26,22,18,.75) 0%,transparent 100%);
  padding:28px 8px 8px;
  display:flex;align-items:flex-end;justify-content:center;
}}
.btn-save{{
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  background:rgba(250,246,236,.92);color:var(--ink);
  border:none;padding:10px;width:100%;
  font-family:'Inter Tight',sans-serif;font-size:13px;font-weight:600;
  cursor:pointer;text-decoration:none;white-space:nowrap;
}}
.btn-save:active{{background:var(--paper)}}

/* ── Lightbox ── */
.lightbox{{
  display:none;position:fixed;inset:0;
  background:rgba(26,22,18,.96);z-index:1000;
  flex-direction:column;align-items:center;justify-content:center;
  padding:16px;
}}
.lightbox.open{{display:flex}}
.lightbox-img-wrap{{display:flex;align-items:center;justify-content:center;flex:1;width:100%}}
.lightbox img{{max-width:95vw;max-height:72vh;object-fit:contain}}
.lightbox-close{{
  position:absolute;top:14px;right:14px;
  background:rgba(250,246,236,.1);border:none;
  color:var(--paper);width:40px;height:40px;border-radius:50%;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  font-size:20px;
}}
.lightbox-close:active{{background:rgba(250,246,236,.2)}}
.lightbox-footer{{
  display:flex;align-items:center;flex-direction:column;gap:10px;padding-top:16px;
  width:100%;max-width:340px;
}}
.lb-hint{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.08em;color:rgba(250,246,236,.3);text-align:center}}
.btn-lb-save{{
  display:inline-flex;align-items:center;justify-content:center;gap:10px;
  background:var(--gold);color:var(--ink);
  border:none;padding:18px 24px;width:100%;
  font-family:'Inter Tight',sans-serif;font-size:17px;font-weight:600;
  cursor:pointer;text-decoration:none;letter-spacing:.01em;
}}
.btn-lb-save:active{{background:#b8945a}}

/* ── Footer ── */
.footer{{
  text-align:center;padding:28px 16px;
  font-family:'JetBrains Mono',monospace;
  font-size:10px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--ink-faint);border-top:1px solid var(--rule);
  background:var(--paper);
}}
</style>
</head>
<body>

<div class="header">
  <div class="header-brand">QAMRA</div>
  <div class="header-event">{event_name}</div>
  <div class="header-count">{len(file_ids)} صورة وجدناها لك</div>
</div>

<div class="action-bar">
  <span class="action-hint">اضغط على أي صورة للمعاينة والحفظ</span>
  <a class="btn-save-all" href="{zip_url}" download="قمرة-صورك.zip">
    ↓ حفظ الكل ({len(file_ids)})
  </a>
</div>

<div class="gallery" id="gallery"></div>

<div class="lightbox" id="lightbox">
  <button class="lightbox-close" onclick="closeLb()">✕</button>
  <div class="lightbox-img-wrap"><img id="lb-img" src="" alt=""></div>
  <div class="lightbox-footer">
    <a class="btn-lb-save" id="lb-save" href="#" target="_blank" rel="noopener">
      ↓ حفظ الصورة بجودة HD
    </a>
    <div class="lb-hint">جودة أصلية · لا يوجد ضغط</div>
  </div>
</div>

<div class="footer">Powered by QAMRA · صورك بجودتها الأصلية</div>

<script>
const photos    = {photos_js};
const APP_URL   = "{APP_URL}";
const TOKEN     = "{token}";
const EVENT_CODE = "{event_code}";

function thumbUrl(id) {{
  return APP_URL + '/photo/' + id;
}}
function lbUrl(id) {{
  return 'https://drive.google.com/thumbnail?id=' + id + '&sz=w1920';
}}
function dlUrl(id) {{
  return 'https://drive.google.com/uc?export=download&id=' + id;
}}
function viewUrl(id) {{
  return APP_URL + '/photo/' + id + '/view';
}}

async function sendToWa() {{
  const btn = document.getElementById('btn-send-wa');
  btn.disabled = true;
  btn.textContent = '⏳ جاري الإرسال...';
  try {{
    const r = await fetch(APP_URL + '/gallery/' + EVENT_CODE + '/g/' + TOKEN + '/send-to-wa', {{method: 'POST'}});
    const d = await r.json();
    if (r.ok) {{
      btn.textContent = '✅ تم! تحقق من واتساب';
    }} else {{
      btn.disabled = false;
      btn.textContent = '📨 أرسل لي الصور على واتساب';
      alert(d.error || 'حصل خطأ، حاول مرة ثانية');
    }}
  }} catch(e) {{
    btn.disabled = false;
    btn.textContent = '📨 أرسل لي الصور على واتساب';
  }}
}}

const gallery = document.getElementById('gallery');
photos.forEach((p, i) => {{
  const card = document.createElement('div');
  card.className = 'photo-card';
  card.innerHTML = `
    <div class="skeleton" id="sk${{i}}"></div>
    <img src="${{thumbUrl(p.id)}}" alt="" loading="lazy"
         onload="var s=document.getElementById('sk${{i}}');if(s)s.remove()"
         onerror="var s=document.getElementById('sk${{i}}');if(s)s.style.opacity='.2'">
    <div class="overlay">
      <a class="btn-save" href="${{dlUrl(p.id)}}" target="_blank" rel="noopener"
         onclick="event.stopPropagation()">↓ حفظ</a>
    </div>`;
  card.addEventListener('click', () => openLb(i));
  gallery.appendChild(card);
}});

function openLb(i) {{
  const p = photos[i];
  document.getElementById('lb-img').src = lbUrl(p.id);
  const s = document.getElementById('lb-save');
  s.href = dlUrl(p.id);
  document.getElementById('lightbox').classList.add('open');
}}
function closeLb() {{
  document.getElementById('lightbox').classList.remove('open');
}}
document.getElementById('lightbox').addEventListener('click', e => {{
  if (e.target === document.getElementById('lightbox')) closeLb();
}});
document.addEventListener('keydown', e => {{ if (e.key==='Escape') closeLb(); }});

</script>
</body>
</html>"""

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/f/<code>", methods=["GET"])
def short_link_redirect(code):
    url = _short_links.get(code)
    if not url:
        return "رابط غير صالح", 404
    from flask import redirect
    return redirect(url, code=302)

@app.route("/gallery/<event_code>/g/<token>", methods=["GET"])
def gallery_view(event_code, token):
    data = load_gallery_token(token)
    if not data:
        return """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>قمرة — رابط منتهي</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#F2EDE3;color:#1A1612;font-family:'Inter Tight',-apple-system,sans-serif;
display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;padding:24px;text-align:center}}
.brand{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.22em;text-transform:uppercase;
color:rgba(26,22,18,.4);margin-bottom:32px}}
h1{{font-family:'Instrument Serif',serif;font-size:32px;font-weight:400;font-style:italic;margin-bottom:12px}}
p{{font-size:14px;color:rgba(26,22,18,.55);line-height:1.6}}
</style>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@1&family=JetBrains+Mono&display=swap" rel="stylesheet">
</head><body>
<div class="brand">QAMRA</div>
<h1>انتهت صلاحية الرابط</h1>
<p>هذا الرابط لم يعد صالحاً.<br>تواصل مع المصور للحصول على رابط جديد.</p>
</body></html>""", 410, {"Content-Type": "text/html; charset=utf-8"}

    state    = load_state(data["event_code"])
    file_map = state.get("file_map", {})
    event    = get_event(data["event_code"]) or {}
    html     = _gallery_html(
        event_name=event.get("name", data["event_code"]),
        token=token,
        event_code=data["event_code"],
        file_ids=data["file_ids"],
        file_map=file_map,
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/gallery/<event_code>/g/<token>/zip", methods=["GET"])
def gallery_zip(event_code, token):
    from flask import stream_with_context, Response
    data = load_gallery_token(token)
    if not data:
        return "رابط غير صالح أو منتهي", 410

    state    = load_state(data["event_code"])
    file_map = state.get("file_map", {})
    file_ids = data["file_ids"]

    def generate():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for fid in file_ids:
                try:
                    raw  = download_file(fid)
                    name = file_map.get(fid, {}).get("name", f"{fid}.jpg")
                    zf.writestr(name, raw)
                except Exception as e:
                    print(f"[ZIP] skip {fid}: {e}", flush=True)
        buf.seek(0)
        while True:
            chunk = buf.read(65536)
            if not chunk:
                break
            yield chunk

    event = get_event(data["event_code"]) or {}
    phone = data.get("phone", "").strip().replace("+", "").replace(" ", "")
    name_part = event.get("name", event_code)
    fname = (f"قمرة-{name_part}-{phone}.zip" if phone else f"قمرة-{name_part}.zip").replace("/", "-")
    return Response(
        stream_with_context(generate()),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{requests.utils.quote(fname)}"},
    )


@app.route("/gallery/<event_code>/g/<token>/send-to-wa", methods=["POST"])
def gallery_send_to_wa(event_code, token):
    data = load_gallery_token(token)
    if not data:
        return jsonify({"error": "رابط غير صالح أو منتهي"}), 410

    phone    = data.get("phone", "").strip()
    file_ids = data.get("file_ids", [])
    if not phone:
        return jsonify({"error": "لا يوجد رقم هاتف مرتبط بهذا المعرض"}), 400
    if not file_ids:
        return jsonify({"error": "لا يوجد صور"}), 400

    now = int(time.time())
    if now - data.get("last_wa_send", 0) < 600:
        return jsonify({"error": "يرجى الانتظار 10 دقائق قبل الطلب مجدداً"}), 429

    data["last_wa_send"] = now
    try:
        _session.put(
            f"{FACE_SERVICE_URL}/v1/kv/gallery_{token}",
            json={"value": json.dumps(data, ensure_ascii=False)},
            headers=_face_hdrs(), timeout=10,
        )
    except Exception as e:
        print(f"[SEND_TO_WA] Token update error: {e}", flush=True)

    ev_name = (get_event(event_code.upper()) or {}).get("name", event_code)

    def _send():
        send_msg(f"+{phone}",
            f"📲 إليك صورك من *{ev_name}* 🌙\n"
            "تنبيه: الصور المرسلة هنا ستكون بجودة أقل من الأصلية. للجودة الكاملة افتح معرضك الشخصي واضغط 'حفظ الكل'."
        )
        for fid in file_ids:
            send_msg(f"+{phone}", " ", media_url=f"{APP_URL}/photo/{fid}")
            time.sleep(0.8)

    threading.Thread(target=_send, daemon=True).start()
    return jsonify({"status": "sending", "count": len(file_ids)}), 200


@app.route("/gallery/<event_code>/count", methods=["GET"])
def gallery_count(event_code):
    state = load_state(event_code.upper())
    return jsonify({"count": len(state.get("indexed_ids", []))}), 200


@app.route("/gallery/<event_code>/all", methods=["GET"])
def gallery_all(event_code):
    event = get_event(event_code.upper())
    if not event:
        return "حفل غير موجود", 404
    state    = load_state(event_code.upper())
    file_map = state.get("file_map", {})
    all_ids  = list(reversed(list(file_map.keys())))
    total    = len(all_ids)
    page     = int(request.args.get("page", 0))
    per_page = 50
    page_ids = all_ids[page * per_page:(page + 1) * per_page]
    has_more = (page + 1) * per_page < total

    photos_js  = json.dumps([{"id": fid} for fid in page_ids], ensure_ascii=False)
    all_ids_js = json.dumps(all_ids, ensure_ascii=False)
    wa_number  = OWNER_PHONE
    wa_link    = f"https://wa.me/{wa_number}"

    html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>قمرة — كل صور {event['name']}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter+Tight:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#F2EDE3;--bg-alt:#E8E1D2;--paper:#FAF6EC;
  --ink:#1A1612;--ink-soft:#4A413A;--ink-mute:rgba(26,22,18,.50);
  --ink-faint:rgba(26,22,18,.18);--rule:rgba(26,22,18,.14);
  --gold:#C9A96E;
}}
html,body{{min-height:100%;background:var(--bg);color:var(--ink);
  font-family:'Inter Tight',-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased;direction:rtl}}
.header{{background:var(--paper);border-bottom:1px solid var(--rule);padding:20px 20px 18px;text-align:center;position:relative}}
.header-brand{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--ink-mute);margin-bottom:10px}}
.header-event{{font-family:'Inter Tight',-apple-system,sans-serif;font-size:clamp(18px,5vw,28px);font-weight:700;font-style:normal;color:var(--ink);margin-bottom:6px;letter-spacing:-.01em}}
.header-count{{display:inline-flex;align-items:center;gap:8px;font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--gold);margin-top:4px}}
.header-count::before,.header-count::after{{content:"";display:block;width:16px;height:1px;background:var(--gold);opacity:.6}}
.btn-back{{display:inline-flex;align-items:center;gap:6px;background:transparent;color:var(--ink-mute);border:1px solid var(--rule);padding:7px 14px;font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.08em;text-transform:uppercase;text-decoration:none;transition:background .15s;margin-bottom:14px}}
.btn-back:active{{background:var(--bg-alt)}}
.gallery{{display:grid;grid-template-columns:1fr 1fr;gap:3px;padding:3px;background:var(--bg-alt)}}
.photo-card{{position:relative;aspect-ratio:1/1;overflow:hidden;background:var(--bg-alt);cursor:pointer}}
.photo-card img{{width:100%;height:100%;object-fit:cover;display:block;transition:transform .3s}}
.photo-card:active img{{transform:scale(1.04)}}
.skeleton{{position:absolute;inset:0;background:linear-gradient(90deg,var(--bg-alt) 25%,var(--rule) 50%,var(--bg-alt) 75%);background-size:200% 100%;animation:shimmer 1.4s infinite}}
@keyframes shimmer{{0%{{background-position:200% 0}}100%{{background-position:-200% 0}}}}
.overlay{{position:absolute;inset:0;background:linear-gradient(to top,rgba(26,22,18,.68) 0%,transparent 48%);opacity:0;transition:opacity .2s;display:flex;align-items:flex-end;justify-content:flex-end;padding:10px}}
.photo-card:hover .overlay{{opacity:1}}
@media(max-width:768px){{.overlay{{opacity:1;background:linear-gradient(to top,rgba(26,22,18,.52) 0%,transparent 44%)}}}}
.btn-save{{display:inline-flex;align-items:center;gap:5px;background:var(--paper);color:var(--ink);border:none;padding:6px 11px;font-family:'Inter Tight',sans-serif;font-size:12px;font-weight:500;cursor:pointer;text-decoration:none;white-space:nowrap}}
.load-more-wrap{{text-align:center;padding:32px 16px;background:var(--bg)}}
.btn-load-more{{display:inline-flex;align-items:center;gap:12px;background:var(--ink);color:var(--bg);border:none;padding:14px 32px;font-family:'Inter Tight',sans-serif;font-size:14px;font-weight:500;cursor:pointer;transition:background .15s;letter-spacing:.02em}}
.btn-load-more::after{{content:"";display:block;width:14px;height:1px;background:currentColor}}
.btn-load-more:active{{background:var(--ink-soft)}}
.btn-load-more:disabled{{opacity:.4;cursor:not-allowed}}
.lightbox{{display:none;position:fixed;inset:0;background:rgba(26,22,18,.97);z-index:1000;flex-direction:column;align-items:center;justify-content:center;padding:16px}}
.lightbox.open{{display:flex}}
.lightbox-img-wrap{{display:flex;align-items:center;justify-content:center;flex:1;width:100%}}
.lightbox img{{max-width:95vw;max-height:80vh;object-fit:contain}}
.lightbox-close{{position:absolute;top:14px;right:14px;background:rgba(250,246,236,.1);border:none;color:var(--paper);width:38px;height:38px;border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:18px}}
.lb-nav{{position:absolute;top:50%;transform:translateY(-50%);background:rgba(250,246,236,.1);border:none;color:var(--paper);width:44px;height:44px;border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:22px}}
.lb-nav.prev{{left:10px}}.lb-nav.next{{right:10px}}
.lb-counter{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.12em;color:rgba(250,246,236,.4);position:absolute;bottom:72px}}
.lightbox-footer{{display:flex;align-items:center;gap:14px;padding-top:16px}}
.btn-lb-save{{display:inline-flex;align-items:center;gap:8px;background:var(--gold);color:var(--ink);border:none;padding:11px 24px;font-family:'Inter Tight',sans-serif;font-size:14px;font-weight:500;cursor:pointer;text-decoration:none}}
.footer{{text-align:center;padding:28px 16px;font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--ink-faint);border-top:1px solid var(--rule);background:var(--paper)}}
.new-banner{{
  display:none;position:sticky;top:0;z-index:100;
  background:var(--ink);color:var(--bg);
  padding:14px 20px;text-align:center;cursor:pointer;
  font-family:'Inter Tight',sans-serif;font-size:14px;font-weight:500;
  letter-spacing:.02em;border-bottom:2px solid var(--gold);
  animation:slide-down .3s ease;
}}
@keyframes slide-down{{from{{transform:translateY(-100%)}}to{{transform:translateY(0)}}}}
.new-banner:active{{background:var(--ink-soft)}}
</style>
</head>
<body>
<div class="new-banner" id="new-banner" onclick="location.reload()"></div>
<div class="header">
  <div class="header-brand">QAMRA</div>
  <a class="btn-back" href="/event/{event_code.upper()}/landing">← رجوع</a>
  <div class="header-event">{event['name']}</div>
  <div class="header-count" id="count-label">{total} صورة</div>
</div>
<div class="gallery" id="gallery"></div>
{'<div class="load-more-wrap"><button class="btn-load-more" id="btn-more" onclick="loadMore()">تحميل المزيد</button></div>' if has_more else ''}
<div class="lightbox" id="lightbox">
  <button class="lightbox-close" onclick="closeLb()">✕</button>
  <button class="lb-nav prev" onclick="navLb(-1)">‹</button>
  <button class="lb-nav next" onclick="navLb(1)">›</button>
  <div class="lightbox-img-wrap"><img id="lb-img" src="" alt=""></div>
  <div class="lb-counter" id="lb-counter"></div>
  <div class="lightbox-footer">
    <a class="btn-lb-save" id="lb-save" href="#" target="_blank" rel="noopener">↓ حفظ الصورة الأصلية</a>
  </div>
</div>
<div class="footer">Powered by QAMRA · صورك بجودتها الأصلية</div>
<script>
const APP_URL = "{APP_URL}";
const EVENT_CODE = "{event_code.upper()}";
const PER_PAGE = {per_page};
let currentPage = {page};
let allIds = {all_ids_js};
let loadedIds = {photos_js}.map(p => p.id);

function thumbUrl(id) {{ return APP_URL + '/photo/' + id; }}
function dlUrl(id) {{ return 'https://drive.google.com/uc?export=download&id=' + id; }}
function viewUrl(id) {{ return APP_URL + '/photo/' + id + '/view'; }}

const gallery = document.getElementById('gallery');

let currentLbIdx = 0;

function addPhotos(ids) {{
  ids.forEach((id, i) => {{
    const idx = gallery.children.length;
    if (idx >= loadedIds.length) loadedIds.push(id);
    const card = document.createElement('div');
    card.className = 'photo-card';
    card.dataset.idx = idx;
    card.innerHTML = `
      <div class="skeleton" id="sk${{idx}}"></div>
      <img src="${{thumbUrl(id)}}" alt="" loading="lazy"
           onload="var s=document.getElementById('sk${{idx}}');if(s)s.remove()"
           onerror="var s=document.getElementById('sk${{idx}}');if(s)s.style.opacity='.2'">
      <div class="overlay">
        <a class="btn-save" href="${{dlUrl(id)}}" target="_blank" rel="noopener"
           onclick="event.stopPropagation()">↓ حفظ</a>
      </div>`;
    card.addEventListener('click', () => openLb(idx));
    gallery.appendChild(card);
  }});
}}

function openLb(idx) {{
  currentLbIdx = idx;
  updateLb();
  document.getElementById('lightbox').classList.add('open');
}}
function updateLb() {{
  const id = loadedIds[currentLbIdx];
  document.getElementById('lb-img').src = thumbUrl(id);
  document.getElementById('lb-save').href = dlUrl(id);
  document.getElementById('lb-counter').textContent = (currentLbIdx + 1) + ' / ' + loadedIds.length;
}}
function navLb(dir) {{
  currentLbIdx = (currentLbIdx + dir + loadedIds.length) % loadedIds.length;
  updateLb();
}}
function closeLb() {{ document.getElementById('lightbox').classList.remove('open'); }}
document.getElementById('lightbox').addEventListener('click', e => {{
  if (e.target === document.getElementById('lightbox')) closeLb();
}});
document.addEventListener('keydown', e => {{
  if (e.key==='Escape') closeLb();
  if (e.key==='ArrowLeft') navLb(1);
  if (e.key==='ArrowRight') navLb(-1);
}});
let _tx=null;
document.getElementById('lightbox').addEventListener('touchstart',e=>{{_tx=e.touches[0].clientX;}},{{passive:true}});
document.getElementById('lightbox').addEventListener('touchend',e=>{{
  if(_tx===null)return;const dx=e.changedTouches[0].clientX-_tx;
  if(Math.abs(dx)>40)navLb(dx>0?1:-1);_tx=null;
}});

async function loadMore() {{
  const btn = document.getElementById('btn-more');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'جاري التحميل...';
  currentPage++;
  const start = currentPage * PER_PAGE;
  const slice = allIds.slice(start, start + PER_PAGE);
  addPhotos(slice);
  if ((currentPage + 1) * PER_PAGE >= allIds.length) {{
    btn.parentElement.remove();
  }} else {{
    btn.disabled = false;
    btn.textContent = 'تحميل المزيد';
  }}
}}

addPhotos(loadedIds);

// Poll for new photos every 30 seconds
let knownCount = {total};
setInterval(async () => {{
  try {{
    const r = await fetch(APP_URL + '/gallery/' + EVENT_CODE + '/count');
    const d = await r.json();
    if (d.count > knownCount) {{
      const diff = d.count - knownCount;
      const banner = document.getElementById('new-banner');
      banner.textContent = diff + ' صور جديدة متاحة — اضغط لعرضها';
      banner.style.display = 'block';
    }}
  }} catch(_) {{}}
}}, 30000);
</script>
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/event/<code>/selfie-search", methods=["POST"])
def event_selfie_search(code):
    event = get_event(code.upper())
    if not event:
        return jsonify({"error": "حفل غير موجود"}), 404

    f = request.files.get("photo")
    if not f:
        return jsonify({"error": "لم يتم إرسال صورة"}), 400

    selfie_bytes = f.read()

    face_count = count_faces(selfie_bytes)
    if face_count > 1:
        return jsonify({"error": "الصورة تحتوي على أكثر من وجه — أرسل سيلفي لوجهك منفرداً"}), 400

    matches = search_by_selfie(selfie_bytes, event["collection_id"])
    if not matches:
        return jsonify({"error": "ما لقينا وجهك — حاول مرة ثانية بسيلفي أوضح"}), 404

    state    = load_state(code.upper())
    face_map = state.get("face_map", {})
    seen, file_ids = set(), []
    for m in matches:
        fid = face_map.get(m["persistedFaceId"])
        if fid and fid not in seen:
            seen.add(fid)
            file_ids.append(fid)

    if not file_ids:
        return jsonify({"error": "ما لقينا صورك — حاول مرة ثانية"}), 404

    token        = create_gallery_token(code.upper(), file_ids)
    gallery_url  = f"{APP_URL}/gallery/{code.upper()}/g/{token}"

    threading.Thread(
        target=_register_guest,
        args=(code.upper(), "", selfie_bytes, file_ids, token),
        daemon=True,
    ).start()

    return jsonify({"gallery_url": gallery_url, "count": len(file_ids)}), 200


@app.route("/event/<code>/selfie", methods=["GET"])
def event_selfie_page(code):
    event = get_event(code.upper())
    if not event:
        return "حفل غير موجود", 404

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>قمرة — ابحث عن صورتك</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@1&family=Inter+Tight:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
@keyframes spin{{to{{transform:rotate(360deg)}}}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
@keyframes countPop{{0%{{transform:scale(1.6);opacity:0}}40%{{opacity:1}}100%{{transform:scale(1);opacity:1}}}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#F2EDE3;--paper:#FAF6EC;--ink:#1A1612;
  --ink-mute:rgba(26,22,18,.50);--ink-faint:rgba(26,22,18,.18);
  --rule:rgba(26,22,18,.14);--gold:#C9A96E;
}}
html,body{{min-height:100vh;background:var(--bg);color:var(--ink);
  font-family:'Inter Tight',-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased;direction:rtl;
  display:flex;flex-direction:column;align-items:center;
  padding:28px 20px;}}
.brand{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--ink-mute);margin-bottom:12px;text-align:center}}
.event-name{{font-family:'Inter Tight',-apple-system,sans-serif;font-size:clamp(18px,5vw,28px);font-weight:700;font-style:normal;text-align:center;margin-bottom:24px;letter-spacing:-.01em}}
.cam-wrap{{
  width:100%;max-width:380px;
  position:relative;overflow:hidden;
  background:#000;aspect-ratio:3/4;
  margin-bottom:20px;
}}
#video{{width:100%;height:100%;object-fit:cover;display:block;transform:scaleX(-1)}}
#canvas{{display:none}}

/* Oval face guide */
.oval-overlay{{
  position:absolute;inset:0;pointer-events:none;
}}
.oval-hole{{
  position:absolute;
  top:50%;left:50%;
  width:72%;padding-bottom:88%;
  transform:translate(-50%,-54%);
  border-radius:50%;
  box-shadow:0 0 0 200px rgba(0,0,0,0.52);
  border:2.5px solid rgba(255,255,255,0.55);
}}
.oval-hint{{
  position:absolute;bottom:14px;left:0;right:0;
  font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.08em;
  color:rgba(255,255,255,.65);text-align:center;
}}

/* Countdown */
.countdown{{
  position:absolute;inset:0;display:none;
  align-items:center;justify-content:center;
  pointer-events:none;
}}
.countdown.show{{display:flex}}
.countdown-num{{
  font-family:'Instrument Serif',serif;font-size:120px;font-weight:400;
  color:#fff;text-shadow:0 2px 24px rgba(0,0,0,.6);
  animation:countPop .55s ease-out forwards;
}}

/* Loading screen */
.loading-screen{{
  display:none;position:fixed;inset:0;z-index:999;
  background:var(--bg);flex-direction:column;
  align-items:center;justify-content:center;gap:24px;
}}
.loading-screen.show{{display:flex}}
.loading-spinner{{
  width:52px;height:52px;border-radius:50%;
  border:3px solid var(--rule);
  border-top-color:var(--gold);
  animation:spin .9s linear infinite;
}}
.loading-title{{
  font-family:'Instrument Serif',serif;font-size:26px;font-weight:400;font-style:italic;
  color:var(--ink);animation:pulse 1.6s ease-in-out infinite;
}}
.loading-sub{{
  font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-mute);
}}
.loading-err{{
  font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.06em;
  color:#c0392b;text-align:center;padding:0 24px;display:none;
}}

.btn-capture{{
  width:100%;max-width:380px;
  background:var(--ink);color:var(--bg);
  border:none;padding:18px;
  font-family:'Inter Tight',sans-serif;font-size:17px;font-weight:600;
  cursor:pointer;letter-spacing:.01em;
  transition:background .15s;margin-bottom:12px;
}}
.btn-capture:active{{background:#4A413A}}
.btn-capture:disabled{{opacity:.45;cursor:not-allowed}}
.btn-back{{
  width:100%;max-width:380px;
  background:transparent;color:var(--ink-mute);
  border:1px solid var(--rule);padding:12px;
  font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  cursor:pointer;text-decoration:none;display:block;text-align:center;
}}
.cam-error{{
  width:100%;max-width:380px;aspect-ratio:3/4;
  background:var(--paper);border:1px solid var(--rule);
  display:none;align-items:center;justify-content:center;
  flex-direction:column;gap:12px;text-align:center;padding:24px;
  margin-bottom:20px;
}}
.cam-error p{{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--ink-mute);letter-spacing:.06em}}
</style>
</head>
<body>
<div class="brand">QAMRA</div>
<div class="event-name">{event['name']}</div>

<div class="cam-wrap" id="cam-wrap">
  <video id="video" autoplay playsinline muted></video>
  <div class="oval-overlay">
    <div class="oval-hole" id="oval"></div>
    <div class="oval-hint">ضع وجهك داخل الإطار</div>
  </div>
  <div class="countdown" id="countdown">
    <span class="countdown-num" id="countdown-num">3</span>
  </div>
</div>
<div class="cam-error" id="cam-error">
  <span style="font-size:36px">📷</span>
  <p>تعذّر الوصول للكاميرا<br>تأكد من السماح بالوصول وأعد المحاولة</p>
</div>

<canvas id="canvas"></canvas>
<button class="btn-capture" id="btn-capture" onclick="startCountdown()">📸 التقط سيلفي وابحث</button>
<a class="btn-back" id="btn-back" href="/event/{code.upper()}/landing">← رجوع</a>

<!-- Full-screen loading -->
<div class="loading-screen" id="loading">
  <div class="loading-spinner"></div>
  <div class="loading-title">جاري البحث عن صورك</div>
  <div class="loading-sub">ثوانٍ فقط 🌙</div>
  <div class="loading-err" id="loading-err"></div>
  <button class="btn-capture" id="btn-retry" style="display:none;margin-top:12px" onclick="retryFromLoading()">حاول مرة ثانية</button>
</div>

<script>
const VIDEO    = document.getElementById('video');
const CANVAS   = document.getElementById('canvas');
const BTN      = document.getElementById('btn-capture');
const CAMWRAP  = document.getElementById('cam-wrap');
const CAMERR   = document.getElementById('cam-error');
const CDOWN    = document.getElementById('countdown');
const CDNUM    = document.getElementById('countdown-num');
const LOADING  = document.getElementById('loading');
const LERR     = document.getElementById('loading-err');
const BTNRETRY = document.getElementById('btn-retry');

async function startCamera() {{
  try {{
    const stream = await navigator.mediaDevices.getUserMedia({{
      video: {{ facingMode: 'user', width: {{ ideal: 1280 }}, height: {{ ideal: 960 }} }},
      audio: false,
    }});
    VIDEO.srcObject = stream;
  }} catch(e) {{
    CAMWRAP.style.display = 'none';
    CAMERR.style.display  = 'flex';
    BTN.disabled = true;
  }}
}}

function startCountdown() {{
  BTN.disabled = true;
  document.getElementById('btn-back').style.display = 'none';
  let n = 3;
  CDNUM.textContent = n;
  CDOWN.classList.add('show');

  const tick = () => {{
    CDNUM.style.animation = 'none';
    void CDNUM.offsetWidth;
    CDNUM.style.animation = 'countPop .55s ease-out forwards';
    CDNUM.textContent = n;
    if (n === 0) {{
      CDOWN.classList.remove('show');
      capture();
      return;
    }}
    n--;
    setTimeout(tick, 900);
  }};
  tick();
}}

function capture() {{
  CANVAS.width  = VIDEO.videoWidth  || 1280;
  CANVAS.height = VIDEO.videoHeight || 960;
  const ctx = CANVAS.getContext('2d');
  ctx.translate(CANVAS.width, 0);
  ctx.scale(-1, 1);
  ctx.drawImage(VIDEO, 0, 0);

  LOADING.classList.add('show');
  LERR.style.display  = 'none';
  BTNRETRY.style.display = 'none';

  CANVAS.toBlob(async blob => {{
    const fd = new FormData();
    fd.append('photo', blob, 'selfie.jpg');
    try {{
      const r = await fetch('/event/{code.upper()}/selfie-search', {{ method:'POST', body: fd }});
      const d = await r.json();
      if (r.ok) {{
        window.location.href = d.gallery_url;
      }} else {{
        showError(d.error || 'حصل خطأ، حاول مرة ثانية');
      }}
    }} catch(e) {{
      showError('خطأ في الاتصال — تحقق من الإنترنت وحاول مرة ثانية');
    }}
  }}, 'image/jpeg', 0.92);
}}

function showError(msg) {{
  LERR.textContent   = msg;
  LERR.style.display = 'block';
  BTNRETRY.style.display = 'block';
}}

function retryFromLoading() {{
  LOADING.classList.remove('show');
  BTN.disabled = false;
  document.getElementById('btn-back').style.display = 'block';
}}

startCamera();
</script>
</body></html>""", 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/event/<code>/landing", methods=["GET"])
def event_landing_page(code):
    event = get_event(code.upper())
    if not event:
        return "حفل غير موجود", 404

    gallery_url = f"{APP_URL}/gallery/{code.upper()}/all"
    selfie_url  = f"/event/{code.upper()}/selfie"
    wa_msg      = requests.utils.quote(f"مرحباً! أريد البحث عن صوري من {event['name']} 📸")
    wa_link     = f"https://wa.me/{OWNER_PHONE}?text={wa_msg}"
    landing_url = f"{APP_URL}/event/{code.upper()}/landing"

    html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>قمرة — {event['name']}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter+Tight:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#F2EDE3;--bg-alt:#E8E1D2;--paper:#FAF6EC;
  --ink:#1A1612;--ink-soft:#4A413A;--ink-mute:rgba(26,22,18,.50);
  --ink-faint:rgba(26,22,18,.18);--rule:rgba(26,22,18,.14);
  --gold:#C9A96E;--gold-soft:rgba(201,169,110,.12);
}}
html,body{{min-height:100vh;background:var(--bg);color:var(--ink);
  font-family:'Inter Tight',-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased;direction:rtl;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:32px 20px;}}
.wrap{{width:100%;max-width:420px;display:flex;flex-direction:column;align-items:center;gap:0}}
.brand{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--ink-mute);margin-bottom:16px}}
.event-name{{font-family:'Inter Tight',-apple-system,sans-serif;font-size:clamp(22px,6vw,34px);font-weight:700;font-style:normal;color:var(--ink);text-align:center;margin-bottom:6px;line-height:1.2;letter-spacing:-.01em}}
.rule{{width:40px;height:1px;background:var(--gold);margin:20px auto 28px}}
.cards{{width:100%;display:flex;flex-direction:column;gap:3px;margin-bottom:32px}}
.card{{
  display:flex;align-items:center;gap:20px;
  background:var(--paper);border:1px solid var(--rule);
  padding:22px 20px;text-decoration:none;color:var(--ink);
  transition:background .15s;
}}
.card:active{{background:var(--bg-alt)}}
.card.gold{{background:var(--gold-soft);border-color:rgba(201,169,110,.35)}}
.card-icon{{font-size:32px;flex-shrink:0;line-height:1}}
.card-body{{flex:1}}
.card-title{{font-size:19px;font-weight:600;margin-bottom:4px}}
.card-sub{{font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.06em;color:var(--ink-mute)}}
.card-arrow{{font-size:18px;color:var(--ink-mute);flex-shrink:0}}
.badge{{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;padding:3px 8px;margin-bottom:6px}}
.badge-rec{{background:var(--ink);color:#fff}}
.badge-new{{color:#25D366;border:1px solid rgba(37,211,102,.4)}}
.qr-section{{
  background:var(--paper);border:1px solid var(--rule);
  padding:24px 20px;width:100%;text-align:center;
}}
.qr-label{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-mute);margin-bottom:16px}}
#qr-box{{display:inline-block}}
#qr-box canvas,#qr-box img{{width:180px!important;height:180px!important}}
.qr-hint{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.08em;color:var(--ink-faint);margin-top:12px}}
.footer{{margin-top:28px;font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--ink-faint)}}
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">QAMRA</div>
  <div class="event-name">{event['name']}</div>
  <div class="rule"></div>

  <div class="cards">
    <a href="{gallery_url}" class="card gold">
      <span class="card-icon">🖼️</span>
      <div class="card-body">
        <div class="card-title">تصفح كل الصور</div>
        <div class="card-sub">معرض الحفل كاملاً · جودة أصلية</div>
      </div>
      <span class="card-arrow">←</span>
    </a>
    <a href="{selfie_url}" class="card">
      <span class="card-icon">📷</span>
      <div class="card-body">
        <div class="badge badge-rec">★ الأفضل · موصى به</div>
        <div class="card-title">ابحث عن صورتك من هنا</div>
        <div class="card-sub">افتح الكاميرا · التقط سيلفي · شاهد صورك فوراً</div>
      </div>
      <span class="card-arrow">←</span>
    </a>
    <a href="{wa_link}" target="_blank" class="card">
      <span class="card-icon">💬</span>
      <div class="card-body">
        <div class="badge badge-new">✦ جديد</div>
        <div class="card-title">ابحث عن صورتك عبر بوت واتساب</div>
        <div class="card-sub">أرسل سيلفي على واتساب · احصل على رابط معرضك</div>
      </div>
      <span class="card-arrow">←</span>
    </a>
  </div>

  <div class="qr-section">
    <div class="qr-label">شارك هذه الصفحة</div>
    <div id="qr-box"></div>
    <div class="qr-hint">امسح الرمز لفتح هذه الصفحة</div>
  </div>

  <div class="footer">Powered by QAMRA</div>
</div>
<script>
new QRCode(document.getElementById('qr-box'), {{
  text: "{landing_url}",
  width: 180, height: 180,
  colorDark: "#1A1612", colorLight: "#FAF6EC",
  correctLevel: QRCode.CorrectLevel.M,
}});
</script>
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/", methods=["GET"])
def health():
    # Use only in-memory cache — never make VPS calls here (Railway health checker pings this often)
    summary = {code: len(_state_cache.get(code, {}).get("indexed_ids", [])) for code in _events}
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

@app.route("/photo/<file_id>/view", methods=["GET"])
def photo_view(file_id):
    if not all(c.isalnum() or c in "-_" for c in file_id):
        return "invalid id", 400
    thumb = f"{APP_URL}/photo/{file_id}"
    dl    = f"https://drive.google.com/uc?export=download&id={file_id}"
    html  = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>قمرة — عرض الصورة</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#F2EDE3;--paper:#FAF6EC;--ink:#1A1612;--ink-mute:rgba(26,22,18,.50);--ink-faint:rgba(26,22,18,.18);--rule:rgba(26,22,18,.14);--gold:#C9A96E}}
html,body{{min-height:100vh;background:#1A1612;color:var(--ink);font-family:'Inter Tight',-apple-system,sans-serif;-webkit-font-smoothing:antialiased;direction:rtl;display:flex;flex-direction:column}}
.topbar{{background:rgba(26,22,18,.7);backdrop-filter:blur(8px);padding:12px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10;border-bottom:1px solid rgba(250,246,236,.08)}}
.brand{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:rgba(250,246,236,.45)}}
.btn-back{{display:inline-flex;align-items:center;gap:6px;background:transparent;color:rgba(250,246,236,.6);border:1px solid rgba(250,246,236,.15);padding:7px 14px;font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.08em;text-transform:uppercase;text-decoration:none;cursor:pointer}}
.photo-wrap{{flex:1;display:flex;align-items:center;justify-content:center;padding:24px 16px}}
.photo-wrap img{{max-width:100%;max-height:80vh;object-fit:contain;display:block}}
.action-bar{{background:rgba(26,22,18,.85);backdrop-filter:blur(8px);padding:16px 20px;display:flex;align-items:center;justify-content:center;gap:12px;border-top:1px solid rgba(250,246,236,.08);position:sticky;bottom:0}}
.btn-dl{{display:inline-flex;align-items:center;gap:10px;background:var(--gold);color:var(--ink);border:none;padding:14px 32px;font-family:'Inter Tight',sans-serif;font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;letter-spacing:.01em}}
.btn-dl:active{{opacity:.85}}
</style>
</head>
<body>
<div class="topbar">
  <button class="btn-back" onclick="history.back()">← رجوع</button>
  <div class="brand">QAMRA</div>
</div>
<div class="photo-wrap">
  <img src="{thumb}" alt="صورة">
</div>
<div class="action-bar">
  <a class="btn-dl" href="{dl}" target="_blank" rel="noopener">↓ تحميل الصورة بجودتها الأصلية</a>
</div>
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/folder-status/<session_id>", methods=["GET"])
def folder_status(session_id):
    # Check in-memory cache first (fast path)
    if session_id in _folder_cache:
        url = _folder_cache[session_id]
        if url is None:
            return jsonify({"status": "building"}), 202
        if url == "":
            return jsonify({"status": "failed"}), 200
        return jsonify({"status": "ready", "folder_url": url}), 200
    # Not in memory — Railway may have restarted; check VPS KV
    try:
        r = _session.get(
            f"{FACE_SERVICE_URL}/v1/kv/folder_{session_id}",
            headers=_face_hdrs(), timeout=5,
        )
        print(f"[FOLDER_STATUS] KV lookup sid={session_id} status={r.status_code}", flush=True)
        if r.status_code == 200:
            url = r.json().get("value", "")
            if url:
                _folder_cache[session_id] = url  # warm local cache
                return jsonify({"status": "ready", "folder_url": url}), 200
    except Exception as kv_err:
        print(f"[FOLDER_STATUS] KV error sid={session_id}: {kv_err}", flush=True)
    return jsonify({"status": "not_found"}), 404

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
    face_map = state.get("face_map", {})
    if not state.get("indexed_ids"):
        return jsonify({"error": "Photos not indexed yet — call /index first"}), 503

    matches = search_by_selfie(selfie_bytes, event["collection_id"])
    if not matches:
        return jsonify({"matches": [], "message": "No face found or no matches"}), 200

    seen, results, file_ids = set(), [], []
    for m in matches:
        file_id = face_map.get(m["persistedFaceId"])
        conf    = round(m["confidence"] * 100, 1)
        if not file_id or file_id in seen:
            continue
        seen.add(file_id)
        file_ids.append(file_id)
        entry = file_map.get(file_id, {})
        results.append({
            "url":        f"{APP_URL}/photo/{file_id}",
            "confidence": conf,
            "name":       entry.get("name", ""),
            "drive_link": entry.get("link", ""),
        })

    session_id = hashlib.md5(f"{time.time()}{phone}".encode()).hexdigest()[:16]

    # Create folder + set permissions synchronously so we can return folder_url immediately.
    # Shortcuts are added in the background — they appear in the folder within seconds.
    folder_url = None
    folder_svc = None
    folder_id  = None
    if file_ids:
        try:
            guest_num  = get_next_guest_number(event_code)
            folder_svc = _drive(write=True)
            _f         = folder_svc.files().create(body={
                "name": f"صورك من {event['name']} 🌙 — ضيف {guest_num}",
                "mimeType": "application/vnd.google-apps.folder",
            }, fields="id").execute()
            folder_id  = _f["id"]
            folder_svc.permissions().create(
                fileId=folder_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()
            folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
            print(f"[FOLDER] Shell created {folder_url}", flush=True)
        except Exception as e:
            print(f"[FOLDER] Shell create error (will retry in background): {e}", flush=True)

    _folder_cache[session_id] = folder_url  # None = still building (shell failed)

    def _build_shortcuts():
        url = folder_url
        try:
            if url and folder_svc and folder_id:
                # Shell succeeded — just add shortcuts
                _add_shortcuts(folder_svc, folder_id, file_ids, file_map)
            elif file_ids:
                # Shell failed — full fallback create
                guest_num2 = get_next_guest_number(event_code)
                url = create_guest_folder(guest_num2, file_ids, event["name"], file_map)
            _folder_cache[session_id] = url or ""
            # Persist to VPS KV so URL survives Railway restarts — retry up to 3x
            if url:
                saved = False
                for _attempt in range(3):
                    try:
                        r_kv = _session.put(
                            f"{FACE_SERVICE_URL}/v1/kv/folder_{session_id}",
                            json={"value": url},
                            headers=_face_hdrs(), timeout=15,
                        )
                        if r_kv.status_code == 200:
                            saved = True
                            break
                        print(f"[FOLDER] KV save attempt {_attempt+1} status={r_kv.status_code}", flush=True)
                    except Exception as kv_e:
                        print(f"[FOLDER] KV save attempt {_attempt+1} error: {kv_e}", flush=True)
                    time.sleep(2)
                print(f"[FOLDER] Done session={session_id} files={len(file_ids)} kv_saved={saved}", flush=True)
        except Exception as e:
            _folder_cache[session_id] = ""
            print(f"[FOLDER] Error session={session_id}: {e}", flush=True)

    threading.Thread(target=_build_shortcuts, daemon=True).start()

    gallery_token = create_gallery_token(event_code, file_ids, phone=phone)
    gallery_url   = f"{APP_URL}/gallery/{event_code}/g/{gallery_token}"

    resp = {"matches": results, "session_id": session_id, "gallery_url": gallery_url}
    if folder_url:
        resp["folder_url"] = folder_url
    return jsonify(resp), 200

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
        landing_url = f"{APP_URL}/event/{code}/landing"
        gallery_url = f"{APP_URL}/gallery/{code}/all"
        events_rows += f"""
        <tr>
          <td><strong>{code}</strong></td>
          <td>{ev['name']}</td>
          <td>{count} صورة</td>
          <td>
            <a href="{landing_url}" target="_blank" style="color:#C9A96E">صفحة الطاولات ↗</a>
            &nbsp;·&nbsp;
            <a href="{gallery_url}" target="_blank" style="color:#aaa;font-size:12px">كل الصور ↗</a>
          </td>
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
      <div><label>Azure FaceList ID</label><input name="collection_id" placeholder="qamra-ahmed2026" required></div>
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
            pageSize=200,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            corpora="allDrives",
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

@app.route("/admin/reset-index/<code>", methods=["POST"])
def admin_reset_index(code):
    if not _check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    code = code.upper()
    if code not in _events:
        return jsonify({"error": "Not found"}), 404
    event = _events[code]
    collection_id = event.get("collection_id", "")
    # Clear VPS face collection so orphan tokens don't accumulate
    try:
        _session.delete(
            f"{FACE_SERVICE_URL}/v1/clear/{collection_id}",
            headers=_face_hdrs(), timeout=10,
        )
    except Exception as e:
        print(f"[RESET] VPS clear error (non-fatal): {e}", flush=True)
    state = load_state(code)
    state["indexed_ids"]  = []
    state["no_face_ids"]  = []
    state["face_map"]     = {}
    state["file_map"]     = {}
    save_state(code, state)
    threading.Thread(target=run_index, args=(code,), daemon=True).start()
    return jsonify({"status": "reset", "event": code, "indexing": "started"}), 200

@app.route("/admin/test-folder", methods=["POST"])
def admin_test_folder():
    """Test Drive folder creation — helps diagnose failures.
    ?limit=N controls how many files to use (default 50).
    """
    if not _check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    event_code = request.args.get("event", "DEFAULT").upper()
    limit = int(request.args.get("limit", "50"))
    event = get_event(event_code)
    if not event:
        return jsonify({"error": f"No event {event_code}"}), 404
    state    = load_state(event_code)
    file_map = state.get("file_map", {})
    file_ids = list(file_map.keys())[:limit]
    if not file_ids:
        return jsonify({"error": "No indexed files yet"}), 503
    try:
        import traceback as _tb, time as _time
        t0 = _time.time()
        guest_num = get_next_guest_number(event_code)
        url = create_guest_folder(guest_num, file_ids, event["name"], file_map)
        elapsed = round(_time.time() - t0, 2)
        return jsonify({"status": "ok", "url": url, "files_used": len(file_ids), "elapsed_s": elapsed}), 200
    except Exception as e:
        return jsonify({"error": str(e), "traceback": _tb.format_exc()}), 500

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

@app.route("/admin/ratings", methods=["GET"])
def admin_ratings():
    if not _check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    all_ratings = {}
    for code in _events:
        key = f"ratings_{code}"
        try:
            r = _session.get(f"{FACE_SERVICE_URL}/v1/kv/{key}", headers=_face_hdrs(), timeout=10)
            all_ratings[code] = json.loads(r.json()["value"]) if r.status_code == 200 else []
        except Exception:
            all_ratings[code] = []
    return jsonify(all_ratings), 200

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

    wa_number   = OWNER_PHONE
    wa_link     = f"https://wa.me/{wa_number}?text={code.upper()}"
    gallery_url = f"{APP_URL}/gallery/{code.upper()}/all"
    kiosk_url   = event.get("kiosk_url", "")
    name        = event["name"]

    cards = f"""
        <a href="{gallery_url}" class="card">
          <div class="icon">🖼️</div>
          <div class="label">شاهد جميع صور الحفل</div>
          <div class="sub">معرض الحفل كاملاً — جودة أصلية</div>
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

    if _is_duplicate_msg(msg_id):
        print(f"[WH] duplicate msg_id={msg_id}, ignoring", flush=True)
        return "", 200

    def _reply(text, murl=None):
        threading.Thread(target=send_msg, args=(sender, text), kwargs={"media_url": murl}, daemon=True).start()
        return "", 200

    def _reply_buttons(body, buttons):
        threading.Thread(target=send_buttons, args=(sender, body, buttons), daemon=True).start()
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
                _agent_sessions[user_phone]["ts"] = time.time()  # renew TTL
                _cancel_inquiry_timer(user_phone)
                _set_conv(user_phone, "with_agent", event_code=_get_conv(user_phone).get("event_code"))
                return "", 200
        return "", 200

    conv  = _get_conv(sender)
    state = conv["state"]

    # ── Selfie received — ACK instantly, download + search in background ─────
    if has_media:
        # Treat any image as a selfie search — skip the menu entirely
        if state not in ("awaiting_selfie", "choosing_event"):
            _set_conv(sender, "awaiting_selfie")

        event_code = conv.get("event_code")
        if not event_code or not get_event(event_code):
            if not _events:
                return _reply("⚠️ ما فيه حفل مسجل. تواصل مع المنظم.")
            elif len(_events) == 1:
                event_code = next(iter(_events))
                _set_conv(sender, "awaiting_selfie", event_code=event_code)
            else:
                all_ev = list(_events.items())
                _set_conv(sender, "choosing_event")
                _conv[sender]["today_events"] = [c for c, _ in all_ev]
                body, btn_labels = _event_picker_body_and_btns(all_ev)
                return _reply_buttons(body, btn_labels)

        _clear_conv(sender)
        _set_conv(sender, "awaiting_selfie", event_code=event_code)
        event_name = get_event(event_code)["name"]
        _sender, _event_code = sender, event_code
        _media_bytes_captured = None
        _media_url_captured   = media_url

        # Fire "searching..." in background — webhook returns 200 instantly
        threading.Thread(target=send_msg, args=(_sender, f"🔍 جاري البحث في {event_name}... سأرسل لك النتيجة خلال ثوانٍ ⏳"), daemon=True).start()

        _cap_msg_link  = msg_link
        _cap_msg_id    = msg_id
        _cap_device_id = device_id
        _cap_media_url = media_url
        _cap_media_wid = media_wid

        def run():
            selfie_bytes = None

            # Try direct URL download first (only if Wassenger included it in the webhook)
            if _cap_media_url:
                _wa_base = "https://api.wassenger.com"
                _abs_url = (_wa_base + _cap_media_url) if _cap_media_url.startswith("/") else _cap_media_url
                auth_hdrs = {"Authorization": WASSENGER_API_KEY} if WASSENGER_API_KEY else {}
                try:
                    r = requests.get(_abs_url, headers=auth_hdrs, timeout=20, allow_redirects=True)
                    print(f"[SELFIE] direct url status={r.status_code} size={len(r.content)}", flush=True)
                    if r.status_code == 200 and len(r.content) > 500:
                        selfie_bytes = r.content
                except Exception as e:
                    print(f"[SELFIE] direct url error: {e}", flush=True)

            # Wassenger API lookup — always needed when webhook has no media_url
            _done = threading.Event()
            def _progress():
                if not _done.is_set():
                    send_msg(_sender, "⏳ لسه شغال على البحث، معك بثوانٍ...")
            _progress_timer = threading.Timer(10, _progress)
            _progress_timer.daemon = True
            _progress_timer.start()
            try:
                if not selfie_bytes and WASSENGER_API_KEY:
                    hdrs = {"Authorization": WASSENGER_API_KEY}
                    BASE = "https://api.wassenger.com"

                    def _abs(u):
                        return (BASE + u) if u and u.startswith("/") else u

                    _mu = _abs(_cap_media_url) if _cap_media_url else ""
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
            finally:
                _done.set()
                _progress_timer.cancel()

        threading.Thread(target=run, daemon=True).start()
        return "", 200

    # ── Text received ─────────────────────────────────────────────────────────
    upper = body_text.upper().strip()

    if state == "choosing_event":
        today_list = conv.get("today_events", [])
        chosen = None
        # Match by button label (interactive tap)
        for code in today_list:
            ev = get_event(code)
            if ev and body_text.strip() == _event_btn_label(ev["name"]):
                chosen = code
                break
        # Fallback: match by number
        if chosen is None:
            try:
                idx = int(body_text.strip()) - 1
                if 0 <= idx < len(today_list):
                    chosen = today_list[idx]
            except (ValueError, TypeError):
                pass
        if chosen:
            event = get_event(chosen)
            _set_conv(sender, "awaiting_selfie", event_code=chosen)
            return _reply(f"✨ اخترت *{event['name']}*!\n\nأرسل لي *سيلفي* لوجهك وسأجد صورك 📸" + _SELFIE_TIPS)
        all_ev = [(c, get_event(c)) for c in today_list if get_event(c)]
        body, btn_labels = _event_picker_body_and_btns(all_ev)
        return _reply_buttons(body, btn_labels)

    if state in ("new", "routing") and any(w in body_text for w in ("مرحبا", "هلا", "hi", "hello", "start", "مرحبً")):
        _clear_conv(sender)
        _end_agent_session(sender)
        state = "new"

    if state == "new":
        # Deep-link from landing page — skip menu and go straight to selfie
        if "أريد البحث عن صوري" in body_text:
            if len(_events) == 1:
                code  = next(iter(_events))
                event = _events[code]
                _set_conv(sender, "awaiting_selfie", event_code=code)
                return _reply(f"✨ أهلاً بك في *{event['name']}*!\n\nأرسل لي *سيلفي* لوجهك وسأجد لك جميع صورك 🎉📸" + _SELFIE_TIPS)
            else:
                all_ev = list(_events.items())
                _set_conv(sender, "choosing_event")
                _conv[sender]["today_events"] = [c for c, _ in all_ev]
                body, btn_labels = _event_picker_body_and_btns(all_ev)
                return _reply_buttons(body, btn_labels)
        _set_conv(sender, "routing")
        threading.Thread(target=send_buttons, args=(sender, "🌙 أهلاً وسهلاً! كيف أقدر أساعدك؟", ["📸 ابحث عن صوري", "💬 استفسار وتواصل"]), daemon=True).start()
        return "", 200

    if state == "routing":
        picked_photos  = body_text in ("1", "١", "📸 ابحث عن صوري", "🔄 بحث بوجه آخر") or \
                         any(w in body_text for w in ("صور", "ضيف", "صورة", "حفل", "ابحث", "بحث"))
        picked_inquiry = body_text in ("2", "٢", "💬 استفسار وتواصل") or \
                         any(w in body_text for w in ("استفسار", "سؤال", "تواصل"))

        if picked_photos:
            if not _events:
                return _reply("⚠️ ما فيه حفل مسجل. تواصل مع المنظم.")
            elif len(_events) == 1:
                code  = next(iter(_events))
                event = _events[code]
                _set_conv(sender, "awaiting_selfie", event_code=code)
                return _reply(f"✨ أهلاً بك في *{event['name']}*!\n\nأرسل لي *سيلفي* لوجهك وسأجد لك جميع صورك 🎉📸" + _SELFIE_TIPS)
            else:
                all_ev = list(_events.items())
                _set_conv(sender, "choosing_event")
                _conv[sender]["today_events"] = [c for c, _ in all_ev]
                body, btn_labels = _event_picker_body_and_btns(all_ev)
                return _reply_buttons(body, btn_labels)
        elif picked_inquiry:
            _set_conv(sender, "awaiting_inquiry")
            return _reply("بكل سرور! اكتب استفسارك وسنوصله لفريق الدعم 💬")
        elif body_text.strip() == "⏹️ انتهيت":
            _clear_conv(sender)
            return _reply("نراك في المرة القادمة! 🌙")
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
        return _reply(f"📸 أرسل لي *سيلفي* لوجهك وسأجد صورك من {name}!" + _SELFIE_TIPS)

    if state == "awaiting_inquiry":
        # First message → start bidirectional agent session
        clean_sender = sender.replace("whatsapp:", "")
        owner = OWNER_PHONE.lstrip("+")
        _start_agent_session(sender, owner)
        _set_conv(sender, "collecting_inquiry")
        send_msg(f"+{OWNER_PHONE}",
            f"📩 *استفسار جديد* — الضيف: +{clean_sender}\n\n"
            f"{body_text}\n\n"
            "_للرد: أجب على هذه الرسالة وسيصل للضيف تلقائياً_\n"
            "_لإنهاء المحادثة: أرسل *#end*_"
        )
        send_msg("+97433323001",
            f"📩 استفسار جديد من +{clean_sender}:\n\nالاستفسار: {body_text}"
        )
        _start_inquiry_timer(sender)
        return _reply("شكراً! هل لديك أي إضافة أو استفسار آخر؟ 💬")

    if state == "collecting_inquiry":
        # User adding more to their inquiry — forward and reset timer
        clean_sender = sender.replace("whatsapp:", "")
        session = _get_agent_session(sender)
        if session:
            send_msg(f"+{OWNER_PHONE}",
                f"💬 *إضافة من الضيف* (+{clean_sender}):\n{body_text}"
            )
            _agent_sessions[sender]["ts"] = time.time()
            _start_inquiry_timer(sender)
            return _reply("✅ تم إضافة رسالتك.")
        else:
            _clear_conv(sender)
            send_buttons(sender,
                "🌙 انتهت جلسة الدعم. كيف أقدر أساعدك؟",
                ["📸 ابحث عن صوري", "💬 استفسار وتواصل"]
            )
            return "", 200

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

    if state == "awaiting_send_confirm":
        meta      = conv.get("meta") or _conv.get(sender, {}).get("meta") or {}
        file_ids  = meta.get("file_ids", [])
        said_yes  = body_text.strip() in ("نعم، أرسل صوري 📲", "نعم", "yes", "1", "١")
        said_no   = body_text.strip() in ("لا، شكراً", "لا", "no", "2", "٢")
        is_rating = False
        try:
            v = int(body_text.strip())
            if 1 <= v <= 10:
                is_rating = True
        except (ValueError, TypeError):
            pass

        if said_yes and file_ids:
            event_code_s = conv.get("event_code", "DEFAULT")
            ev_name      = (get_event(event_code_s) or {}).get("name", event_code_s)
            def _send_all_photos():
                send_msg(sender,
                    f"📲 إليك صورك من *{ev_name}* 🌙\n"
                    "تنبيه: الصور المرسلة هنا ستكون بجودة أقل من الأصلية. للجودة الكاملة افتح معرضك الشخصي."
                )
                for fid in file_ids:
                    send_msg(sender, " ", media_url=f"{APP_URL}/photo/{fid}")
                    time.sleep(0.8)
                _set_conv(sender, "awaiting_rating", event_code=event_code_s)
                send_buttons(sender,
                    "كيف كانت تجربتك مع قمرة؟ 🌙",
                    ["😍 ممتاز", "👍 جيد", "🤔 تحتاج تحسين"],
                )
            threading.Thread(target=_send_all_photos, daemon=True).start()
            return "", 200

        # No, or rating typed, or unknown text → skip to rating
        _set_conv(sender, "awaiting_rating", event_code=conv.get("event_code"))
        if is_rating:
            # Re-inject the rating value by falling through
            _conv[sender]["state"] = "awaiting_rating"
            body_text_forwarded = body_text
            # handle inline below — fall through to awaiting_rating block
        else:
            send_buttons(sender,
                "كيف كانت تجربتك مع قمرة؟ 🌙",
                ["😍 ممتاز", "👍 جيد", "🤔 تحتاج تحسين"],
            )
            return "", 200

    if state == "awaiting_rating":
        chosen = body_text.strip()
        if "ممتاز" in chosen:
            rating, label = 10, "😍 ممتاز"
        elif "جيد" in chosen:
            rating, label = 7, "👍 جيد"
        elif "تحسين" in chosen:
            rating, label = 4, "🤔 تحتاج تحسين"
        else:
            return _reply_buttons(
                "كيف كانت تجربتك مع قمرة؟ 🌙",
                ["😍 ممتاز", "👍 جيد", "🤔 تحتاج تحسين"],
            )
        _set_conv(sender, "awaiting_comment", event_code=conv.get("event_code"))
        _conv[sender]["pending_rating"] = rating
        threading.Thread(target=send_buttons, args=(
            sender,
            f"{label}\n\nشكراً على رأيك! 🙏\n\nهل تودّ إضافة تعليق؟",
            ["✍️ أضف تعليقاً", "⏭️ تخطي"],
        ), daemon=True).start()
        return "", 200

    if state == "awaiting_comment":
        event_code = conv.get("event_code", "DEFAULT")
        rating     = _conv.get(sender, {}).get("pending_rating", 0)
        skip       = body_text.strip() in ("⏭️ تخطي", "تخطي", "2", "٢", "skip", "لا", "no", "-")
        comment    = "" if skip else body_text.strip()
        # If they clicked "add comment" button, prompt them to write it
        if body_text.strip() in ("✍️ أضف تعليقاً", "1", "١"):
            return _reply("اكتب تعليقك وسنحفظه 📝")
        threading.Thread(target=save_rating, args=(event_code, sender, rating, comment), daemon=True).start()
        _set_conv(sender, "routing")
        send_buttons(sender,
            "✅ تم حفظ تقييمك، شكراً جزيلاً! 🌙\n\nهل تريد البحث عن وجه آخر؟",
            ["🔄 بحث بوجه آخر", "⏹️ انتهيت"],
        )
        return "", 200

    # Fallback
    _clear_conv(sender)
    threading.Thread(target=send_buttons, args=(sender, "🌙 أهلاً وسهلاً! كيف أقدر أساعدك؟", ["📸 ابحث عن صوري", "💬 استفسار وتواصل"]), daemon=True).start()
    return "", 200


def _ensure_webhook():
    """Register Wassenger inbound webhook on startup, re-register if failed."""
    if not WASSENGER_API_KEY:
        return
    headers = {"Authorization": WASSENGER_API_KEY, "Content-Type": "application/json"}
    webhook_url = f"{APP_URL}/whatsapp"
    try:
        hooks_r = requests.get("https://api.wassenger.com/v1/webhooks", headers=headers, timeout=15)
        hooks = hooks_r.json() if hooks_r.status_code == 200 else []
        if isinstance(hooks, list):
            for h in hooks:
                if h.get("url") == webhook_url:
                    if h.get("status") == "failed":
                        hid = h.get("id") or h.get("_id")
                        if hid:
                            requests.delete(f"https://api.wassenger.com/v1/webhooks/{hid}",
                                            headers=headers, timeout=10)
                        print(f"[WEBHOOK_INIT] Deleted failed webhook, re-registering", flush=True)
                        break
                    else:
                        print("[WEBHOOK_INIT] Webhook active", flush=True)
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

def _webhook_watchdog_loop():
    """Check webhook health every hour and auto-heal if failed."""
    time.sleep(300)
    while True:
        try:
            _ensure_webhook()
        except Exception as e:
            print(f"[WEBHOOK_WATCHDOG] Error: {e}", flush=True)
        time.sleep(3600)

threading.Thread(target=_ensure_webhook, daemon=True).start()
threading.Thread(target=_webhook_watchdog_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
