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

# ── Flask App ────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
TWILIO_SID              = os.environ["TWILIO_SID"]
TWILIO_TOKEN            = os.environ["TWILIO_TOKEN"]
TWILIO_WHATSAPP         = os.environ["TWILIO_WHATSAPP"]
FACEPP_API_KEY          = os.environ["FACEPP_API_KEY"]
FACEPP_API_SECRET       = os.environ["FACEPP_API_SECRET"]
GDRIVE_FOLDER_ID        = os.environ["GDRIVE_FOLDER_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS"]

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

# ── Google Drive ──────────────────────────────────────────────────────────────
def get_drive_service():
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)


def list_drive_photos(folder_id: str) -> list[dict]:
    service = get_drive_service()
    results, page_token = [], None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false",
            fields="nextPageToken, files(id, name, webViewLink)",
            pageSize=100,
            pageToken=page_token
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def get_drive_photo_bytes(file_id: str) -> bytes:
    service = get_drive_service()
    request_dl = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request_dl)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ── Face++ ────────────────────────────────────────────────────────────────────
FACEPP_DETECT_URL  = "https://api-us.faceplusplus.com/facepp/v3/detect"
FACEPP_COMPARE_URL = "https://api-us.faceplusplus.com/facepp/v3/compare"
MATCH_THRESHOLD    = 75


def detect_face_token(image_bytes: bytes) -> str | None:
    resp = requests.post(
        FACEPP_DETECT_URL,
        data={"api_key": FACEPP_API_KEY, "api_secret": FACEPP_API_SECRET},
        files={"image_file": ("selfie.jpg", image_bytes, "image/jpeg")},
        timeout=15
    )
    resp.raise_for_status()
    faces = resp.json().get("faces", [])
    return faces[0]["face_token"] if faces else None


def compare_faces(token1: str, image_bytes: bytes) -> float:
    resp = requests.post(
        FACEPP_COMPARE_URL,
        data={
            "api_key": FACEPP_API_KEY,
            "api_secret": FACEPP_API_SECRET,
            "face_token1": token1,
        },
        files={"image_file2": ("photo.jpg", image_bytes, "image/jpeg")},
        timeout=15
    )
    resp.raise_for_status()
    return resp.json().get("confidence", 0)


# ── Core Logic ────────────────────────────────────────────────────────────────
def find_photos_for_selfie(selfie_bytes: bytes) -> list[str]:
    face_token = detect_face_token(selfie_bytes)
    if not face_token:
        raise ValueError("ما لقيت وجه في الصورة")

    photos = list_drive_photos(GDRIVE_FOLDER_ID)
    matched_links = []

    for photo in photos:
        try:
            photo_bytes = get_drive_photo_bytes(photo["id"])
            confidence  = compare_faces(face_token, photo_bytes)
            if confidence >= MATCH_THRESHOLD:
                matched_links.append(photo["webViewLink"])
        except Exception as e:
            print(f"[SKIP] {photo['name']}: {e}", flush=True)

    return matched_links


# ── WhatsApp Webhook ──────────────────────────────────────────────────────────
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
        media_resp = requests.get(
            media_url,
            auth=(TWILIO_SID, TWILIO_TOKEN),
            timeout=15
        )
        media_resp.raise_for_status()
    except Exception as e:
        print(f"[MEDIA DOWNLOAD ERROR] {e}", flush=True)
        msg.body("⚠️ ما قدرت أحمل الصورة. جرب مرة ثانية.")
        return str(resp)

    selfie_bytes = media_resp.content
    app_ctx = app.app_context()

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
            print(f"[SEARCH ERROR] {str(e)}", flush=True)
            body = f"⚠️ خطأ أثناء البحث: {str(e)}"
        finally:
            app_ctx.pop()

        try:
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP,
                to=sender,
                body=body
            )
        except Exception as e:
            print(f"[TWILIO SEND ERROR] {str(e)}", flush=True)

    thread = threading.Thread(target=search_and_reply)
    thread.daemon = True
    thread.start()

    msg.body("🔍 جاري البحث عن صورك... سأرسل لك النتيجة خلال دقيقة ⏳")
    return str(resp)


# ── Health Check ──────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "قمرة 🌙 — running", 200


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
