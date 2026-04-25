"""
sync_piufoto_to_drive.py
────────────────────────
بعد العرس — اضغط زر وحد وكل الصور تنتقل من PiuFoto لـ Google Drive.

الاستخدام:
    python sync_piufoto_to_drive.py --album ALBUM_ID
"""

import os
import sys
import argparse
import requests
import io
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ── Config ──────────────────────────────────────────────────────────────────
GDRIVE_FOLDER_ID  = "1aifdC63hhImbS9rqP7hiheFrJhWWeOvt"
GDRIVE_CREDS_FILE = "credentials.json"

# PiuFoto — يحتاج token بعد ما تسجل دخول في التطبيق
# روح Settings → API في التطبيق وانسخ الـ token
PIUFOTO_TOKEN = os.getenv("PIUFOTO_TOKEN", "YOUR_PIUFOTO_TOKEN_HERE")
PIUFOTO_BASE  = "https://api.piufoto.com"   # endpoint رسمي، يتأكد من الدوكيومنتيشن


# ── Google Drive ─────────────────────────────────────────────────────────────
def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        GDRIVE_CREDS_FILE,
        scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    return build("drive", "v3", credentials=creds)


def upload_to_drive(service, filename: str, image_bytes: bytes, folder_id: str):
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/jpeg")
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink"
    ).execute()
    return file


# ── PiuFoto ──────────────────────────────────────────────────────────────────
def get_piufoto_photos(album_id: str) -> list[dict]:
    """جيب كل الصور من ألبوم محدد."""
    headers = {"Authorization": f"Bearer {PIUFOTO_TOKEN}"}
    resp = requests.get(
        f"{PIUFOTO_BASE}/albums/{album_id}/photos",
        headers=headers
    )
    resp.raise_for_status()
    return resp.json().get("photos", [])


def download_piufoto_photo(photo_url: str) -> bytes:
    resp = requests.get(photo_url)
    resp.raise_for_status()
    return resp.content


# ── Main ──────────────────────────────────────────────────────────────────────
def sync(album_id: str):
    print(f"🔄 بداية sync من PiuFoto album: {album_id}")
    drive = get_drive_service()

    photos = get_piufoto_photos(album_id)
    print(f"📸 عدد الصور: {len(photos)}")

    success = 0
    for i, photo in enumerate(photos, 1):
        try:
            url      = photo.get("url") or photo.get("original_url")
            filename = photo.get("filename") or f"photo_{i:04d}.jpg"

            print(f"  [{i}/{len(photos)}] تحميل {filename}...")
            img_bytes = download_piufoto_photo(url)

            result = upload_to_drive(drive, filename, img_bytes, GDRIVE_FOLDER_ID)
            print(f"  ✅ رُفع: {result['webViewLink']}")
            success += 1

        except Exception as e:
            print(f"  ❌ خطأ في {photo}: {e}")

    print(f"\n✅ اكتمل: {success}/{len(photos)} صورة رُفعت على Google Drive")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PiuFoto → Google Drive Sync")
    parser.add_argument("--album", required=True, help="PiuFoto Album ID")
    args = parser.parse_args()
    sync(args.album)
