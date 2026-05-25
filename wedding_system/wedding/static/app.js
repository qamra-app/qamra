/* ═══════════════════════════════════════════════════════════
   QAMRA Kiosk — App Logic
   Screens: welcome → whatsapp → selfie → processing → results → photo
═══════════════════════════════════════════════════════════ */

const App = (() => {
  const RESET_SEC = 60;

  const COUNTRIES = [
    { dial: "+974", flag: "🇶🇦", name: "قطر",      digits: 8 },
    { dial: "+966", flag: "🇸🇦", name: "السعودية", digits: 9 },
    { dial: "+971", flag: "🇦🇪", name: "الإمارات", digits: 9 },
    { dial: "+965", flag: "🇰🇼", name: "الكويت",   digits: 8 },
    { dial: "+973", flag: "🇧🇭", name: "البحرين",  digits: 8 },
    { dial: "+968", flag: "🇴🇲", name: "عُمان",    digits: 8 },
  ];

  let country     = COUNTRIES[0]; // Qatar default
  let phone       = "";
  let stream      = null;
  let matches     = [];
  let faceKey     = "";
  let folderUrl   = "";
  let sessionId   = "";
  let qr          = null;
  let driveQr     = null;
  let folderPoll  = null;
  let resetTimer  = null;
  let countTimer  = null;
  let history     = [];

  // ── Screens ────────────────────────────────────────────
  function show(id) {
    document.querySelectorAll(".screen").forEach(s => s.classList.remove("active"));
    document.getElementById(id).classList.add("active");
  }

  function push(id) {
    history.push(id);
    show(id);
  }

  function start() {
    phone   = "";
    country = COUNTRIES[0];
    history = ["screen-welcome"];
    refreshCountryDisplay();
    refreshPhoneDisplay();
    push("screen-whatsapp");
  }

  function goBack() {
    stopCamera();
    history.pop();
    const prev = history[history.length - 1] || "screen-welcome";
    show(prev);
  }

  function backToResults() {
    clearCountdown();
    show("screen-results");
  }

  function reset() {
    clearCountdown();
    stopCamera();
    phone     = "";
    matches   = [];
    faceKey   = "";
    folderUrl = "";
    sessionId = "";
    history   = [];
    country   = COUNTRIES[0];
    if (folderPoll) { clearInterval(folderPoll); folderPoll = null; }
    if (qr)         { try { qr.clear();      } catch(_) {} qr      = null; }
    if (driveQr)    { try { driveQr.clear(); } catch(_) {} driveQr = null; }
    document.getElementById("results-grid").innerHTML  = "";
    document.getElementById("qr-box").innerHTML        = "";
    document.getElementById("drive-qr-box").innerHTML  = "";
    document.getElementById("drive-bar").style.display = "none";
    show("screen-welcome");
  }

  // ── Country Picker ─────────────────────────────────────
  function buildCountryList() {
    const list = document.getElementById("country-list");
    list.innerHTML = "";
    COUNTRIES.forEach(c => {
      const btn = document.createElement("button");
      btn.className = "country-option" + (c.dial === country.dial ? " selected" : "");
      btn.innerHTML = `
        <span class="flag">${c.flag}</span>
        <span class="info">
          <span class="name">${c.name}</span>
          <span class="code">${c.dial}</span>
        </span>`;
      btn.addEventListener("click", e => { e.stopPropagation(); selectCountry(c); });
      list.appendChild(btn);
    });
  }

  function openCountryPicker() {
    buildCountryList();
    document.getElementById("country-picker").classList.add("open");
  }

  function closeCountryPicker(e) {
    if (!e || e.target === document.getElementById("country-picker")) {
      document.getElementById("country-picker").classList.remove("open");
    }
  }

  function selectCountry(c) {
    country = c;
    phone   = "";
    refreshCountryDisplay();
    refreshPhoneDisplay();
    closeCountryPicker();
  }

  function refreshCountryDisplay() {
    document.getElementById("selected-flag").textContent = country.flag;
    document.getElementById("selected-dial").textContent = country.dial;
  }

  // ── Phone Numpad ───────────────────────────────────────
  function digit(d) {
    if (phone.length >= country.digits) return;
    phone += d;
    refreshPhoneDisplay();
  }

  function del() {
    phone = phone.slice(0, -1);
    refreshPhoneDisplay();
  }

  function refreshPhoneDisplay() {
    document.getElementById("phone-digits").textContent = phone.padEnd(country.digits, "_");
    document.getElementById("btn-ok").disabled          = phone.length < country.digits;
  }

  async function confirmPhone() {
    if (phone.length < country.digits) return;
    await openCamera();
    push("screen-selfie");
  }

  // ── Camera ─────────────────────────────────────────────
  async function openCamera() {
    const video = document.getElementById("cam");
    const strategies = [
      { facingMode: { ideal: "user" }, width: { ideal: 1280 }, height: { ideal: 1280 } },
      { width: { ideal: 1280 }, height: { ideal: 1280 } },
      true,
    ];

    let lastError = null;
    for (const constraint of strategies) {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: constraint, audio: false });
        break;
      } catch (e) {
        lastError = e;
      }
    }

    if (!stream) {
      console.error("[CAM]", lastError);
      alert("تعذّر تشغيل الكاميرا. تأكد من منح الصلاحيات في المتصفح.");
      return;
    }

    video.srcObject = stream;
    try {
      await video.play();
    } catch (e) {
      console.error("[CAM play]", e);
    }
  }

  function stopCamera() {
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    const cam = document.getElementById("cam");
    if (cam) cam.srcObject = null;
  }

  function snap() {
    const video  = document.getElementById("cam");
    const canvas = document.getElementById("snap-canvas");
    canvas.width  = video.videoWidth  || 640;
    canvas.height = video.videoHeight || 640;
    const ctx = canvas.getContext("2d");
    ctx.translate(canvas.width, 0);
    ctx.scale(-1, 1);
    ctx.drawImage(video, 0, 0);
    stopCamera();
    push("screen-processing");
    canvas.toBlob(blob => search(blob), "image/jpeg", 0.92);
  }

  // ── Face Search ────────────────────────────────────────
  async function search(blob) {
    try {
      const form = new FormData();
      form.append("photo", blob, "selfie.jpg");
      const res  = await fetch("/api/match", { method: "POST", body: form });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      matches   = (data.matches || []).slice(0, 21);
      faceKey   = data.face_path || "";
      folderUrl = data.folder_url || "";
      sessionId = data.session_id || "";
      await logGuest();
      renderResults();
    } catch (e) {
      console.error("[SEARCH]", e);
      alert("حدث خطأ في الاتصال. سنعود للكاميرا — حاول مرة ثانية.");
      await openCamera();
      show("screen-selfie");
    }
  }

  // ── Results ────────────────────────────────────────────
  function renderResults() {
    const grid  = document.getElementById("results-grid");
    const label = document.getElementById("results-label");
    grid.innerHTML = "";

    if (matches.length === 0) {
      label.textContent = "لم نجد صورك";
      grid.innerHTML = `
        <div class="no-results">
          <span class="nr-icon">😕</span>
          <p>ما لقينا صورك هذي المرة</p>
          <small>تأكد أن السيلفي واضح وحاول مرة ثانية</small>
        </div>`;
    } else {
      label.textContent = `وجدنا ${matches.length} صورة`;
      matches.forEach((m, i) => {
        const card = document.createElement("div");
        card.className = "photo-card";
        card.innerHTML = `
          <img src="${m.url}" loading="lazy" alt="صورة ${i + 1}"
               onerror="this.style.opacity='0.3'">
          <div class="conf-pill">100%</div>`;
        card.addEventListener("click", () => showPhoto(m));
        grid.appendChild(card);

        // Animate confidence from 100% down to actual value
        const pill   = card.querySelector(".conf-pill");
        const target = Math.round(m.confidence);
        let current  = 100;
        const delay  = i * 120;
        setTimeout(() => {
          const step = setInterval(() => {
            current--;
            pill.textContent = current + "%";
            if (current <= target) clearInterval(step);
          }, 18);
        }, delay);
      });
    }

    // Drive QR — show instantly if folder_url returned, else poll session_id
    if (folderPoll) { clearInterval(folderPoll); folderPoll = null; }
    const driveBar = document.getElementById("drive-bar");
    const driveBox = document.getElementById("drive-qr-box");
    driveBox.innerHTML = "";
    if (driveQr) { try { driveQr.clear(); } catch(_) {} driveQr = null; }

    function renderDriveQr(url) {
      driveBox.innerHTML = "";
      if (driveQr) { try { driveQr.clear(); } catch(_) {} driveQr = null; }
      driveQr = new QRCode(driveBox, {
        text:         url,
        width:        120,
        height:       120,
        colorDark:    "#1A1612",
        colorLight:   "#FAF6EC",
        correctLevel: QRCode.CorrectLevel.M,
      });
      driveBar.style.display = "flex";
    }

    if (folderUrl && matches.length > 0) {
      renderDriveQr(folderUrl);
    } else if (sessionId && matches.length > 0) {
      driveBar.style.display = "none";
      let attempts = 0;
      folderPoll = setInterval(async () => {
        attempts++;
        if (attempts > 30) { clearInterval(folderPoll); folderPoll = null; return; }
        try {
          const r = await fetch(`/api/folder-status/${sessionId}`);
          const d = await r.json();
          if (d.status === "ready" && d.folder_url) {
            clearInterval(folderPoll); folderPoll = null;
            folderUrl = d.folder_url;
            renderDriveQr(folderUrl);
          }
        } catch (_) {}
      }, 3000);
    } else {
      driveBar.style.display = "none";
    }

    push("screen-results");
  }

  // ── Photo Detail + QR ──────────────────────────────────
  function showPhoto(match) {
    document.getElementById("full-photo").src = match.url;

    const dlBtn = document.getElementById("dl-btn");
    dlBtn.href  = match.url;
    dlBtn.setAttribute("download", match.name || "qamra-photo.jpg");

    const box = document.getElementById("qr-box");
    box.innerHTML = "";
    if (qr) { try { qr.clear(); } catch(_) {} }
    const size = Math.min(Math.round(window.innerWidth * 0.52), 280);
    qr = new QRCode(box, {
      text:         match.url,
      width:        size,
      height:       size,
      colorDark:    "#1A1612",
      colorLight:   "#FAF6EC",
      correctLevel: QRCode.CorrectLevel.M,
    });

    push("screen-photo");
    startCountdown();
  }

  // ── Countdown / Auto-reset ─────────────────────────────
  function startCountdown() {
    clearCountdown();
    let remaining = RESET_SEC;
    document.getElementById("countdown").textContent = remaining;

    const bar = document.getElementById("reset-bar");
    bar.style.transition = "none";
    bar.style.width      = "100%";
    requestAnimationFrame(() => {
      bar.style.transition = `width ${RESET_SEC}s linear`;
      bar.style.width      = "0%";
    });

    countTimer = setInterval(() => {
      remaining--;
      const el = document.getElementById("countdown");
      if (el) el.textContent = remaining;
      if (remaining <= 0) reset();
    }, 1000);

    resetTimer = setTimeout(reset, RESET_SEC * 1000 + 200);
  }

  function clearCountdown() {
    clearInterval(countTimer);
    clearTimeout(resetTimer);
    countTimer = resetTimer = null;
    const bar = document.getElementById("reset-bar");
    if (bar) { bar.style.transition = "none"; bar.style.width = "100%"; }
  }

  // ── Guest Logging ──────────────────────────────────────
  async function logGuest() {
    try {
      await fetch("/api/log-guest", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          whatsapp:  country.dial + phone,
          photos:    matches.map(m => ({ url: m.url, confidence: m.confidence })),
          face_path: faceKey,
        }),
      });
    } catch (_) { /* non-critical */ }
  }

  // ── Public API ─────────────────────────────────────────
  return { start, goBack, backToResults, reset, digit, del, confirmPhone, snap, openCountryPicker, closeCountryPicker };
})();
