import os
import io
import json
import threading
import hashlib
import time
import requests

import boto3
from botocore.exceptions import ClientError
from flask import Flask, request, send_file, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image, ImageOps

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TWILIO_SID              = os.environ["TWILIO_SID"]
TWILIO_TOKEN            = os.environ["TWILIO_TOKEN"]
TWILIO_WHATSAPP         = os.environ["TWILIO_WHATSAPP"]
AWS_ACCESS_KEY_ID       = os.environ["AWS_ACCESS_KEY_ID"]
AWS_SECRET_ACCESS_KEY   = os.environ["AWS_SECRET_ACCESS_KEY"]
AWS_REGION              = os.environ.get("AWS_REGION", "us-east-1")
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS"]
APP_URL                 = os.environ.get("APP_URL", "https://qamra-production.up.railway.app")
ADMIN_TOKEN             = os.environ.get("ADMIN_TOKEN", "qamra-admin-2026")
OWNER_WHATSAPP          = "whatsapp:+97470263297"

MATCH_CONF  = 80
MEDIA_DIR   = "/tmp/qamra_media"
EVENTS_FILE = "/tmp/qamra_events.json"   # ephemeral; backed up to Drive
EVENTS_DRIVE_NAME = "_qamra_events_.json"

os.makedirs(MEDIA_DIR, exist_ok=True)
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

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
    # Try local file first
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE) as f:
                _events = json.load(f)
            print(f"[EVENTS] Loaded {len(_events)} events from local file", flush=True)
            return
        except Exception:
            pass
    # Fallback: load from Drive
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

load_events()

# ── Per-event state ───────────────────────────────────────────────────────────
def _state_file(event_code):
    return f"/tmp/qamra_state_{event_code}.json"

def load_state(event_code):
    path = _state_file(event_code)
    try:
        with open(path) as f:
            s = json.load(f)
            if s.get("indexed_ids") is not None:
                return s
    except Exception:
        pass
    return {"indexed_ids": [], "file_map": {}}

def save_state(event_code, state):
    with open(_state_file(event_code), "w") as f:
        json.dump(state, f)

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
    if len(image_bytes) <= 5 * 1024 * 1024:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        if img.width > 2048:
            ratio = 2048 / img.width
            img = img.resize((2048, int(img.height * ratio)), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
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
    time.sleep(5)  # wait for startup
    while True:
        with _events_lock:
            codes = list(_events.keys())
        for code in codes:
            try:
                run_index(code)
            except Exception as e:
                print(f"[AUTO-INDEX] {code} error: {e}", flush=True)
        time.sleep(120)  # re-check every 2 minutes

threading.Thread(target=_auto_index_loop, daemon=True).start()

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
    for fid in file_ids:
        try:
            svc.files().create(body={
                "name": fid,
                "mimeType": "application/vnd.google-apps.shortcut",
                "shortcutDetails": {"targetId": fid},
                "parents": [folder_id],
            }, fields="id").execute()
        except Exception as e:
            print(f"[FOLDER] shortcut error {fid}: {e}", flush=True)
    link = f"https://drive.google.com/drive/folders/{folder_id}"
    print(f"[FOLDER] Created: {link}", flush=True)
    return link

# ── WhatsApp search + send ────────────────────────────────────────────────────
def search_and_send(selfie_bytes, sender, event_code):
    event    = get_event(event_code)
    if not event:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="⚠️ الحفل غير موجود. تأكد من الكود وحاول مرة ثانية."
        )
        return

    state    = load_state(event_code)
    file_map = state.get("file_map", {})

    if not state.get("indexed_ids"):
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="⏳ الصور لم تُفهرس بعد — تواصل مع المنظم"
        )
        return

    matches = search_by_selfie(selfie_bytes, event["collection_id"])
    if not matches:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="😕 ما لقيت وجه في الصورة أو ما لقيت صورك — أرسل سيلفي واضح وحاول مرة ثانية"
        )
        return

    seen_ids, matched_entries = set(), []
    for m in matches:
        file_id = m["Face"]["ExternalImageId"]
        conf    = m["Similarity"]
        if file_id not in seen_ids:
            seen_ids.add(file_id)
            entry = file_map.get(file_id, {"name": file_id, "link": ""})
            entry["conf"] = conf
            matched_entries.append((file_id, entry))

    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP, to=sender,
        body=f"✅ وجدت {len(matched_entries)} صورة لك من {event['name']} 🎉 — جاري الإرسال..."
    )

    uid  = hashlib.md5(f"{sender}{time.time()}".encode()).hexdigest()[:8]
    sent = 0
    for i, (file_id, entry) in enumerate(matched_entries):
        try:
            raw      = download_file(file_id)
            img_name = f"qamra_{uid}_{i+1}.jpg"
            img_path = os.path.join(MEDIA_DIR, img_name)
            if save_jpeg(raw, img_path):
                img_url = f"{APP_URL}/media/{img_name}"
                conf    = entry.get("conf", 0)
                twilio_client.messages.create(
                    from_=TWILIO_WHATSAPP, to=sender,
                    body=f"📷 صورة {i+1} — تطابق {conf:.0f}%",
                    media_url=[img_url]
                )
                sent += 1
                time.sleep(2)
        except Exception as e:
            print(f"[REPLY] ERROR photo {i+1}: {e}", flush=True)

    # Personal folder with all photos
    try:
        phone_label = sender.replace("whatsapp:", "").replace("+", "")
        folder_link = create_guest_folder(phone_label, [fid for fid, _ in matched_entries], event["name"])
        time.sleep(2)
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body=f"📂 جميع صورك ({len(matched_entries)} صورة) في مجلد خاص بك:\n\n{folder_link}"
        )
    except Exception as e:
        print(f"[REPLY] ERROR folder: {e}", flush=True)

    time.sleep(3)

    if sent > 0:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="شكراً لاستخدامك قمرة 🌙\nنتمنى أن الصور عجبتك وخلّت الذكرى تدوم ✨\ننتظرك معنا في المرة الجاية 🎉"
        )
    else:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="⚠️ فيه خطأ في إرسال الصور، جرب مرة ثانية."
        )

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
        return jsonify({"error": f"Unknown event: {event_code}. Register it first via /admin/event"}), 404

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

    # Build personal folder in background
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
    """
    Register a new wedding event.
    POST JSON: {
      "code": "AHMED2026",
      "name": "حفل أحمد ومريم",
      "collection_id": "qamra-ahmed2026",
      "gdrive_folder_id": "1ABC...",
      "drive_url": "https://drive.google.com/drive/folders/...",  (optional — public album link)
      "kiosk_url": "http://192.168.1.10:5000?event=AHMED2026"     (optional — local kiosk URL)
    }
    Header: X-Admin-Token: <token>
    """
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

# ── WhatsApp webhook ──────────────────────────────────────────────────────────
@app.route("/event/<code>", methods=["GET"])
def event_landing(code):
    event = get_event(code.upper())
    if not event:
        return "حفل غير موجود", 404

    wa_number  = TWILIO_WHATSAPP.replace("whatsapp:", "").replace("+", "")
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

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    num_media = int(request.form.get("NumMedia", 0))
    sender    = request.form.get("From", "")
    body_text = request.form.get("Body", "").strip()
    conv      = _get_conv(sender)
    state     = conv["state"]
    resp      = MessagingResponse()
    msg       = resp.message()

    # ── Selfie received ───────────────────────────────────────────────────────
    if num_media > 0:
        media_url = request.form.get("MediaUrl0")
        if not media_url:
            msg.body("⚠️ ما وصلت الصورة. جرب مرة ثانية.")
            return str(resp)

        event_code = conv.get("event_code")
        if not event_code or not get_event(event_code):
            msg.body("⚠️ ما عندك حفل محدد. امسح QR الكود من الحفل أو أرسل كود الحفل أولاً.")
            return str(resp)

        selfie_bytes = None
        for auth in [None, (TWILIO_SID, TWILIO_TOKEN)]:
            try:
                r = requests.get(media_url, auth=auth, timeout=20, allow_redirects=True)
                if r.status_code == 200:
                    selfie_bytes = r.content
                    break
            except Exception as e:
                print(f"[DOWNLOAD] error: {e}", flush=True)

        if not selfie_bytes:
            msg.body("⚠️ ما قدرت أحمل الصورة. جرب مرة ثانية.")
            return str(resp)

        _clear_conv(sender)
        app_ctx = app.app_context()

        def run():
            app_ctx.push()
            try:
                search_and_send(selfie_bytes, sender, event_code)
            except Exception as e:
                print(f"[ERROR] {e}", flush=True)
                try:
                    twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=sender, body=f"⚠️ {str(e)}")
                except Exception:
                    pass
            finally:
                app_ctx.pop()

        threading.Thread(target=run, daemon=True).start()
        event_name = get_event(event_code)["name"]
        msg.body(f"🔍 جاري البحث في {event_name}... سأرسل لك النتيجة خلال ثوانٍ ⏳")
        return str(resp)

    # ── Text received ─────────────────────────────────────────────────────────
    upper = body_text.upper().strip()

    # Check if the text is a registered event code
    if upper in _events:
        event = _events[upper]
        _set_conv(sender, "awaiting_selfie", event_code=upper)
        msg.body(
            f"🌙 أهلاً بك في *{event['name']}*!\n\n"
            "أرسل لي *سيلفي واضح* لوجهك وسأجد لك جميع صورك من الحفل 🎉📸"
        )
        return str(resp)

    # Reset keywords
    if any(w in body_text for w in ("مرحبا", "هلا", "hi", "hello", "start", "مرحبا")):
        _clear_conv(sender)
        _set_conv(sender, "routing")

    # New user or reset
    if state in ("new", "routing") and upper not in _events:
        _set_conv(sender, "routing")
        msg.body(
            "🌙 أهلاً وسهلاً!\n\n"
            "كيف أقدر أساعدك؟\n\n"
            "رد بـ *1* — إذا كنت ضيفاً تبحث عن صورك من الحفل 📸\n"
            "رد بـ *2* — إذا لديك استفسار عام 💬"
        )
        return str(resp)

    if state == "routing":
        if body_text in ("1", "١") or any(w in body_text for w in ("صور", "ضيف", "صورة", "حفل")):
            _set_conv(sender, "awaiting_event_code")
            codes = ", ".join(_events.keys()) if _events else "(لا يوجد أحداث مسجلة)"
            msg.body(
                "✨ ممتاز!\n\n"
                f"أرسل لي *كود الحفل* — ستجده على QR الكود في الحفل.\n\n"
                f"الأحداث المتاحة: {codes}"
            )
        elif body_text in ("2", "٢") or any(w in body_text for w in ("استفسار", "سؤال")):
            _set_conv(sender, "awaiting_inquiry")
            msg.body("بكل سرور! اكتب استفسارك وسأوصله لفريقنا 💬")
        else:
            msg.body("من فضلك رد بـ *1* أو *2*.")
        return str(resp)

    if state == "awaiting_event_code":
        msg.body(
            f"⚠️ الكود '{body_text}' غير موجود.\n\n"
            "تأكد من الكود وحاول مرة ثانية، أو امسح QR الكود من الحفل مباشرة."
        )
        return str(resp)

    if state == "awaiting_selfie":
        event_name = get_event(conv.get("event_code", ""))
        name = event_name["name"] if event_name else "الحفل"
        msg.body(f"📸 أرسل لي *سيلفي* لوجهك وسأجد صورك من {name}!")
        return str(resp)

    if state == "awaiting_inquiry":
        _clear_conv(sender)
        msg.body("شكراً! تم إيصال استفسارك وسيتواصل معك فريقنا قريباً 🌙")
        try:
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP, to=OWNER_WHATSAPP,
                body=f"📩 استفسار جديد\nمن: {sender.replace('whatsapp:', '')}\n\n{body_text}"
            )
        except Exception as e:
            print(f"[INQUIRY] Forward error: {e}", flush=True)
        return str(resp)

    # Fallback
    _set_conv(sender, "routing")
    msg.body(
        "🌙 أهلاً وسهلاً!\n\n"
        "رد بـ *1* — تبحث عن صورك 📸\n"
        "رد بـ *2* — استفسار عام 💬"
    )
    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
