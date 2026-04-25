import os
import io
import requests
import tempfile
from flask import Flask, request, jsonify, send_from_directory
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import base64

app = Flask(__name__, static_folder='static')

# ── Config ─────────────────────────────────────────────────────────────────
LUXAND_API_KEY     = "9fdf790f53114183b4cdece769982688"
TWILIO_SID         = "AC3c891aa3953ea2d2191a41fa9d92b583"
TWILIO_TOKEN       = "ce9cf49614219a626957e5b632e03a9e"
TWILIO_WHATSAPP    = "whatsapp:+15705256477"
GDRIVE_FOLDER_ID   = "1aifdC63hhImbS9rqP7hiheFrJhWWeOvt"
GDRIVE_CREDS_FILE  = "credentials.json"   # Google service account JSON

LUXAND_SUBJECT_ID  = "wedding_guests"     # one subject per event if needed

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

# ── Google Drive helper ─────────────────────────────────────────────────────
def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        GDRIVE_CREDS_FILE,
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)


def list_photos_in_folder(folder_id: str) -> list[dict]:
    """Return list of {id, name, webContentLink} for images in folder."""
    service = get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and mimeType contains 'image/'",
        fields="files(id, name, webContentLink, webViewLink)",
        pageSize=1000
    ).execute()
    return results.get("files", [])


def download_photo_bytes(file_id: str) -> bytes:
    service = get_drive_service()
    request_dl = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request_dl)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ── Luxand helpers ──────────────────────────────────────────────────────────
LUXAND_BASE = "https://api.luxand.cloud"

def luxand_add_photo(image_bytes: bytes, name: str = "guest") -> str:
    """Add face to Luxand, return person_id."""
    resp = requests.post(
        f"{LUXAND_BASE}/photo/v2",
        headers={"token": LUXAND_API_KEY},
        files={"photo": ("selfie.jpg", image_bytes, "image/jpeg")},
        data={"name": name, "collections": LUXAND_SUBJECT_ID, "store": "1"}
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("uuid") or data.get("person_id") or str(data)


def luxand_recognize(image_bytes: bytes) -> list[dict]:
    """Recognize faces in image, return list of matches with person_id & prob."""
    resp = requests.post(
        f"{LUXAND_BASE}/photo/search/v2",
        headers={"token": LUXAND_API_KEY},
        files={"photo": ("photo.jpg", image_bytes, "image/jpeg")},
        data={"collections": LUXAND_SUBJECT_ID}
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    # returns list of faces, each with matches
    matches = []
    for face in data:
        for match in face.get("matches", []):
            matches.append({
                "person_id": match.get("uuid") or match.get("person_id"),
                "probability": match.get("probability", 0)
            })
    return matches


# ── In-memory store: person_id → phone + name ───────────────────────────────
# In production replace with a real DB (SQLite at minimum)
guest_registry: dict[str, dict] = {}
# phone → person_id  (for WhatsApp lookup)
phone_to_person: dict[str, str] = {}


# ── Core search logic ───────────────────────────────────────────────────────
def find_photos_for_selfie(selfie_bytes: bytes) -> list[str]:
    """
    1. Add selfie as temp face
    2. For each photo in Drive, run recognition
    3. Collect matching photo links
    Returns list of webViewLink strings.
    """
    # Register the selfie temporarily
    temp_person_id = luxand_add_photo(selfie_bytes, name="temp_search")

    matched_links = []
    photos = list_photos_in_folder(GDRIVE_FOLDER_ID)

    for photo in photos:
        photo_bytes = download_photo_bytes(photo["id"])
        matches = luxand_recognize(photo_bytes)
        for m in matches:
            if m["person_id"] == temp_person_id and m["probability"] >= 0.85:
                matched_links.append(photo.get("webViewLink", ""))
                break

    return list(set(matched_links))  # deduplicate


# ── WhatsApp bot ────────────────────────────────────────────────────────────
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    sender  = request.form.get("From", "")          # whatsapp:+974xxxxxxxx
    num_media = int(request.form.get("NumMedia", 0))
    body    = request.form.get("Body", "").strip().lower()
    resp    = MessagingResponse()
    msg     = resp.message()

    if num_media == 0:
        msg.body(
            "🌙 أهلاً وسهلاً بك في قمرة\n\n"
            "نحن سعداء بوجودك معنا الليلة ✨\n\n"
            "أرسل لي *سيلفي واضح* لوجهك وسأجد لك جميع صورك من العرس خلال ثوانٍ 🎉"
        )
        return str(resp)

    # Download selfie from Twilio
    media_url  = request.form.get("MediaUrl0")
    media_resp = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN))
    selfie_bytes = media_resp.content

    msg.body("🔍 جاري البحث عن صورك... ثواني!")
    # We must reply quickly to Twilio, so send first ACK then process
    # For simplicity here we process inline (for production use a task queue)

    matched = find_photos_for_selfie(selfie_bytes)

    if not matched:
        msg.body("😕 ما لقيت صورك. تأكد أن السيلفي واضح وجرب مرة ثانية.")
    elif len(matched) == 1:
        msg.body(f"✅ وجدت صورتك!\n\n📷 {matched[0]}")
    else:
        links = "\n\n".join([f"📷 {l}" for l in matched[:10]])
        msg.body(f"✅ وجدت {len(matched)} صورة لك!\n\n{links}")

    return str(resp)


# ── Web page API ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/search", methods=["POST"])
def web_search():
    if "selfie" not in request.files:
        return jsonify({"error": "No selfie uploaded"}), 400

    selfie_file  = request.files["selfie"]
    selfie_bytes = selfie_file.read()

    matched = find_photos_for_selfie(selfie_bytes)

    if not matched:
        return jsonify({"found": 0, "photos": []})

    return jsonify({"found": len(matched), "photos": matched})


# ── Health check ────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    import os
port = int(os.environ.get("PORT", 5000))
app.run(host="0.0.0.0", port=port, debug=False)
