# نظام صور العرس 📸

## الملفات
- `app.py` — السيرفر الرئيسي (واتساب بوت + صفحة ويب)
- `sync_piufoto_to_drive.py` — سكريبت نقل الصور من PiuFoto لـ Google Drive
- `static/index.html` — صفحة الويب للضيف
- `requirements.txt` — المكتبات المطلوبة

---

## خطوات التشغيل

### 1. Google Drive credentials
1. روح console.cloud.google.com
2. اعمل Project جديد
3. فعّل Google Drive API
4. اعمل Service Account وحمّل credentials.json
5. شارك فولدر Drive مع إيميل الـ Service Account

### 2. تثبيت المكتبات
```bash
pip install -r requirements.txt
```

### 3. تشغيل السيرفر
```bash
python app.py
```

### 4. Twilio Webhook
في لوحة Twilio، اضبط الـ WhatsApp webhook على:
```
https://YOUR_SERVER_URL/whatsapp
```

### 5. بعد العرس — sync الصور
```bash
python sync_piufoto_to_drive.py --album YOUR_PIUFOTO_ALBUM_ID
```

---

## التكلفة الشهرية
| الخدمة | التكلفة |
|--------|---------|
| PiuFoto Silver | $20 |
| Luxand API | $20 |
| Server (Railway/DigitalOcean) | $10 |
| Twilio (~250 رسالة) | ~$12 |
| **الإجمالي** | **~$62** |

---

## ملاحظة مهمة
تحتاج credentials.json من Google Cloud قبل ما يشتغل Google Drive.
