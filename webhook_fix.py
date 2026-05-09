import io
import json
import os
import threading

import requests
from dotenv import load_dotenv
from flask import Flask, request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

TWILIO_SID = os.environ["TWILIO_SID"]
TWILIO_TOKEN = os.environ["TWILIO_TOKEN"]
TWILIO_WHATSAPP = os.environ["TWILIO_WHATSAPP"]
DRIVE_FOLDER_ID = os.environ["GDRIVE_FOLDER_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS"]  # full service account JSON as string
FACEPP_API_KEY = os.environ["FACEPP_API_KEY"]
FACEPP_API_SECRET = os.environ["FACEPP_API_SECRET"]
FACEPP_FACESET_TOKEN = os.environ.get("FACEPP_FACESET_TOKEN", "")  # created once, then stored here
REFRESH_SECRET = os.environ.get("REFRESH_SECRET", "")

FACEPP_BASE = "https://api-us.faceplusplus.com/facepp/v3"
FACEMAP_FILE = "facemap.json"   # face_token -> drive_file_id mapping

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
app = Flask(__name__)


# ── Google Drive helpers ──────────────────────────────────────────────────────

def _get_drive_service():
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=scopes)
    return build("drive", "v3", credentials=creds)


def _list_drive_images(service) -> list[dict]:
    results, page_token = [], None
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false and mimeType contains 'image/'"
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
            pageSize=200,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def _download_image_bytes(service, file_id: str) -> bytes:
    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


# ── Face++ helpers ────────────────────────────────────────────────────────────

def _facepp_auth() -> dict:
    return {"api_key": FACEPP_API_KEY, "api_secret": FACEPP_API_SECRET}


def _detect_face_token(image_bytes: bytes) -> str | None:
    """Detect a face in image_bytes and return its face_token, or None."""
    resp = requests.post(
        f"{FACEPP_BASE}/detect",
        data=_facepp_auth(),
        files={"image_file": ("image.jpg", image_bytes)},
    ).json()
    faces = resp.get("faces", [])
    return faces[0]["face_token"] if faces else None


def _ensure_faceset() -> str:
    """Return the FaceSet token, creating one if FACEPP_FACESET_TOKEN is not set."""
    if FACEPP_FACESET_TOKEN:
        return FACEPP_FACESET_TOKEN
    resp = requests.post(
        f"{FACEPP_BASE}/faceset/create",
        data={**_facepp_auth(), "display_name": "wedding_photos"},
    ).json()
    token = resp["faceset_token"]
    print(f"Created FaceSet: {token} — add FACEPP_FACESET_TOKEN={token} to your .env", flush=True)
    return token


def _load_facemap() -> dict:
    if os.path.exists(FACEMAP_FILE):
        with open(FACEMAP_FILE) as f:
            return json.load(f)
    return {}


def _save_facemap(facemap: dict):
    with open(FACEMAP_FILE, "w") as f:
        json.dump(facemap, f)


# ── Indexing ──────────────────────────────────────────────────────────────────

def build_index():
    """Index all Drive photos into Face++ FaceSet. Skips already-indexed files."""
    service = _get_drive_service()
    files = _list_drive_images(service)
    faceset_token = _ensure_faceset()
    facemap = _load_facemap()

    indexed_drive_ids = set(facemap.values())
    new_faces = []

    for f in files:
        file_id = f["id"]
        if file_id in indexed_drive_ids:
            continue
        try:
            img_bytes = _download_image_bytes(service, file_id)
            face_token = _detect_face_token(img_bytes)
            if face_token:
                new_faces.append((face_token, file_id))
                print(f"Indexed: {f['name']}", flush=True)
            else:
                print(f"No face found in: {f['name']}", flush=True)
        except Exception as e:
            print(f"Skipping {f['name']}: {e}", flush=True)

    # Add new face tokens to FaceSet in batches of 5 (Face++ limit per call)
    for i in range(0, len(new_faces), 5):
        batch = new_faces[i:i+5]
        tokens = ",".join(ft for ft, _ in batch)
        requests.post(
            f"{FACEPP_BASE}/faceset/addface",
            data={**_facepp_auth(), "faceset_token": faceset_token, "face_tokens": tokens},
        )
        for face_token, file_id in batch:
            facemap[face_token] = file_id

    _save_facemap(facemap)
    print(f"Index complete: {len(facemap)} faces total", flush=True)
    return facemap


# ── Core matching ─────────────────────────────────────────────────────────────

def find_photos_for_selfie(selfie_bytes: bytes) -> list[str]:
    """Search FaceSet for selfie face and return matching Drive links."""
    face_token = _detect_face_token(selfie_bytes)
    if not face_token:
        raise ValueError("No face detected in the selfie")

    faceset_token = _ensure_faceset()
    resp = requests.post(
        f"{FACEPP_BASE}/search",
        data={
            **_facepp_auth(),
            "face_token": face_token,
            "faceset_token": faceset_token,
            "return_result_count": 50,
        },
    ).json()

    results = resp.get("results", [])
    facemap = _load_facemap()

    matched_drive_ids = set()
    for r in results:
        if r["confidence"] >= 76:   # Face++ recommends 76+ as "same person"
            drive_id = facemap.get(r["face_token"])
            if drive_id:
                matched_drive_ids.add(drive_id)

    return [f"https://drive.google.com/file/d/{fid}/view" for fid in matched_drive_ids]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/refresh-cache", methods=["POST"])
def refresh_cache():
    secret = request.args.get("secret", "")
    if REFRESH_SECRET and secret != REFRESH_SECRET:
        return {"error": "unauthorized"}, 401

    def rebuild():
        try:
            build_index()
        except Exception as e:
            print(f"Index rebuild failed: {e}", flush=True)

    threading.Thread(target=rebuild).start()
    return {"status": "indexing new photos in background"}, 202


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

    sender = request.form.get("From", "")
    media_url = request.form.get("MediaUrl0")
    media_resp = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN))
    selfie_bytes = media_resp.content

    def search_and_reply():
        try:
            matched = find_photos_for_selfie(selfie_bytes)
            if not matched:
                body = "😕 ما لقيت صورك. تأكد أن السيلفي واضح وجرب مرة ثانية."
            else:
                links = "\n\n".join([f"📷 {l}" for l in matched])
                body = f"✅ وجدت {len(matched)} صورة لك!\n\n{links}"
        except Exception as e:
            print(f"ERROR: {str(e)}", flush=True)
            body = f"⚠️ خطأ: {str(e)}"

        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP,
            to=sender,
            body=body
        )

    threading.Thread(target=search_and_reply).start()

    msg.body("🔍 جاري البحث عن صورك... سأرسل لك النتيجة خلال دقيقة ⏳")
    return str(resp)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
