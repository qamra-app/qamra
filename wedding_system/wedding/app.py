import os
import io
import json
import requests
from flask import Flask, request, jsonify, send_from_directory
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

app = Flask(__name__, static_folder='static')

# ── Config ───────────────────────────────────────────────────────────────────
TWILIO_SID       = os.environ.get("TWILIO_SID", "AC3c891aa3953ea2d2191a41fa9d92b583")
TWILIO_TOKEN     = os.environ.get("TWILIO_TOKEN", "ce9cf49614219a626957e5b632e03a9e")
TWILIO_WHATSAPP  = os.environ.get("TWILIO_WHATSAPP", "whatsapp:+14155238886")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "1aifdC63hhImbS9rqP7hiheFrJhWWeOvt")
FACEPP_KEY       = os.environ.get("FACEPP_KEY", "B6mb_5WgXHyKROpziREqSL30JRz5THHO")
FACEPP_SECRET    = os.environ.get("FACEPP_SECRET", "ym8vY2OMeOiHp1IKYdTh_YANE7Z4Jy8e")
FACEPP_BASE      = "https://api-us.faceplusplus.com/facepp/v3"
FACESET_TOKEN    = os.environ.get("FACESET_TOKEN", "")

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)


# ── Face++ helpers ────────────────────────────────────────────────────────────
def facepp_detect(image_bytes):
    """Detect faces in image, return list of face_tokens."""
    resp = requests.post(
        f"{FACEPP_BASE}/detect",
        data={"api_key": FACEPP_KEY, "api_secret": FACEPP_SECRET},
        files={"image_file": ("photo.jpg", image_bytes, "image/jpeg")}
    )
    print(f"Detect: {resp.status_code} {resp.text[:200]}", flush=True)
    if resp.status_code != 200:
        return []
    return [f["face_token"] for f in resp.json().get("faces", [])]


def facepp_compare(face_token1, image_bytes):
    """Compare face_token1 against all faces in image_bytes. Returns max similarity."""
    resp = requests.post(
        f"{FACEPP_BASE}/compare",
        data={
            "api_key": FACEPP_KEY,
            "api_secret": FACEPP_SECRET,
            "face_token1": face_token1,
        },
        files={"image_file2": ("photo.jpg", image_bytes, "image/jpeg")}
    )
    print(f"Compare: {resp.status_code} {resp.text[:200]}", flush=True)
    if resp.status_code != 200:
        return 0
    return resp.json().get("confidence", 0)


# ── Google Drive ──────────────────────────────────────────────────────────────
def get_drive_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            "credentials.json",
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
    return build("drive", "v3", credentials=creds)


def list_photos_in_folder(folder_id):
    service = get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and mimeType contains 'image/'",
        fields="files(id, name, webViewLink)",
        pageSize=1000
    ).execute()
    return results.get("files", [])


def download_photo_bytes(file_id):
    service = get_drive_service()
    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ── Core logic ────────────────────────────────────────────────────────────────
def find_photos_for_selfie(selfie_bytes):
    # 1. Detect face in selfie
    selfie_faces = facepp_detect(selfie_bytes)
    if not selfie_faces:
        raise ValueError("ما قدرت أتعرف على وجه في السيلفي — تأكد أن الوجه واضح")
    
    selfie_face_token = selfie_faces[0]
    print(f"Selfie face token: {selfie_face_token}", flush=True)

    matched_links = []
    photos = list_photos_in_folder(GDRIVE_FOLDER_ID)
    print(f"Searching {len(photos)} photos in Drive", flush=True)

    for photo in photos:
        try:
            photo_bytes = download_photo_bytes(photo["id"])
            confidence = facepp_compare(selfie_face_token, photo_bytes)
            print(f"Photo {photo['name']}: confidence={confidence}", flush=True)
            if confidence >= 70:
                print(f"✅ MATCH: {photo['name']} ({confidence}%)", flush=True)
                matched_links.append(photo.get("webViewLink", ""))
        except Exception as e:
            print(f"Error on {photo['name']}: {e}", flush=True)

    return list(set(matched_links))


# ── WhatsApp bot ──────────────────────────────────────────────────────────────
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    num_media = int(request.form.get("NumMedia", 0))
    resp = MessagingResponse()
    msg = resp.message()

    if num_media == 0:
        msg.body(
            "🌙 أهلاً وسهلاً بك في قمرة\n\n"
            "نحن سعداء بوجودك معنا الليلة ✨\n\n"
            "أرسل لي *سيلفي واضح* لوجهك وسأجد لك جميع صورك من العرس خلال ثوانٍ 🎉"
        )
        return str(resp)

    media_url = request.form.get("MediaUrl0")
    media_resp = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN))
    selfie_bytes = media_resp.content

    try:
        matched = find_photos_for_selfie(selfie_bytes)
        if not matched:
            msg.body("😕 ما لقيت صورك. تأكد أن السيلفي واضح وجرب مرة ثانية.")
        else:
            links = "\n\n".join([f"📷 {l}" for l in matched[:10]])
            msg.body(f"✅ وجدت {len(matched)} صورة لك!\n\n{links}")
    except Exception as e:
        print(f"ERROR: {str(e)}", flush=True)
        msg.body(f"⚠️ خطأ: {str(e)}")

    return str(resp)


# ── Web page ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/search", methods=["POST"])
def web_search():
    if "selfie" not in request.files:
        return jsonify({"error": "No selfie uploaded"}), 400
    selfie_bytes = request.files["selfie"].read()
    try:
        matched = find_photos_for_selfie(selfie_bytes)
        return jsonify({"found": len(matched), "photos": matched})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
