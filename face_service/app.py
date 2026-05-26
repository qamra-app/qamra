import os
import uuid
import sqlite3
import threading
import numpy as np
import cv2
from flask import Flask, request, jsonify
from insightface.app import FaceAnalysis

app = Flask(__name__)

API_KEY = os.environ.get("FACE_API_KEY", "qamra-face-2026")
DB_PATH = os.environ.get("DB_PATH", "/data/faces.db")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# ── Model ─────────────────────────────────────────────────────────────────────
_face_app   = None
_model_lock = threading.Lock()

def get_model():
    global _face_app
    if _face_app is None:
        fa = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        fa.prepare(ctx_id=0, det_size=(640, 640))
        _face_app = fa
        print("[MODEL] buffalo_l ready", flush=True)
    return _face_app

# Warm up on startup — blocks until model is loaded
_warmup_done = threading.Event()
def _warmup():
    get_model()
    _warmup_done.set()
threading.Thread(target=_warmup, daemon=False).start()

# ── Database ──────────────────────────────────────────────────────────────────
_db_lock = threading.Lock()

def _init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS faces (
            face_token    TEXT PRIMARY KEY,
            collection_id TEXT NOT NULL,
            file_id       TEXT NOT NULL,
            embedding     BLOB NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_col ON faces(collection_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv_store (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

_conn = _init_db()

# ── Helpers ───────────────────────────────────────────────────────────────────
def _auth():
    return request.headers.get("X-API-Key") == API_KEY

def _decode(raw):
    arr = np.frombuffer(raw, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    if max(h, w) > 1920:
        s = 1920 / max(h, w)
        img = cv2.resize(img, (int(w * s), int(h * s)))
    return img

def _e2b(e): return e.astype(np.float32).tobytes()
def _b2e(b): return np.frombuffer(b, dtype=np.float32)

def _cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(np.dot(a, b) / (na * nb)) if na and nb else 0.0

def _get_faces(raw):
    img = _decode(raw)
    if img is None:
        return []
    with _model_lock:
        return get_model().get(img)

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    with _db_lock:
        n = _conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
    return jsonify({"status": "ok", "faces": n, "model_ready": _warmup_done.is_set()})

@app.route("/v1/detect", methods=["POST"])
def detect():
    if not _auth(): return jsonify({"error": "Unauthorized"}), 401
    f = request.files.get("photo")
    if not f: return jsonify({"error": "photo required"}), 400
    return jsonify({"count": len(_get_faces(f.read()))})

@app.route("/v1/index", methods=["POST"])
def index_face():
    if not _auth(): return jsonify({"error": "Unauthorized"}), 401
    f             = request.files.get("photo")
    file_id       = request.form.get("file_id", "")
    collection_id = request.form.get("collection_id", "")
    if not f or not file_id or not collection_id:
        return jsonify({"error": "photo, file_id, collection_id required"}), 400

    faces = _get_faces(f.read())
    if not faces:
        return jsonify({"face_tokens": [], "count": 0})

    tokens = []
    with _db_lock:
        for face in faces:
            token = uuid.uuid4().hex[:16]
            _conn.execute(
                "INSERT OR IGNORE INTO faces VALUES (?, ?, ?, ?)",
                (token, collection_id, file_id, _e2b(face.embedding))
            )
            tokens.append(token)
        _conn.commit()

    return jsonify({"face_tokens": tokens, "count": len(tokens)})

@app.route("/v1/search", methods=["POST"])
def search():
    if not _auth(): return jsonify({"error": "Unauthorized"}), 401
    f             = request.files.get("photo")
    collection_id = request.form.get("collection_id", "")
    if not f or not collection_id:
        return jsonify({"error": "photo, collection_id required"}), 400

    faces = _get_faces(f.read())
    if not faces:
        return jsonify({"results": []})

    # Largest face = the person standing at the kiosk
    selfie_emb = max(
        faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1])
    ).embedding

    with _db_lock:
        rows = _conn.execute(
            "SELECT face_token, embedding FROM faces WHERE collection_id = ?",
            (collection_id,)
        ).fetchall()

    results = []
    for token, emb_b in rows:
        sim = _cos(selfie_emb, _b2e(emb_b))
        if sim > 0:
            results.append({"face_token": token, "confidence": round(sim * 100, 1)})

    results.sort(key=lambda x: x["confidence"], reverse=True)
    return jsonify({"results": results[:1000]})

@app.route("/v1/status/<collection_id>")
def status(collection_id):
    if not _auth(): return jsonify({"error": "Unauthorized"}), 401
    with _db_lock:
        n = _conn.execute(
            "SELECT COUNT(*) FROM faces WHERE collection_id = ?", (collection_id,)
        ).fetchone()[0]
    return jsonify({"collection_id": collection_id, "face_count": n})

@app.route("/v1/clear/<collection_id>", methods=["DELETE"])
def clear(collection_id):
    if not _auth(): return jsonify({"error": "Unauthorized"}), 401
    with _db_lock:
        _conn.execute("DELETE FROM faces WHERE collection_id = ?", (collection_id,))
        _conn.commit()
    return jsonify({"status": "cleared", "collection_id": collection_id})

@app.route("/v1/kv/<key>", methods=["GET"])
def kv_get(key):
    if not _auth(): return jsonify({"error": "Unauthorized"}), 401
    with _db_lock:
        row = _conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"key": key, "value": row[0]})

@app.route("/v1/kv/<key>", methods=["PUT"])
def kv_put(key):
    if not _auth(): return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True)
    if data is None or "value" not in data:
        return jsonify({"error": "body must be {\"value\": \"...\"}"}), 400
    with _db_lock:
        _conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
            (key, data["value"])
        )
        _conn.commit()
    return jsonify({"status": "ok", "key": key})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
