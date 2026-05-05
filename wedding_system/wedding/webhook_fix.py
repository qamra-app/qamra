import os
import io
import json
import threading
import hashlib
import time
import requests

from flask import Flask, request, send_file
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from PIL import Image

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TWILIO_SID              = os.environ["TWILIO_SID"]
TWILIO_TOKEN            = os.environ["TWILIO_TOKEN"]
TWILIO_WHATSAPP         = os.environ["TWILIO_WHATSAPP"]
FACEPP_API_KEY          = os.environ["FACEPP_API_KEY"]
FACEPP_API_SECRET       = os.environ["FACEPP_API_SECRET"]
GDRIVE_FOLDER_ID        = os.environ["GDRIVE_FOLDER_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS"]
FACEPP_FACESET_TOKEN    = os.environ.get("FACEPP_FACESET_TOKEN", "")
APP_URL                 = os.environ.get("APP_URL", "https://qamra-production.up.railway.app")

FACEPP_BASE      = "https://api-us.faceplusplus.com/facepp/v3"
MATCH_CONF       = 70
LOCAL_STATE_FILE = "/tmp/qamra_state.json"
MEDIA_DIR        = "/tmp/qamra_media"
DRIVE_STATE_NAME = "qamra_state.json"

os.makedirs(MEDIA_DIR, exist_ok=True)
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

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
            if s.get("faces"):
                return s
    except Exception:
        pass

    # Load from Drive (state file stored INSIDE the wedding folder)
    try:
        svc = _drive()
        hits = svc.files().list(
            q=f"name='{DRIVE_STATE_NAME}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false",
            fields="files(id)"
        ).execute().get("files", [])
        if hits:
            raw = download_file(hits[0]["id"])
            state = json.loads(raw.decode())
            with open(LOCAL_STATE_FILE, "w") as f:
                json.dump(state, f)
            print(f"[STATE] Loaded from Drive: {len(state.get('faces',{}))} faces", flush=True)
            return state
    except Exception as e:
        print(f"[STATE] Drive load error: {e}", flush=True)

    return {"faceset_token": FACEPP_FACESET_TOKEN, "faces": {}}

def save_state(state):
    with open(LOCAL_STATE_FILE, "w") as f:
        json.dump(state, f)

    try:
        svc = _drive(write=True)
        content = json.dumps(state).encode()
        media   = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/json")
        hits    = svc.files().list(
            q=f"name='{DRIVE_STATE_NAME}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false",
            fields="files(id)"
        ).execute().get("files", [])
        if hits:
            svc.files().update(fileId=hits[0]["id"], media_body=media).execute()
        else:
            svc.files().create(
                body={"name": DRIVE_STATE_NAME, "parents": [GDRIVE_FOLDER_ID]},
                media_body=media
            ).execute()
        print("[STATE] Saved to Drive", flush=True)
    except Exception as e:
        print(f"[STATE] Drive save error: {e}", flush=True)

# ── Face++ helpers ────────────────────────────────────────────────────────────
def facepp(endpoint, data, files=None, retries=3):
    d = dict(data)
    d["api_key"]    = FACEPP_API_KEY
    d["api_secret"] = FACEPP_API_SECRET

    for attempt in range(retries):
        try:
            resp = requests.post(f"{FACEPP_BASE}/{endpoint}", data=d,
                                 files=files, timeout=30)
            if not resp.content:
                print(f"[FACEPP] {endpoint} empty response (attempt {attempt+1})", flush=True)
                time.sleep(1)
                continue
            result = resp.json()
            if "error_message" in result:
                print(f"[FACEPP] {endpoint} error: {result['error_message']}", flush=True)
                if "CONCURRENCY_LIMIT" in result.get("error_message", "") or \
                   "RATE_LIMIT" in result.get("error_message", ""):
                    time.sleep(2)
                    continue
            return result
        except Exception as e:
            print(f"[FACEPP] {endpoint} attempt {attempt+1} error: {e}", flush=True)
            time.sleep(1)
    return {}

def resize_for_facepp(image_bytes):
    """Resize image to max 2MB / 1920px for Face++ upload."""
    if len(image_bytes) <= 2 * 1024 * 1024:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        if img.width > 1920:
            ratio = 1920 / img.width
            img = img.resize((1920, int(img.height * ratio)), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=80)
        return out.getvalue()
    except Exception as e:
        print(f"[RESIZE] {e}", flush=True)
        return image_bytes

def detect_token(image_bytes):
    image_bytes = resize_for_facepp(image_bytes)
    result = facepp("detect", {}, files={"image_file": ("img.jpg", image_bytes, "image/jpeg")})
    faces  = result.get("faces", [])
    print(f"[DETECT] {len(faces)} face(s) found. API={result.get('error_message','ok')}", flush=True)
    return faces[0]["face_token"] if faces else None

def ensure_faceset():
    global FACEPP_FACESET_TOKEN
    if FACEPP_FACESET_TOKEN:
        return FACEPP_FACESET_TOKEN
    state = load_state()
    if state.get("faceset_token"):
        FACEPP_FACESET_TOKEN = state["faceset_token"]
        return FACEPP_FACESET_TOKEN
    result = facepp("faceset/create", {"display_name": "qamra_wedding"})
    token  = result.get("faceset_token", "")
    print(f"[FACESET] Created: {token}", flush=True)
    if token:
        FACEPP_FACESET_TOKEN = token
        state["faceset_token"] = token
        save_state(state)
    return token

# ── PDF builder ───────────────────────────────────────────────────────────────
def build_pdf(photo_bytes_list, output_path):
    pages = []
    for raw in photo_bytes_list:
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            if img.width > 1800:
                ratio = 1800 / img.width
                img = img.resize((1800, int(img.height * ratio)), Image.LANCZOS)
            pages.append(img)
        except Exception as e:
            print(f"[PDF] Skip: {e}", flush=True)
    if not pages:
        return False
    pages[0].save(output_path, format="PDF", save_all=True,
                  append_images=pages[1:], resolution=150)
    mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"[PDF] {len(pages)} pages, {mb:.1f}MB → {output_path}", flush=True)
    return True

# ── Indexing ──────────────────────────────────────────────────────────────────
def run_index():
    faceset_token = ensure_faceset()
    if not faceset_token:
        print("[INDEX] No faceset token!", flush=True)
        return 0

    state       = load_state()
    faces       = state.get("faces", {})
    indexed_ids = {v["drive_id"] for v in faces.values()}

    try:
        photos = list_drive_photos()
    except Exception as e:
        print(f"[INDEX] Drive list error: {e}", flush=True)
        return 0

    print(f"[INDEX] {len(photos)} photos, {len(indexed_ids)} already indexed", flush=True)
    new_tokens = []

    for i, photo in enumerate(photos):
        if photo["id"] in indexed_ids:
            continue
        try:
            img_bytes = download_file(photo["id"])
            img_bytes = resize_for_facepp(img_bytes)
            result    = facepp("detect", {}, files={"image_file": ("img.jpg", img_bytes, "image/jpeg")})
            detected  = result.get("faces", [])
            if not detected:
                print(f"[INDEX] No face: {photo['name']}", flush=True)
            else:
                for face in detected:
                    ft = face["face_token"]
                    faces[ft] = {"drive_id": photo["id"], "link": photo["webViewLink"], "name": photo["name"]}
                    new_tokens.append(ft)
                print(f"[INDEX] {len(detected)} face(s): {photo['name']}", flush=True)
        except Exception as e:
            print(f"[INDEX] Error {photo['name']}: {e}", flush=True)

        # Rate-limit safe: 0.3s delay between calls
        time.sleep(0.3)

        # Save progress every 20 photos
        if (i + 1) % 20 == 0:
            state["faces"] = faces
            save_state(state)
            print(f"[INDEX] Progress: {i+1}/{len(photos)}, {len(new_tokens)} new faces", flush=True)

    # Add to FaceSet in batches of 5
    for i in range(0, len(new_tokens), 5):
        batch  = new_tokens[i:i+5]
        result = facepp("faceset/addface", {
            "faceset_token": faceset_token,
            "face_tokens":   ",".join(batch)
        })
        print(f"[INDEX] FaceSet add: {result.get('face_added',0)} added", flush=True)
        time.sleep(0.3)

    state["faces"] = faces
    save_state(state)
    print(f"[INDEX] Complete: {len(faces)} total faces indexed", flush=True)
    return len(faces)

# ── Search + reply ────────────────────────────────────────────────────────────
def search_and_send(selfie_bytes, sender):
    face_token = detect_token(selfie_bytes)
    if not face_token:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="😕 ما لقيت وجه في الصورة — أرسل سيلفي واضح لوجهك وحاول مرة ثانية"
        )
        return

    faceset_token = ensure_faceset()
    state = load_state()
    faces = state.get("faces", {})

    if not faces:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="⏳ الصور لم تُفهرس بعد — تواصل مع المنظم"
        )
        return

    result  = facepp("search", {
        "face_token":          face_token,
        "faceset_token":       faceset_token,
        "return_result_count": 50,
    })
    print(f"[SEARCH] raw: {json.dumps(result)[:500]}", flush=True)

    seen_ids, matched = set(), []
    for r in result.get("results", []):
        conf  = r.get("confidence", 0)
        entry = faces.get(r.get("face_token", ""))
        if conf >= MATCH_CONF and entry and entry["drive_id"] not in seen_ids:
            seen_ids.add(entry["drive_id"])
            matched.append(entry)
            print(f"[MATCH] {entry['name']} conf={conf:.1f}", flush=True)

    if not matched:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="😕 ما لقيت صورك. تأكد السيلفي واضح وحاول مرة ثانية."
        )
        return

    # Download matched photos (max 20)
    photo_bytes_list = []
    for entry in matched[:20]:
        try:
            photo_bytes_list.append(download_file(entry["drive_id"]))
        except Exception as e:
            print(f"[PDF] Download error {entry['name']}: {e}", flush=True)

    if not photo_bytes_list:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body=f"✅ وجدت {len(matched)} صورة لكن فيه خطأ في التحميل، جرب مرة ثانية."
        )
        return

    # Build PDF and send
    uid      = hashlib.md5(f"{sender}{time.time()}".encode()).hexdigest()[:10]
    pdf_name = f"qamra_{uid}.pdf"
    pdf_path = os.path.join(MEDIA_DIR, pdf_name)

    if build_pdf(photo_bytes_list, pdf_path):
        pdf_url = f"{APP_URL}/media/{pdf_name}"
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body=f"✅ وجدت {len(matched)} صورة لك من العرس 🎉",
            media_url=[pdf_url]
        )
        print(f"[REPLY] PDF sent: {pdf_url}", flush=True)
    else:
        links = "\n".join([f"📷 {e['link']}" for e in matched[:10]])
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body=f"✅ وجدت {len(matched)} صورة لك!\n\n{links}"
        )

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    state = load_state()
    return f"قمرة 🌙 — running | {len(state.get('faces', {}))} faces indexed", 200

@app.route("/debug", methods=["GET"])
def debug():
    state = load_state()
    return {
        "faces_indexed": len(state.get("faces", {})),
        "faceset_token": state.get("faceset_token", "(none)"),
        "env": {k: bool(os.environ.get(k)) for k in
                ["TWILIO_SID","TWILIO_TOKEN","TWILIO_WHATSAPP",
                 "FACEPP_API_KEY","FACEPP_API_SECRET",
                 "GDRIVE_FOLDER_ID","GOOGLE_CREDENTIALS"]},
        "gdrive_folder": os.environ.get("GDRIVE_FOLDER_ID",""),
        "app_url": APP_URL,
    }, 200

@app.route("/test-facepp", methods=["GET"])
def test_facepp():
    """Test Face++ API with a tiny 1x1 pixel image to verify credentials."""
    import base64
    # 1×1 white JPEG, enough to get a real API response (will say no face found)
    tiny = base64.b64decode(
        "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U"
        "HRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgN"
        "DRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
        "MjL/wAARCAABAAEDASIAAhEBAxEB/8QAFgABAQEAAAAAAAAAAAAAAAAABgUE/8QAIxAAAQME"
        "AgMAAAAAAAAAAAAAAQIDBAURBhIhMf/EABQBAQAAAAAAAAAAAAAAAAAAAAD/xAAUEQEAAAAA"
        "AAAAAAAAAAAAAP/aAAwDAQACEQMRAD8Aq9a1ra1rWtQf/9k="
    )
    result = facepp("detect", {}, files={"image_file": ("test.jpg", tiny, "image/jpeg")})
    return {"facepp_response": result, "api_key_prefix": FACEPP_API_KEY[:8] + "..."}, 200

@app.route("/media/<filename>", methods=["GET"])
def serve_media(filename):
    if not filename.endswith(".pdf"):
        return "Not found", 404
    filepath = os.path.join(MEDIA_DIR, filename)
    if os.path.exists(filepath):
        return send_file(filepath, mimetype="application/pdf")
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

    # Download selfie — no auth first (WhatsApp CDN), then Twilio auth
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
