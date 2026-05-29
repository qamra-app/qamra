/* ═══════════════════════════════════════════════════════════
   QAMRA Kiosk — App Logic
   Screens: welcome → selfie → processing → results → photo
═══════════════════════════════════════════════════════════ */

const App = (() => {
  const RESET_SEC  = 60;
  const EVENT_CODE = new URLSearchParams(window.location.search).get("event") || "NAMLAAN";

  let stream      = null;
  let faceApiReady      = false;
  let stopFaceLoop      = null;
  let matches     = [];
  let faceKey     = "";
  let folderUrl   = "";
  let qr          = null;
  let resetTimer    = null;
  let countTimer    = null;
  let history       = [];
  let renderedCount = 0;
  const PAGE_SIZE   = 20;

  // ── Screens ────────────────────────────────────────────
  function show(id) {
    document.querySelectorAll(".screen").forEach(s => s.classList.remove("active"));
    document.getElementById(id).classList.add("active");
  }

  function push(id) {
    history.push(id);
    show(id);
  }

  async function start() {
    history = ["screen-welcome"];
    push("screen-selfie");
    await openCamera();
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
    matches   = [];
    faceKey   = "";
    folderUrl = "";
    history   = [];
    if (qr) { try { qr.clear(); } catch(_) {} qr = null; }
    document.getElementById("results-grid").innerHTML = "";
    document.getElementById("qr-box").innerHTML       = "";
    show("screen-welcome");
  }

  // ── Face Detection ─────────────────────────────────────
  async function initFaceApi() {
    if (faceApiReady || !window.faceapi) return;
    try {
      await faceapi.nets.tinyFaceDetector.loadFromUri(
        "https://cdn.jsdelivr.net/npm/@vladmandic/face-api/model/"
      );
      faceApiReady = true;
    } catch (e) {
      console.warn("[FACE] model load failed:", e);
    }
  }

  function startFaceDetection() {
    if (!faceApiReady || !window.faceapi) return;
    let active = true;
    stopFaceLoop = () => { active = false; };

    const video  = document.getElementById("cam");
    const canvas = document.getElementById("face-canvas");
    const ctx    = canvas.getContext("2d");

    const loop = async () => {
      if (!active || !stream || !video.videoWidth) {
        if (active) setTimeout(loop, 100);
        return;
      }

      canvas.width  = canvas.offsetWidth;
      canvas.height = canvas.offsetHeight;
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      try {
        const dets = await faceapi.detectAllFaces(
          video,
          new faceapi.TinyFaceDetectorOptions({ inputSize: 224, scoreThreshold: 0.4 })
        );
        if (dets.length > 0 && active) {
          const det = dets.reduce((a, b) => a.score > b.score ? a : b);

          // Map video coords → canvas coords accounting for object-fit:cover.
          // No manual x-mirror needed — the .camera-inner wrapper has
          // transform:scaleX(-1) in CSS which flips both video and canvas together.
          const vdw   = canvas.width;
          const vdh   = canvas.height;
          const vw    = video.videoWidth;
          const vh    = video.videoHeight;
          const scale = Math.max(vdw / vw, vdh / vh);
          const cropX = (vw * scale - vdw) / 2;
          const cropY = (vh * scale - vdh) / 2;

          const b  = det.box;
          const ex = b.x * scale - cropX;
          const ey = b.y * scale - cropY;
          const ew = b.width  * scale;
          const eh = b.height * scale;

          const lw = Math.max(2, vdw * 0.003);
          const corner = Math.min(ew, eh) * 0.18;

          ctx.strokeStyle = "#4ade80";
          ctx.lineWidth   = lw;
          ctx.shadowColor = "rgba(0,0,0,0.6)";
          ctx.shadowBlur  = 6;

          // Draw corner brackets
          const cx = ex, cy = ey;
          ctx.beginPath();
          // top-left
          ctx.moveTo(cx + corner, cy); ctx.lineTo(cx, cy); ctx.lineTo(cx, cy + corner);
          // top-right
          ctx.moveTo(cx + ew - corner, cy); ctx.lineTo(cx + ew, cy); ctx.lineTo(cx + ew, cy + corner);
          // bottom-left
          ctx.moveTo(cx, cy + eh - corner); ctx.lineTo(cx, cy + eh); ctx.lineTo(cx + corner, cy + eh);
          // bottom-right
          ctx.moveTo(cx + ew - corner, cy + eh); ctx.lineTo(cx + ew, cy + eh); ctx.lineTo(cx + ew, cy + eh - corner);
          ctx.stroke();
        }
      } catch (_) {}

      if (active) setTimeout(loop, 150);
    };

    loop();
  }

  function stopFaceDetection() {
    if (stopFaceLoop) { stopFaceLoop(); stopFaceLoop = null; }
    const canvas = document.getElementById("face-canvas");
    if (canvas) canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  }

  // ── Camera ─────────────────────────────────────────────
  async function openCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      alert("المتصفح لا يدعم الكاميرا. جرّب Chrome.");
      return;
    }

    /*
     * Chrome/Android black screen fix:
     * - Do NOT request square (320x320) constraints — Android cameras don't
     *   support square capture; Chrome returns a black stream silently.
     * - Do NOT use exact facingMode — fall through to simpler constraints.
     * - Let the browser pick its native resolution; we scale in CSS/canvas.
     */
    const strategies = [
      { facingMode: { ideal: "user" } },
      { facingMode: "user" },
      {},
      true,
    ];

    let lastError = null;
    for (const videoConstraint of strategies) {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: videoConstraint, audio: false });
        break;
      } catch (e) {
        lastError = e;
      }
    }

    if (!stream) {
      console.error("[CAM]", lastError);
      alert("تعذّر تشغيل الكاميرا: " + (lastError?.message || "خطأ غير معروف") + "\nتأكد من منح صلاحية الكاميرا.");
      return;
    }

    const video = document.getElementById("cam");
    video.srcObject = null;
    video.src = "";
    video.load(); // reset element so Chromium accepts the new srcObject

    // Older Chromium (some Fully Kiosk versions) didn't support srcObject —
    // fall back to the deprecated createObjectURL if srcObject assignment fails.
    try {
      video.srcObject = stream;
    } catch (_) {
      video.src = URL.createObjectURL(stream);
    }

    // Call play() immediately — Chrome on Android sometimes never fires
    // onloadedmetadata if play() hasn't been attempted first.
    // Also start face detection in play().then() as a fallback for Fully Kiosk
    // Browser builds where onloadedmetadata never fires.
    let faceStarted = false;
    video.play().then(() => {
      if (!faceStarted) { faceStarted = true; startFaceDetection(); }
    }).catch(() => {});
    video.onloadedmetadata = () => {
      video.play().catch(e => console.error("[CAM play]", e));
      if (!faceStarted) { faceStarted = true; startFaceDetection(); }
    };
  }

  function stopCamera() {
    stopFaceDetection();
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    const cam = document.getElementById("cam");
    if (cam) {
      cam.srcObject = null;
      cam.src = "";   // also clear createObjectURL fallback if it was used
      cam.load();     // force browser to release the media resource
    }
  }

  function snap() {
    const video  = document.getElementById("cam");
    const canvas = document.getElementById("snap-canvas");
    const MAX = 320;
    const vw  = video.videoWidth  || 320;
    const vh  = video.videoHeight || 320;
    const scale = Math.min(MAX / vw, MAX / vh, 1);
    canvas.width  = Math.round(vw * scale);
    canvas.height = Math.round(vh * scale);
    const ctx = canvas.getContext("2d");
    ctx.translate(canvas.width, 0);
    ctx.scale(-1, 1);
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    stopCamera();
    push("screen-processing");
    canvas.toBlob(blob => search(blob), "image/jpeg", 0.55);
  }

  // ── Face Search ────────────────────────────────────────
  async function search(blob) {
    const dbgEl = document.getElementById("debug-status");
    const MAX_ATTEMPTS = 3;

    let ticker = null;
    function startTicker() {
      const start = Date.now();
      const msgs = [
        "جاري اكتشاف وجهك...",
        "جاري المطابقة مع الصور...",
        "يرجى الانتظار قليلاً...",
        "تقريباً جاهز...",
      ];
      let i = 0;
      ticker = setInterval(() => {
        const sec = Math.round((Date.now() - start) / 1000);
        const hint = document.getElementById("processing-hint");
        if (hint) hint.textContent = msgs[Math.min(i++, msgs.length - 1)];
        if (dbgEl) dbgEl.textContent = `${sec} ثانية`;
      }, 3000);
    }
    function stopTicker() { if (ticker) { clearInterval(ticker); ticker = null; } }

    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
      try {
        if (!blob) throw new Error("no image captured");
        const form = new FormData();
        form.append("photo", blob, "selfie.jpg");
        form.append("phone", "");
        form.append("event_code", EVENT_CODE);
        const ctrl  = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 90000);
        startTicker();
        let res;
        try {
          res = await fetch("/api/match", { method: "POST", body: form, signal: ctrl.signal });
        } finally {
          clearTimeout(timer);
          stopTicker();
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data  = await res.json();
        matches     = data.matches    || [];
        faceKey     = data.face_path  || "";
        folderUrl   = "";
        logGuest().catch(() => {});
        renderResults();
        if (data.session_id) pollFolder(data.session_id);
        return;
      } catch (e) {
        stopTicker();
        console.error(`[SEARCH] attempt ${attempt}`, e);
        if (attempt < MAX_ATTEMPTS) {
          if (dbgEl) dbgEl.textContent = `محاولة ${attempt + 1}...`;
          await new Promise(r => setTimeout(r, 1000));
          continue;
        }
        alert("حدث خطأ في الاتصال. سنعود للكاميرا — حاول مرة ثانية.");
        await openCamera();
        show("screen-selfie");
      }
    }
  }

  // ── Folder polling ─────────────────────────────────────
  async function pollFolder(sessionId) {
    for (let i = 0; i < 30; i++) {
      await new Promise(r => setTimeout(r, 3000));
      try {
        const res  = await fetch(`/api/folder-status/${sessionId}`);
        const data = await res.json();
        if (data.status === "ready" && data.folder_url) {
          folderUrl = data.folder_url;
          renderFolderQR();
          return;
        }
        if (data.status === "not_found") return;
      } catch (_) {}
    }
  }

  function renderFolderQR() {
    const qrBox = document.getElementById("results-qr-box");
    if (!qrBox || !folderUrl) return;
    qrBox.innerHTML = "";
    if (qr) { try { qr.clear(); } catch(_) {} }
    const size = Math.min(Math.round(window.innerWidth * 0.28), 180);
    qr = new QRCode(qrBox, {
      text:         folderUrl,
      width:        size,
      height:       size,
      colorDark:    "#1A1612",
      colorLight:   "#FAF6EC",
      correctLevel: QRCode.CorrectLevel.M,
    });
  }

  // ── Results ────────────────────────────────────────────
  function makeCard(m, i) {
    const card = document.createElement("div");
    card.className = "photo-card";
    card.innerHTML = `<img src="${m.url}" loading="lazy" alt="صورة ${i + 1}" onerror="this.style.opacity='0.3'"><div class="conf-pill">100%</div>`;
    card.addEventListener("click", () => showPhoto(m));
    const pill   = card.querySelector(".conf-pill");
    const target = Math.round(m.confidence);
    let current  = 100;
    setTimeout(() => {
      const step = setInterval(() => {
        current--;
        pill.textContent = current + "%";
        if (current <= target) clearInterval(step);
      }, 18);
    }, i * 60);
    return card;
  }

  function renderMore() {
    const grid    = document.getElementById("results-grid");
    const moreBtn = document.getElementById("load-more-btn");
    const batch   = matches.slice(renderedCount, renderedCount + PAGE_SIZE);
    batch.forEach((m, j) => grid.insertBefore(makeCard(m, renderedCount + j), moreBtn));
    renderedCount += batch.length;
    if (renderedCount >= matches.length) {
      if (moreBtn) moreBtn.style.display = "none";
    } else {
      if (moreBtn) moreBtn.textContent = `تحميل المزيد (${matches.length - renderedCount} صورة متبقية)`;
    }
  }

  function renderResults() {
    const grid  = document.getElementById("results-grid");
    const label = document.getElementById("results-label");
    const qrBox = document.getElementById("results-qr-box");
    grid.innerHTML = "";
    renderedCount  = 0;

    if (matches.length === 0) {
      label.textContent = "لم نجد صورك";
      if (qrBox) qrBox.style.display = "none";
      grid.innerHTML = `
        <div class="no-results">
          <span class="nr-icon">😕</span>
          <p>ما لقينا صورك هذي المرة</p>
          <small>تأكد أن السيلفي واضح وحاول مرة ثانية</small>
        </div>`;
    } else {
      label.textContent = `وجدنا ${matches.length} صورة`;
      if (qrBox) qrBox.innerHTML = "";

      const moreBtn = document.createElement("button");
      moreBtn.id        = "load-more-btn";
      moreBtn.className = "btn-load-more";
      moreBtn.style.display = "none";
      moreBtn.addEventListener("click", renderMore);
      grid.appendChild(moreBtn);

      renderMore();
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
          whatsapp:  "",
          photos:    matches.map(m => ({ url: m.url, confidence: m.confidence })),
          face_path: faceKey,
        }),
      });
    } catch (_) { /* non-critical */ }
  }

  // preload face model while user is on welcome screen
  setTimeout(() => initFaceApi().catch(() => {}), 500);

  // ── Public API ─────────────────────────────────────────
  return { start, goBack, backToResults, reset, snap };
})();
