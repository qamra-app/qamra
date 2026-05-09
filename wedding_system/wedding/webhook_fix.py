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

def get_todays_events():
    """Return list of (code, event) tuples for weddings happening today."""
    from datetime import date
    today = date.today().isoformat()
    return [(c, e) for c, e in _events.items() if e.get("date", "") == today]

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

    # Auto-select if no event code provided
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
// Load Drive folders
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
            "date":             data.get("date", ""),   # YYYY-MM-DD
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
            todays = get_todays_events()
            if len(todays) == 1:
                # Exactly one wedding today — auto-select
                event_code = todays[0][0]
                _set_conv(sender, "awaiting_selfie", event_code=event_code)
            elif len(_events) == 1:
                # Only one wedding ever registered — auto-select
                event_code = next(iter(_events))
                _set_conv(sender, "awaiting_selfie", event_code=event_code)
            elif len(todays) > 1:
                # Multiple weddings today — show numbered list
                options = "\n".join(f"*{i+1}* — {e['name']}" for i, (_, e) in enumerate(todays))
                _set_conv(sender, "choosing_event")
                _conv[sender]["today_events"] = [c for c, _ in todays]
                msg.body(f"🌙 فيه أكثر من حفل اليوم، اختر الحفل الذي أنت فيه:\n\n{options}")
                return str(resp)
            else:
                msg.body("⚠️ ما فيه حفل مسجل اليوم. تواصل مع المنظم.")
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

    # Guest choosing between multiple today's weddings
    if state == "choosing_event":
        today_list = conv.get("today_events", [])
        try:
            idx = int(body_text.strip()) - 1
            if 0 <= idx < len(today_list):
                chosen = today_list[idx]
                event  = get_event(chosen)
                _set_conv(sender, "awaiting_selfie", event_code=chosen)
                msg.body(f"✨ اخترت *{event['name']}*!\n\nأرسل لي *سيلفي واضح* لوجهك وسأجد صورك 📸")
                return str(resp)
        except (ValueError, TypeError):
            pass
        options = "\n".join(f"*{i+1}* — {get_event(c)['name']}" for i, c in enumerate(today_list))
        msg.body(f"أرسل رقم الحفل:\n\n{options}")
        return str(resp)

    # Check if the text is a registered event code (from QR)
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
            todays = get_todays_events()
            if len(todays) == 1:
                code  = todays[0][0]
                event = todays[0][1]
                _set_conv(sender, "awaiting_selfie", event_code=code)
                msg.body(f"✨ أهلاً بك في *{event['name']}*!\n\nأرسل لي *سيلفي واضح* لوجهك وسأجد لك جميع صورك 🎉📸")
                return str(resp)
            elif len(_events) == 1:
                code  = next(iter(_events))
                event = _events[code]
                _set_conv(sender, "awaiting_selfie", event_code=code)
                msg.body(f"✨ أهلاً بك في *{event['name']}*!\n\nأرسل لي *سيلفي واضح* لوجهك وسأجد لك جميع صورك 🎉📸")
                return str(resp)
            elif len(todays) > 1:
                options = "\n".join(f"*{i+1}* — {e['name']}" for i, (_, e) in enumerate(todays))
                _set_conv(sender, "choosing_event")
                _conv[sender]["today_events"] = [c for c, _ in todays]
                msg.body(f"🌙 فيه أكثر من حفل اليوم، اختر الحفل الذي أنت فيه:\n\n{options}")
                return str(resp)
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
