import threading

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

    # رد فوري
    sender = request.form.get("From", "")
    media_url = request.form.get("MediaUrl0")
    media_resp = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN))
    selfie_bytes = media_resp.content

    # شغّل البحث في الخلفية
    def search_and_reply():
        try:
            matched = find_photos_for_selfie(selfie_bytes)
            if not matched:
                body = "😕 ما لقيت صورك. تأكد أن السيلفي واضح وجرب مرة ثانية."
            else:
                links = "\n\n".join([f"📷 {l}" for l in matched[:10]])
                body = f"✅ وجدت {len(matched)} صورة لك!\n\n{links}"
        except Exception as e:
            print(f"ERROR: {str(e)}", flush=True)
            body = f"⚠️ خطأ: {str(e)}"

        # بعث الرسالة عبر Twilio REST API
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP,
            to=sender,
            body=body
        )

    thread = threading.Thread(target=search_and_reply)
    thread.start()

    # رد فوري لـ Twilio
    msg.body("🔍 جاري البحث عن صورك... سأرسل لك النتيجة خلال دقيقة ⏳")
    return str(resp)
