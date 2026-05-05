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
DRIVE_STATE_FILE = "qamra_state.json"   # persisted in Drive root

os.makedirs(MEDIA_DIR, exist_ok=True)
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

# ── Google Drive (read) ───────────────────────────────────────────────────────
def _creds(scope):
    return service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS_JSON), scopes=[scope]
    )

def get_drive_ro():
    return build("drive", "v3", credentials=_creds("https://www.googleapis.com/auth/drive.readonly"))

def get_drive_rw():
    return build("drive", "v3", credentials=_creds("https://www.googleapis.com/auth/drive"))

def list_drive_photos():
    svc = get_drive_ro()
    results, page_token = [], None
    while True:
        resp = svc.files().list(
            q=f"'{GDRIVE_FOLDER_ID}' in parents and mimeType contains 'image/' and trashed=false",
            fields="nextPageToken, files(id, name, webViewLink)",
            pageSize=200, pageToken=page_token
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results

def download_photo(file_id):
    svc = get_drive_ro()
    req = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()

# ── State: load/save with Drive backup ───────────────────────────────────────
def load_state():
    # Try local first
    try:
        with open(LOCAL_STATE_FILE) as f:
            s = json.load(f)
            if s.get("faces"):
                return s
    except Exception:
        pass

    # Fall back to Drive backup
    try:
        svc = get_drive_ro()
        results = svc.files().list(
            q=f"name='{DRIVE_STATE_FILE}' and trashed=false",
            fields="files(id)"
        ).execute().get("files", [])
        if results:
            fid = results[0]["id"]
            raw = download_photo(fid)
            state = json.loads(raw.decode())
            # Cache locally
            with open(LOCAL_STATE_FILE, "w") as f:
                json.dump(state, f)
            print(f"[STATE] Loaded from Drive: {len(state.get('faces', {}))} faces", flush=True)
            return state
    except Exception as e:
        print(f"[STATE] Drive load error: {e}", flush=True)

    return {"faceset_token": FACEPP_FACESET_TOKEN, "faces": {}}

def save_state(state):
    # Save locally
    with open(LOCAL_STATE_FILE, "w") as f:
        json.dump(state, f)

    # Back up to Drive
    try:
        content = json.dumps(state).encode()
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/json")
        svc = get_drive_rw()
        results = svc.files().list(
            q=f"name='{DRIVE_STATE_FILE}' and trashed=false",
            fields="files(id)"
        ).execute().get("files", [])
        if results:
            svc.files().update(fileId=results[0]["id"], media_body=media).execute()
        else:
            svc.files().create(body={"name": DRIVE_STATE_FILE}, media_body=media).execute()
        print("[STATE] Saved to Drive", flush=True)
    except Exception as e:
        print(f"[STATE] Drive save error: {e}", flush=True)

# ── Face++ helpers ────────────────────────────────────────────────────────────
def facepp(endpoint, data, files=None):
    d = dict(data)
    d["api_key"]    = FACEPP_API_KEY
    d["api_secret"] = FACEPP_API_SECRET
    resp = requests.post(f"{FACEPP_BASE}/{endpoint}", data=d, files=files, timeout=30)
    return resp.json()

def detect_token(image_bytes):
    result = facepp("detect", {}, files={"image_file": ("img.jpg", image_bytes, "image/jpeg")})
    faces  = result.get("faces", [])
    print(f"[DETECT] {len(faces)} face(s) — {result.get('error_message','ok')}", flush=True)
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
            print(f"[PDF] Skip image: {e}", flush=True)
    if not pages:
        return False
    pages[0].save(output_path, format="PDF", save_all=True,
                  append_images=pages[1:], resolution=150)
    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"[PDF] Built {len(pages)} pages, {size_mb:.1f} MB → {output_path}", flush=True)
    return True

# ── Indexing ──────────────────────────────────────────────────────────────────
def run_index():
    faceset_token = ensure_faceset()
    if not faceset_token:
        print("[INDEX] ERROR: no faceset token", flush=True)
        return 0

    state      = load_state()
    faces      = state.get("faces", {})
    indexed_ids = {v["drive_id"] for v in faces.values()}

    try:
        photos = list_drive_photos()
    except Exception as e:
        print(f"[INDEX] Drive error: {e}", flush=True)
        return 0

    print(f"[INDEX] {len(photos)} photos in Drive, {len(indexed_ids)} already indexed", flush=True)
    new_tokens = []

    for photo in photos:
        if photo["id"] in indexed_ids:
            continue
        try:
            img_bytes = download_photo(photo["id"])
            result    = facepp("detect", {}, files={"image_file": ("img.jpg", img_bytes, "image/jpeg")})
            detected  = result.get("faces", [])
            if not detected:
                print(f"[INDEX] No face: {photo['name']}", flush=True)
                continue
            for face in detected:
                ft = face["face_token"]
                faces[ft] = {"drive_id": photo["id"], "link": photo["webViewLink"], "name": photo["name"]}
                new_tokens.append(ft)
            print(f"[INDEX] {len(detected)} face(s): {photo['name']}", flush=True)
        except Exception as e:
            print(f"[INDEX] Error {photo['name']}: {e}", flush=True)

    # Add to FaceSet in batches of 5
    for i in range(0, len(new_tokens), 5):
        batch  = new_tokens[i:i+5]
        result = facepp("faceset/addface", {
            "faceset_token": faceset_token,
            "face_tokens":   ",".join(batch)
        })
        print(f"[INDEX] Batch: {result.get('face_added',0)} added, {result.get('failure_detail','')}", flush=True)

    state["faces"] = faces
    save_state(state)
    print(f"[INDEX] Done. {len(faces)} total faces", flush=True)
    return len(faces)

# ── Search + send PDF ─────────────────────────────────────────────────────────
def search_and_send(selfie_bytes, sender):
    face_token = detect_token(selfie_bytes)
    if not face_token:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="😕 ما لقيت وجه في الصورة — تأكد السيلفي واضح وحاول مرة ثانية"
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
        if r.get("confidence", 0) >= MATCH_CONF:
            entry = faces.get(r["face_token"])
            if entry and entry["drive_id"] not in seen_ids:
                seen_ids.add(entry["drive_id"])
                matched.append(entry)
                print(f"[SEARCH] Match: {entry['name']} conf={r['confidence']:.1f}", flush=True)

    if not matched:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body="😕 ما لقيت صورك. تأكد أن السيلفي واضح وجرب مرة ثانية."
        )
        return

    # Download matched photos (up to 20)
    photo_bytes_list = []
    for entry in matched[:20]:
        try:
            photo_bytes_list.append(download_photo(entry["drive_id"]))
        except Exception as e:
            print(f"[PDF] Download error {entry['name']}: {e}", flush=True)

    if not photo_bytes_list:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP, to=sender,
            body=f"✅ وجدت {len(matched)} صورة لكن ما قدرت أحملها. جرب مرة ثانية."
        )
        return

    # Build PDF
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
        # fallback: send links
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
    def do_index():
        run_index()
    threading.Thread(target=do_index, daemon=True).start()
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

    # Download selfie — try without auth (CDN), then with Twilio auth
    selfie_bytes = None
    for auth in [None, (TWILIO_SID, TWILIO_TOKEN)]:
        try:
            r = requests.get(media_url, auth=auth, timeout=20, allow_redirects=True)
            print(f"[DOWNLOAD] status={r.status_code} auth={'yes' if auth else 'no'} len={len(r.content)}", flush=True)
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
