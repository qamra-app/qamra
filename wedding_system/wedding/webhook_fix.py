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
def run_index():
    ensure_collection()
    state       = load_state()
    indexed_ids = set(state.get("indexed_ids", []))
    file_map    = state.get("file_map", {})  # file_id → {name, link}

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

# ── Auto-index ────────────────────────────────────────────────────────────────
AUTO_INDEX_INTERVAL = 900  # seconds (15 min)

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
    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP, to=sender,
        body=f"✅ وجدت {len(matched_entries)} صورة لك من العرس 🎉 — جاري الإرسال..."
    )

    # Download and send each photo as individual JPEG (max 10)
    sent = 0
    uid  = hashlib.md5(f"{sender}{time.time()}".encode()).hexdigest()[:8]
    for i, (file_id, entry) in enumerate(matched_entries[:10]):
        try:
            raw       = download_file(file_id)
            img_name  = f"qamra_{uid}_{i+1}.jpg"
            img_path  = os.path.join(MEDIA_DIR, img_name)
            if save_jpeg(raw, img_path):
                img_url = f"{APP_URL}/media/{img_name}"
                conf    = entry.get("conf", 0)
                twilio_client.messages.create(
                    from_=TWILIO_WHATSAPP, to=sender,
                    body=f"📷 صورة {i+1} — تطابق {conf:.0f}%",
                    media_url=[img_url]
                )
                sent += 1
                print(f"[REPLY] Sent photo {i+1}/{len(matched_entries)}: {img_url}", flush=True)
        except Exception as e:
            print(f"[REPLY] Error sending photo {i+1}: {e}", flush=True)

    if sent == 0:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="⚠️ فيه خطأ في إرسال الصور، جرب مرة ثانية."
        )
    else:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="شكراً لاستخدامك قمرة 🌙\n\nنتمنى أن الصور عجبتك وخلّت الذكرى تدوم ✨\n\ننتظرك معنا في المرة الجاية 🎉"
        )

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

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    num_media = int(request.form.get("NumMedia", 0))
    resp = MessagingResponse()
    msg  = resp.message()

    if num_media == 0:
        msg.body(
            "🌙 أهلاً وسهلاً بك في قمرة\n\n"
            "نحن سعداء بوجودك معنا الليلة ✨\n\n"
            "أرسل لي *سيلفي واضح* لوجهك وسأجد لك جميع صورك من العرس خلال ثوانٍ 🎉"
        )
        return str(resp)

    media_url = request.form.get("MediaUrl0")
    if not media_url:
        msg.body("⚠️ ما وصلت الصورة. جرب مرة ثانية.")
        return str(resp)

    sender = request.form.get("From", "")
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
