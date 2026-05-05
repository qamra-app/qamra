import os
import io
import json
import threading
import requests

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

app = Flask(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
TWILIO_SID              = os.environ["TWILIO_SID"]
TWILIO_TOKEN            = os.environ["TWILIO_TOKEN"]
TWILIO_WHATSAPP         = os.environ["TWILIO_WHATSAPP"]
FACEPP_API_KEY          = os.environ["FACEPP_API_KEY"]
FACEPP_API_SECRET       = os.environ["FACEPP_API_SECRET"]
GDRIVE_FOLDER_ID        = os.environ["GDRIVE_FOLDER_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS"]
FACEPP_FACESET_TOKEN    = os.environ.get("FACEPP_FACESET_TOKEN", "")

FACEPP_BASE    = "https://api-us.faceplusplus.com/facepp/v3"
MATCH_CONF     = 76   # Face++ recommended threshold for same person
FACEMAP_FILE   = "/tmp/facemap.json"   # face_token -> {drive_id, link}

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

# ── Google Drive ──────────────────────────────────────────────────────────────
def get_drive_service():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS_JSON),
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)


def list_drive_photos() -> list[dict]:
    service = get_drive_service()
    results, page_token = [], None
    while True:
        resp = service.files().list(
            q=f"'{GDRIVE_FOLDER_ID}' in parents and mimeType contains 'image/' and trashed=false",
            fields="nextPageToken, files(id, name, webViewLink)",
            pageSize=200,
            pageToken=page_token
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def download_photo(file_id: str) -> bytes:
    service = get_drive_service()
    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


# ── Face++ helpers ────────────────────────────────────────────────────────────
def facepp(endpoint: str, data: dict, files=None):
    data["api_key"]    = FACEPP_API_KEY
    data["api_secret"] = FACEPP_API_SECRET
    resp = requests.post(f"{FACEPP_BASE}/{endpoint}", data=data, files=files, timeout=20)
    return resp.json()


def detect_token(image_bytes: bytes) -> str | None:
    result = facepp("detect", {}, files={"image_file": ("img.jpg", image_bytes, "image/jpeg")})
    faces = result.get("faces", [])
    if not faces:
        print(f"[DETECT] No face found. API response: {result}", flush=True)
    return faces[0]["face_token"] if faces else None


def ensure_faceset() -> str:
    global FACEPP_FACESET_TOKEN
    if FACEPP_FACESET_TOKEN:
        return FACEPP_FACESET_TOKEN
    result = facepp("faceset/create", {"display_name": "qamra_wedding"})
    token = result.get("faceset_token", "")
    print(f"[FACESET] Created: {token} — add FACEPP_FACESET_TOKEN={token} to Railway variables", flush=True)
    FACEPP_FACESET_TOKEN = token
    return token


def load_facemap() -> dict:
    try:
        with open(FACEMAP_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_facemap(fm: dict):
    with open(FACEMAP_FILE, "w") as f:
        json.dump(fm, f)


# ── Indexing (run once via /index endpoint) ───────────────────────────────────
def run_index():
    faceset_token = ensure_faceset()
    facemap       = load_facemap()
    indexed_ids   = {v["drive_id"] for v in facemap.values()}
    photos        = list_drive_photos()
    new_tokens    = []

    for photo in photos:
        if photo["id"] in indexed_ids:
            continue
        try:
            img_bytes = download_photo(photo["id"])
            result    = facepp("detect", {}, files={"image_file": ("img.jpg", img_bytes, "image/jpeg")})
            faces     = result.get("faces", [])
            if not faces:
                print(f"[INDEX] No face: {photo['name']}", flush=True)
                continue
            for face in faces:
                ft = face["face_token"]
                facemap[ft] = {"drive_id": photo["id"], "link": photo["webViewLink"], "name": photo["name"]}
                new_tokens.append(ft)
            print(f"[INDEX] Indexed {len(faces)} face(s): {photo['name']}", flush=True)
        except Exception as e:
            print(f"[INDEX] Error {photo['name']}: {e}", flush=True)

    # Add to FaceSet in batches of 5
    for i in range(0, len(new_tokens), 5):
        batch = new_tokens[i:i+5]
        result = facepp("faceset/addface", {
            "faceset_token": faceset_token,
            "face_tokens": ",".join(batch)
        })
        print(f"[INDEX] Added batch to FaceSet: {result.get('face_added', 0)} added", flush=True)

    save_facemap(facemap)
    print(f"[INDEX] Done. Total faces indexed: {len(facemap)}", flush=True)
    return len(facemap)


# ── Search ────────────────────────────────────────────────────────────────────
def find_photos_for_selfie(selfie_bytes: bytes) -> list[str]:
    face_token = detect_token(selfie_bytes)
    if not face_token:
        raise ValueError("ما لقيت وجه في السيلفي — تأكد الصورة واضحة")

    faceset_token = ensure_faceset()
    facemap       = load_facemap()

    if not facemap:
        raise ValueError("الصور لم تُفهرس بعد — تواصل مع المنظم")

    result  = facepp("search", {
        "face_token":          face_token,
        "faceset_token":       faceset_token,
        "return_result_count": 50,
    })

    print(f"[SEARCH] Face++ search result: {json.dumps(result)[:300]}", flush=True)

    results = result.get("results", [])
    seen    = set()
    links   = []
    for r in results:
        if r["confidence"] >= MATCH_CONF:
            entry = facemap.get(r["face_token"])
            if entry and entry["link"] not in seen:
                seen.add(entry["link"])
                links.append(entry["link"])

    return links


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    fm = load_facemap()
    return f"قمرة 🌙 — running | {len(fm)} faces indexed", 200


@app.route("/index", methods=["POST"])
def index_photos():
    def do_index():
        count = run_index()
        print(f"[INDEX] Complete: {count} faces", flush=True)
    threading.Thread(target=do_index).start()
    return {"status": "indexing started in background"}, 202


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
        msg.body("⚠️ ما وصلت الصورة. جرب ترسلها مرة ثانية.")
        return str(resp)

    sender = request.form.get("From", "")

    try:
        media_resp = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=15)
        media_resp.raise_for_status()
    except Exception as e:
        print(f"[DOWNLOAD] {e}", flush=True)
        msg.body("⚠️ ما قدرت أحمل الصورة. جرب مرة ثانية.")
        return str(resp)

    selfie_bytes = media_resp.content
    app_ctx      = app.app_context()

    def search_and_reply():
        app_ctx.push()
        try:
            matched = find_photos_for_selfie(selfie_bytes)
            if not matched:
                body = "😕 ما لقيت صورك. تأكد أن السيلفي واضح وجرب مرة ثانية."
            else:
                links = "\n\n".join([f"📷 {l}" for l in matched[:10]])
                body  = f"✅ وجدت {len(matched)} صورة لك!\n\n{links}"
        except Exception as e:
            print(f"[SEARCH ERROR] {e}", flush=True)
            body = f"⚠️ {str(e)}"
        finally:
            app_ctx.pop()

        try:
            twilio_client.messages.create(from_=TWILIO_WHATSAPP, to=sender, body=body)
        except Exception as e:
            print(f"[TWILIO] {e}", flush=True)

    threading.Thread(target=search_and_reply, daemon=True).start()

    msg.body("🔍 جاري البحث عن صورك... سأرسل لك النتيجة خلال ثوانٍ ⏳")
    return str(resp)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
