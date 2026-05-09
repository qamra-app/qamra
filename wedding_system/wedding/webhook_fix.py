import os
import io
import json
import threading
import hashlib
import time
import requests

import boto3
from botocore.exceptions import ClientError
from flask import Flask, request, send_file
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
GDRIVE_FOLDER_ID        = os.environ["GDRIVE_FOLDER_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS"]
APP_URL                 = os.environ.get("APP_URL", "https://qamra-production.up.railway.app")

COLLECTION_ID    = "qamra-wedding"
MATCH_CONF       = 80
LOCAL_STATE_FILE = "/tmp/qamra_state.json"
MEDIA_DIR        = "/tmp/qamra_media"
OWNER_WHATSAPP   = "whatsapp:+97470263297"

# In-memory conversation state per user: phone -> {"state": str, "ts": float}
_conv = {}
_CONV_TTL = 3600  # 1 hour

def _get_state(phone):
    e = _conv.get(phone)
    if e and (time.time() - e["ts"]) < _CONV_TTL:
        return e["state"]
    return "new"

def _set_state(phone, state):
    _conv[phone] = {"state": state, "ts": time.time()}

def _clear_state(phone):
    _conv.pop(phone, None)

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
        json.loads(GOOGLE_CREDENTIALS_JSON), scopes=[scope]
    )
    return build("drive", "v3", credentials=creds)

def list_drive_photos():
    svc = _drive()
    results, pt = [], None
    while True:
        resp = svc.files().list(
            q=f"'{GDRIVE_FOLDER_ID}' in parents and mimeType contains 'image/' and trashed=false",
            fields="nextPageToken, files(id, name, webViewLink)",
            pageSize=200, pageToken=pt
        ).execute()
        results.extend(resp.get("files", []))
        pt = resp.get("nextPageToken")
        if not pt:
            break
    return results

def create_guest_folder(sender, file_ids):
    """Create a Drive folder with shortcuts to matched photos, return shareable link."""
    svc = _drive(write=True)
    # Create folder named after sender number
    phone = sender.replace("whatsapp:", "").replace("+", "")
    folder = svc.files().create(body={
        "name": f"صورك من الحفل 🌙 — {phone}",
        "mimeType": "application/vnd.google-apps.folder",
    }, fields="id").execute()
    folder_id = folder["id"]

    # Create a shortcut for each matched photo
    for file_id in file_ids:
        try:
            svc.files().create(body={
                "name": file_id,
                "mimeType": "application/vnd.google-apps.shortcut",
                "shortcutDetails": {"targetId": file_id},
                "parents": [folder_id],
            }, fields="id").execute()
        except Exception as e:
            print(f"[FOLDER] shortcut error {file_id}: {e}", flush=True)

    # Make folder public (anyone with link can view)
    svc.permissions().create(
        fileId=folder_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    link = f"https://drive.google.com/drive/folders/{folder_id}"
    print(f"[FOLDER] Created guest folder: {link}", flush=True)
    return link

def download_file(file_id):
    svc = _drive()
    req = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl  = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()

# ── State persistence ─────────────────────────────────────────────────────────
def load_state():
    try:
        with open(LOCAL_STATE_FILE) as f:
            s = json.load(f)
            if s.get("indexed_ids") is not None:
                return s
    except Exception:
        pass
    return {"indexed_ids": [], "file_map": {}}

def save_state(state):
    with open(LOCAL_STATE_FILE, "w") as f:
        json.dump(state, f)

# ── Rekognition helpers ───────────────────────────────────────────────────────
def resize_for_rekognition(image_bytes):
    """Resize to max 5MB for Rekognition."""
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

def ensure_collection():
    try:
        rek.create_collection(CollectionId=COLLECTION_ID)
        print(f"[COLLECTION] Created: {COLLECTION_ID}", flush=True)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceAlreadyExistsException":
            print(f"[COLLECTION] Already exists: {COLLECTION_ID}", flush=True)
        else:
            raise

def index_face(image_bytes, file_id):
    image_bytes = resize_for_rekognition(image_bytes)
    try:
        resp    = rek.index_faces(
            CollectionId=COLLECTION_ID,
            Image={"Bytes": image_bytes},
            ExternalImageId=file_id,
            DetectionAttributes=[],
            QualityFilter="AUTO",
        )
        indexed = len(resp.get("FaceRecords", []))
        print(f"[INDEX] {indexed} face(s) indexed for file_id={file_id[:12]}...", flush=True)
        return indexed
    except ClientError as e:
        print(f"[INDEX] Rekognition error: {e}", flush=True)
        return 0
    except Exception as e:
        print(f"[INDEX] Error: {e}", flush=True)
        return 0

def search_by_selfie(selfie_bytes):
    selfie_bytes = resize_for_rekognition(selfie_bytes)
    try:
        resp    = rek.search_faces_by_image(
            CollectionId=COLLECTION_ID,
            Image={"Bytes": selfie_bytes},
            MaxFaces=30,
            FaceMatchThreshold=MATCH_CONF,
        )
        matches = resp.get("FaceMatches", [])
        print(f"[SEARCH] {len(matches)} matches (threshold={MATCH_CONF})", flush=True)
        return matches
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "InvalidParameterException":
            print("[SEARCH] No face detected in selfie", flush=True)
        else:
            print(f"[SEARCH] Rekognition error: {e}", flush=True)
        return []
    except Exception as e:
        print(f"[SEARCH] Error: {e}", flush=True)
        return []

def save_jpeg(image_bytes, output_path):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)  # fix landscape rotation
        img = img.convert("RGB")
        if img.width > 1920:
            ratio = 1920 / img.width
            img = img.resize((1920, int(img.height * ratio)), Image.LANCZOS)
        img.save(output_path, format="JPEG", quality=85)
        return True
    except Exception as e:
        print(f"[JPEG] Error: {e}", flush=True)
        return False

# ── Indexing ──────────────────────────────────────────────────────────────────
_index_lock = threading.Lock()

def run_index():
    if not _index_lock.acquire(blocking=False):
        print("[INDEX] Already running, skipping.", flush=True)
        return 0
    try:
        ensure_collection()
        state       = load_state()
        indexed_ids = set(state.get("indexed_ids", []))
        file_map    = state.get("file_map", {})

        try:
            photos = list_drive_photos()
        except Exception as e:
            print(f"[INDEX] Drive list error: {e}", flush=True)
            return 0

        print(f"[INDEX] {len(photos)} photos in Drive, {len(indexed_ids)} already indexed", flush=True)
        new_count = 0

        for i, photo in enumerate(photos):
            if photo["id"] in indexed_ids:
                continue
            try:
                img_bytes = download_file(photo["id"])
                n = index_face(img_bytes, photo["id"])
                if n > 0:
                    indexed_ids.add(photo["id"])
                    file_map[photo["id"]] = {"name": photo["name"], "link": photo["webViewLink"]}
                    new_count += n
                else:
                    print(f"[INDEX] No face found: {photo['name']}", flush=True)
            except Exception as e:
                print(f"[INDEX] Error {photo['name']}: {e}", flush=True)

            if (i + 1) % 20 == 0:
                state["indexed_ids"] = list(indexed_ids)
                state["file_map"]    = file_map
                save_state(state)
                print(f"[INDEX] Progress: {i+1}/{len(photos)}, {new_count} new faces", flush=True)

        state["indexed_ids"] = list(indexed_ids)
        state["file_map"]    = file_map
        save_state(state)
        print(f"[INDEX] Complete: {len(indexed_ids)} photos indexed, {new_count} new faces", flush=True)
        return len(indexed_ids)
    finally:
        _index_lock.release()

# ── Auto-index ────────────────────────────────────────────────────────────────
AUTO_INDEX_INTERVAL = 60  # seconds (1 min)

def _auto_index_loop():
    print("[AUTO-INDEX] Startup run starting...", flush=True)
    try:
        run_index()
    except Exception as e:
        print(f"[AUTO-INDEX] Startup error: {e}", flush=True)
    while True:
        time.sleep(AUTO_INDEX_INTERVAL)
        print("[AUTO-INDEX] Scheduled run starting...", flush=True)
        try:
            run_index()
        except Exception as e:
            print(f"[AUTO-INDEX] Error: {e}", flush=True)

threading.Thread(target=_auto_index_loop, daemon=True).start()

# ── Search + reply ────────────────────────────────────────────────────────────
def search_and_send(selfie_bytes, sender):
    state    = load_state()
    file_map = state.get("file_map", {})

    if not state.get("indexed_ids"):
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="⏳ الصور لم تُفهرس بعد — تواصل مع المنظم"
        )
        return

    matches = search_by_selfie(selfie_bytes)
    if not matches:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="😕 ما لقيت وجه في الصورة أو ما لقيت صورك — أرسل سيلفي واضح وحاول مرة ثانية"
        )
        return

    # Deduplicate by file_id, collect matching Drive file IDs
    seen_ids, matched_entries = set(), []
    for m in matches:
        file_id  = m["Face"]["ExternalImageId"]
        conf     = m["Similarity"]
        if file_id not in seen_ids:
            seen_ids.add(file_id)
            entry = file_map.get(file_id, {"name": file_id, "link": ""})
            entry["conf"] = conf
            matched_entries.append((file_id, entry))
            print(f"[MATCH] {entry['name']} conf={conf:.1f}", flush=True)

    if not matched_entries:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="😕 ما لقيت صورك. تأكد السيلفي واضح وحاول مرة ثانية."
        )
        return

    # Send summary message first
    try:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body=f"✅ وجدت {len(matched_entries)} صورة لك من العرس 🎉 — جاري الإرسال..."
        )
        print(f"[REPLY] Sent summary to {sender}", flush=True)
    except Exception as e:
        print(f"[REPLY] ERROR sending summary to {sender}: {e}", flush=True)

    # Download and send first 10 photos as images, rest as Drive links
    sent = 0
    uid  = hashlib.md5(f"{sender}{time.time()}".encode()).hexdigest()[:8]
    for i, (file_id, entry) in enumerate(matched_entries[:10]):
        try:
            raw       = download_file(file_id)
            img_name  = f"qamra_{uid}_{i+1}.jpg"
            img_path  = os.path.join(MEDIA_DIR, img_name)
            print(f"[REPLY] Saving photo {i+1} → {img_path}", flush=True)
            if save_jpeg(raw, img_path):
                img_url = f"{APP_URL}/media/{img_name}"
                conf    = entry.get("conf", 0)
                print(f"[REPLY] Sending photo {i+1} url={img_url}", flush=True)
                twilio_client.messages.create(
                    from_=TWILIO_WHATSAPP, to=sender,
                    body=f"📷 صورة {i+1} — تطابق {conf:.0f}%",
                    media_url=[img_url]
                )
                sent += 1
                print(f"[REPLY] OK photo {i+1}/{len(matched_entries)}", flush=True)
                time.sleep(2)  # keep delivery order
            else:
                print(f"[REPLY] save_jpeg returned False for photo {i+1}", flush=True)
        except Exception as e:
            print(f"[REPLY] ERROR photo {i+1}: {e}", flush=True)

    # Create personal folder with shortcuts to all matched photos and send link
    try:
        all_file_ids = [fid for fid, _ in matched_entries]
        folder_link = create_guest_folder(sender, all_file_ids)
        time.sleep(2)
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body=f"📂 جميع صورك ({len(matched_entries)} صورة) في مجلد خاص بك:\n\n{folder_link}"
        )
    except Exception as e:
        print(f"[REPLY] ERROR creating guest folder: {e}", flush=True)

    time.sleep(3)  # wait for all photos to deliver before closing message

    if sent == 0:
        try:
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP, to=sender,
                body="⚠️ فيه خطأ في إرسال الصور، جرب مرة ثانية."
            )
        except Exception as e:
            print(f"[REPLY] ERROR sending fallback: {e}", flush=True)
    else:
        try:
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP, to=sender,
                body="شكراً لاستخدامك قمرة 🌙\n\nنتمنى أن الصور عجبتك وخلّت الذكرى تدوم ✨\n\ننتظرك معنا في المرة الجاية 🎉"
            )
        except Exception as e:
            print(f"[REPLY] ERROR sending closing: {e}", flush=True)

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    state = load_state()
    return f"قمرة 🌙 — running | {len(state.get('indexed_ids', []))} photos indexed", 200

@app.route("/debug", methods=["GET"])
def debug():
    state = load_state()
    return {
        "photos_indexed": len(state.get("indexed_ids", [])),
        "env": {k: bool(os.environ.get(k)) for k in
                ["TWILIO_SID","TWILIO_TOKEN","TWILIO_WHATSAPP",
                 "AWS_ACCESS_KEY_ID","AWS_SECRET_ACCESS_KEY",
                 "GDRIVE_FOLDER_ID","GOOGLE_CREDENTIALS"]},
        "gdrive_folder": os.environ.get("GDRIVE_FOLDER_ID",""),
        "collection_id": COLLECTION_ID,
        "aws_region": AWS_REGION,
        "app_url": APP_URL,
    }, 200

@app.route("/test-rekognition", methods=["GET"])
def test_rekognition():
    try:
        ensure_collection()
        photos = list_drive_photos()
        if not photos:
            return {"error": "no photos in drive"}, 200
        img = download_file(photos[0]["id"])
        img = resize_for_rekognition(img)
        resp = rek.detect_faces(Image={"Bytes": img}, Attributes=["DEFAULT"])
        return {
            "photo": photos[0]["name"],
            "faces_detected": len(resp.get("FaceDetails", [])),
            "collection": COLLECTION_ID,
            "region": AWS_REGION,
        }, 200
    except Exception as e:
        return {"error": str(e)}, 200

@app.route("/media/<filename>", methods=["GET"])
def serve_media(filename):
    filepath = os.path.join(MEDIA_DIR, filename)
    if not os.path.exists(filepath):
        return "Not found", 404
    if filename.endswith(".jpg") or filename.endswith(".jpeg"):
        return send_file(filepath, mimetype="image/jpeg")
    if filename.endswith(".png"):
        return send_file(filepath, mimetype="image/png")
    return "Not found", 404

@app.route("/index", methods=["POST"])
def index_photos():
    threading.Thread(target=run_index, daemon=True).start()
    return {"status": "indexing started"}, 202

@app.route("/match", methods=["POST"])
def match_api():
    """
    POST a selfie, get back matching wedding photos as JSON.
    Accepts: multipart/form-data with field 'photo' (file upload)
          OR application/json with field 'image_url' (public URL)
    Returns: { "matches": [ { "url": "...", "confidence": 95.2, "name": "..." } ] }
    """
    selfie_bytes = None

    # Accept file upload
    if "photo" in request.files:
        selfie_bytes = request.files["photo"].read()

    # Accept URL
    elif request.is_json and request.json.get("image_url"):
        try:
            r = requests.get(request.json["image_url"], timeout=15)
            if r.status_code == 200:
                selfie_bytes = r.content
        except Exception as e:
            return {"error": f"Could not fetch image: {e}"}, 400

    if not selfie_bytes:
        return {"error": "Send a photo via 'photo' file field or 'image_url' JSON field"}, 400

    state    = load_state()
    file_map = state.get("file_map", {})

    if not state.get("indexed_ids"):
        return {"error": "Photos not indexed yet, try again in a few minutes"}, 503

    matches = search_by_selfie(selfie_bytes)
    if not matches:
        return {"matches": [], "message": "No face found or no matches"}, 200

    # Deduplicate and build response
    seen, results = set(), []
    uid = hashlib.md5(f"{time.time()}".encode()).hexdigest()[:8]

    for m in matches:
        file_id = m["Face"]["ExternalImageId"]
        conf    = m["Similarity"]
        if file_id in seen:
            continue
        seen.add(file_id)
        entry    = file_map.get(file_id, {})
        img_name = f"qamra_{uid}_{len(results)+1}.jpg"
        img_path = os.path.join(MEDIA_DIR, img_name)
        try:
            raw = download_file(file_id)
            if save_jpeg(raw, img_path):
                results.append({
                    "url":        f"{APP_URL}/media/{img_name}",
                    "confidence": round(conf, 1),
                    "name":       entry.get("name", ""),
                    "drive_link": entry.get("link", ""),
                })
        except Exception as e:
            print(f"[MATCH-API] Error downloading {file_id}: {e}", flush=True)

        if len(results) >= 20:
            break

    return {"matches": results}, 200

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    num_media = int(request.form.get("NumMedia", 0))
    sender    = request.form.get("From", "")
    body_text = request.form.get("Body", "").strip()
    state     = _get_state(sender)
    resp      = MessagingResponse()
    msg       = resp.message()

    # ── Image received → always run face search ───────────────────────────────
    if num_media > 0:
        media_url = request.form.get("MediaUrl0")
        if not media_url:
            msg.body("⚠️ ما وصلت الصورة. جرب مرة ثانية.")
            return str(resp)

        print(f"[WEBHOOK] From={sender} MediaUrl={media_url[:80]}", flush=True)

        selfie_bytes = None
        for auth in [None, (TWILIO_SID, TWILIO_TOKEN)]:
            try:
                r = requests.get(media_url, auth=auth, timeout=20, allow_redirects=True)
                print(f"[DOWNLOAD] status={r.status_code} auth={'yes' if auth else 'no'} size={len(r.content)}", flush=True)
                if r.status_code == 200:
                    selfie_bytes = r.content
                    break
            except Exception as e:
                print(f"[DOWNLOAD] error: {e}", flush=True)

        if not selfie_bytes:
            msg.body("⚠️ ما قدرت أحمل الصورة. جرب مرة ثانية.")
            return str(resp)

        _clear_state(sender)
        app_ctx = app.app_context()

        def run():
            app_ctx.push()
            try:
                search_and_send(selfie_bytes, sender)
            except Exception as e:
                print(f"[ERROR] {e}", flush=True)
                try:
                    twilio_client.messages.create(
                        from_=TWILIO_WHATSAPP, to=sender, body=f"⚠️ {str(e)}"
                    )
                except Exception:
                    pass
            finally:
                app_ctx.pop()

        threading.Thread(target=run, daemon=True).start()
        msg.body("🔍 جاري البحث عن صورك... سأرسل لك النتيجة خلال ثوانٍ ⏳")
        return str(resp)

    # ── Text received ─────────────────────────────────────────────────────────

    # Step 1: new user → ask routing question
    if state == "new":
        _set_state(sender, "routing")
        msg.body(
            "🌙 أهلاً وسهلاً!\n\n"
            "كيف أقدر أساعدك؟\n\n"
            "رد بـ *1* — إذا كنت ضيفاً تبحث عن صورك من الحفل 📸\n"
            "رد بـ *2* — إذا لديك استفسار عام 💬"
        )
        return str(resp)

    # Step 2: waiting for 1 or 2
    if state == "routing":
        if body_text in ("1", "١") or any(w in body_text for w in ("صور", "ضيف", "صورة", "حفل")):
            _set_state(sender, "awaiting_selfie")
            msg.body(
                "✨ ممتاز!\n\n"
                "أرسل لي *سيلفي واضح* لوجهك وسأجد لك جميع صورك من العرس خلال ثوانٍ 🎉"
            )
        elif body_text in ("2", "٢") or any(w in body_text for w in ("استفسار", "سؤال", "تواصل")):
            _set_state(sender, "awaiting_inquiry")
            msg.body("بكل سرور! اكتب استفسارك وسأوصله لفريقنا 💬")
        else:
            msg.body(
                "من فضلك رد بـ *1* أو *2*:\n\n"
                "*1* — ضيف يبحث عن صوره من الحفل 📸\n"
                "*2* — استفسار عام 💬"
            )
        return str(resp)

    # Step 3a: guest told to send selfie but sent text instead
    if state == "awaiting_selfie":
        msg.body("📸 أرسل لي *سيلفي* لوجهك وسأجد صورك!\n\nللبداية من جديد اكتب *مرحبا*")
        return str(resp)

    # Step 3b: inquiry text received → forward to owner
    if state == "awaiting_inquiry":
        _clear_state(sender)
        msg.body("شكراً! تم إيصال استفسارك وسيتواصل معك فريقنا قريباً 🌙")
        try:
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP,
                to=OWNER_WHATSAPP,
                body=f"📩 استفسار جديد\nمن: {sender.replace('whatsapp:', '')}\n\n{body_text}"
            )
            print(f"[INQUIRY] Forwarded from {sender}", flush=True)
        except Exception as e:
            print(f"[INQUIRY] Forward error: {e}", flush=True)
        return str(resp)

    # Fallback / reset on "مرحبا" or anything unexpected
    _set_state(sender, "routing")
    msg.body(
        "🌙 أهلاً وسهلاً!\n\n"
        "كيف أقدر أساعدك؟\n\n"
        "رد بـ *1* — إذا كنت ضيفاً تبحث عن صورك من الحفل 📸\n"
        "رد بـ *2* — إذا لديك استفسار عام 💬"
    )
    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
