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

LUXAND_API_KEY    = os.environ.get("LUXAND_API_KEY", "9fdf790f53114183b4cdece769982688")
TWILIO_SID        = os.environ.get("TWILIO_SID", "AC3c891aa3953ea2d2191a41fa9d92b583")
TWILIO_TOKEN      = os.environ.get("TWILIO_TOKEN", "ce9cf49614219a626957e5b632e03a9e")
TWILIO_WHATSAPP   = os.environ.get("TWILIO_WHATSAPP", "whatsapp:+14155238886")
GDRIVE_FOLDER_ID  = os.environ.get("GDRIVE_FOLDER_ID", "1aifdC63hhImbS9rqP7hiheFrJhWWeOvt")
LUXAND_BASE       = "https://api.luxand.cloud"
LUXAND_SUBJECT_ID = "wedding_guests"

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

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

def luxand_add_photo(image_bytes, name="guest"):
    resp = requests.post(
        f"{LUXAND_BASE}/photo/v2",
        headers={"token": LUXAND_API_KEY},
        files={"photo": ("selfie.jpg", image_bytes, "image/jpeg")},
        data={"name": name, "collections": LUXAND_SUBJECT_ID, "store": "1"}
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("uuid") or data.get("person_id") or str(data)

def luxand_recognize(image_bytes):
    resp = requests.post(
        f"{LUXAND_BASE}/photo/search/v2",
        headers={"token": LUXAND_API_KEY},
        files={"photo": ("photo.jpg", image_bytes, "image/jpeg")},
        data={"collections": LUXAND_SUBJECT_ID}
    )
    if resp.status_code != 200:
        return []
    matches = []
    for face in resp.json():
        for match in face.get("matches", []):
            matches.append({
                "person_id": match.get("uuid") or match.get("person_id"),
                "probability": match.get("probability", 0)
            })
    return matches

def find_photos_for_selfie(selfie_bytes):
    temp_person_id = luxand_add_photo(selfie_bytes, name="temp_search")
    matched_links = []
    photos = list_photos_in_folder(GDRIVE_FOLDER_ID)
    for photo in photos:
        photo_bytes = download_photo_bytes(photo["id"])
        for m in luxand_recognize(photo_bytes):
            if m["person_id"] == temp_person_id and m["probability"] >= 0.85:
                matched_links.append(photo.get("webViewLink", ""))
                break
    return list(set(matched_links))

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
        msg.body("⚠️ حدث خطأ. حاول مرة ثانية.")
    return str(resp)

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
