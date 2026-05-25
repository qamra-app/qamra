# Qamra — API Keys & Environment Variables

## Railway Environment Variables

Set these in Railway → your project → Variables:

| Variable | Description | Where to get it |
|---|---|---|
| `TWILIO_SID` | Twilio Account SID | twilio.com → Console → Account Info |
| `TWILIO_TOKEN` | Twilio Auth Token | twilio.com → Console → Account Info |
| `TWILIO_WHATSAPP` | Your WhatsApp sender number | Format: `whatsapp:+14155238886` |
| `FACEPP_API_KEY` | Face++ API Key | console.faceplusplus.com → API Key |
| `FACEPP_API_SECRET` | Face++ API Secret | console.faceplusplus.com → API Key |
| `GDRIVE_FOLDER_ID` | Google Drive folder ID | From the Drive folder URL: `drive.google.com/drive/folders/FOLDER_ID_HERE` |
| `GOOGLE_CREDENTIALS` | Full service account JSON (paste entire JSON as one line) | Google Cloud Console → IAM → Service Accounts → Keys → JSON |
| `FACEPP_FACESET_TOKEN` | FaceSet token (filled after first /index run) | Printed in Railway logs after POST /index |

---

## Step-by-Step Setup

### 1. After deploying to Railway

POST to your app to start indexing:
```
POST https://qamra-production.up.railway.app/index
```
(Use Postman, curl, or browser fetch)

### 2. Check Railway logs for:
```
[FACESET] Created: abc123token — add FACEPP_FACESET_TOKEN=abc123token to Railway variables
```
Copy that token and add it as `FACEPP_FACESET_TOKEN` in Railway variables.

### 3. Wait for indexing to finish
Logs will show:
```
[INDEX] Done. Total faces indexed: 87
```

### 4. Health check
```
GET https://qamra-production.up.railway.app/
```
Returns: `قمرة 🌙 — running | 87 faces indexed`

### 5. Test WhatsApp
Send a selfie to your Twilio WhatsApp sandbox number. You should get results back in seconds.

---

## Face++ API Endpoints Used

| Endpoint | URL |
|---|---|
| Detect face | `POST https://api-us.faceplusplus.com/facepp/v3/detect` |
| Create FaceSet | `POST https://api-us.faceplusplus.com/facepp/v3/faceset/create` |
| Add faces to FaceSet | `POST https://api-us.faceplusplus.com/facepp/v3/faceset/addface` |
| Search by face | `POST https://api-us.faceplusplus.com/facepp/v3/search` |

Match threshold: **76** (confidence score 0–100, 76+ = same person)

---

## Google Drive Service Account Setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project (or use existing)
3. Enable **Google Drive API**
4. Go to **IAM & Admin → Service Accounts → Create**
5. Download the JSON key
6. Share your Drive folder with the service account email (e.g. `qamra@project.iam.gserviceaccount.com`) — give it **Viewer** access
7. Paste the entire JSON content into the `GOOGLE_CREDENTIALS` Railway variable

---

## Re-indexing

If you add new photos to the Drive folder, POST `/index` again — it skips already-indexed photos and only processes new ones.
