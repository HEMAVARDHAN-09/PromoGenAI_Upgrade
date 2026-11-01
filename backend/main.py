"""PromoGen AI - FastAPI backend.

Run from this folder:  python -m uvicorn main:app --reload
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator

import render as renderer
import scriptgen
from render import RenderError, render_video

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
load_dotenv(BASE / ".env")

DB = BASE / "promogen.db"
UPLOADS = BASE / "uploads"
RENDERS = BASE / "renders"
FRONTEND = ROOT / "frontend"
UPLOADS.mkdir(exist_ok=True)
RENDERS.mkdir(exist_ok=True)


def load_secret():
    """Use SECRET_KEY from .env; otherwise create and persist a random key so tokens never use a shared default."""
    env = os.getenv("SECRET_KEY", "").strip()
    if env and not env.startswith("replace-with"):
        return env
    f = BASE / ".secret_key"
    if f.exists() and f.read_text().strip():
        return f.read_text().strip()
    key = secrets.token_urlsafe(48)
    f.write_text(key)
    return key


SECRET = load_secret()
ALG = "HS256"
TOKEN_MIN = int(os.getenv("ACCESS_TOKEN_MINUTES", "1440"))
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
MAX_VIDEO_UPLOAD = int(os.getenv("MAX_VIDEO_UPLOAD_MB", "80")) * 1024 * 1024
MAX_AUDIO = int(os.getenv("MAX_AUDIO_MB", "25")) * 1024 * 1024
RENDER_SLOT = threading.Lock()  # one FFmpeg render at a time for the whole server
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
VIDEO_TYPES = {"video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}

bearer = HTTPBearer(auto_error=False)
app = FastAPI(title="PromoGen AI", version="4.0")

DEFAULT_ORIGINS = ("http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:5500,"
                   "http://localhost:5500,http://127.0.0.1:3000,http://localhost:3000")
origins = [o.strip() for o in os.getenv("CORS_ORIGINS", DEFAULT_ORIGINS).split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------- database
class Connection(sqlite3.Connection):
    """`with conn() as c:` commits (or rolls back) AND closes, so no file handles leak on Windows."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def conn():
    c = sqlite3.connect(DB, factory=Connection, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def now():
    return datetime.now(timezone.utc).isoformat()


def jload(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,full_name TEXT NOT NULL,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,preferences TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS custom_products(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,name TEXT NOT NULL,brand TEXT,category TEXT NOT NULL,description TEXT NOT NULL,features TEXT,target_audience TEXT,price TEXT,image_path TEXT,campaign_objective TEXT,created_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,title TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',product_json TEXT NOT NULL,config_json TEXT NOT NULL DEFAULT '{}',script_json TEXT,video_filename TEXT,render_status TEXT NOT NULL DEFAULT 'not_started',render_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        """)
        # light migrations so an existing promogen.db keeps working
        cols = {r["name"] for r in c.execute("PRAGMA table_info(projects)")}
        if "render_progress" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN render_progress TEXT")
        if "logo_path" not in {r["name"] for r in c.execute("PRAGMA table_info(custom_products)")}:
            c.execute("ALTER TABLE custom_products ADD COLUMN logo_path TEXT")
        # a render thread cannot survive a server restart
        c.execute("UPDATE projects SET render_status='failed',render_error='Rendering was interrupted by a server restart. Please render again.' WHERE render_status='rendering'")


init_db()


# --------------------------------------------------------------------------- passwords + tokens
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                            maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


DUMMY_HASH = hash_password("not-a-real-password-1")


_FAILS = {}  # "ip|email" -> [timestamps of failed logins]
FAIL_LIMIT, FAIL_WINDOW = 8, 15 * 60


def throttle_check(key):
    hits = [t for t in _FAILS.get(key, []) if time.time() - t < FAIL_WINDOW]
    _FAILS[key] = hits
    if len(hits) >= FAIL_LIMIT:
        raise HTTPException(429, "Too many failed logins. Please wait 15 minutes and try again.")


def throttle_fail(key):
    _FAILS.setdefault(key, []).append(time.time())


def make_token(uid):
    return jwt.encode({"sub": str(uid), "exp": datetime.now(timezone.utc) + timedelta(minutes=TOKEN_MIN)}, SECRET, algorithm=ALG)


def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    if not creds:
        raise HTTPException(401, "Authentication required")
    try:
        uid = int(jwt.decode(creds.credentials, SECRET, algorithms=[ALG])["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(401, "Invalid or expired session")
    with conn() as c:
        u = c.execute("SELECT id,full_name,email,preferences,created_at FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        raise HTTPException(401, "User not found")
    return dict(u)


def check_password_strength(v):
    if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
        raise ValueError("Password must include a letter and a number")
    return v


# --------------------------------------------------------------------------- schemas
class Register(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str

    @field_validator("password")
    @classmethod
    def strong(cls, v):
        return check_password_strength(v)


class Login(BaseModel):
    email: EmailStr
    password: str


class ProfileUpdate(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    preferences: dict = {}


class PasswordUpdate(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


PRODUCT_TEXT_KEYS = ("name", "brand", "category", "description", "price", "target_audience", "campaign_objective", "id")


def clean_product(p):
    """Keep only known product fields, with sane lengths (the product dict is stored and sent to the LLM)."""
    if not isinstance(p, dict):
        raise ValueError("Product details are missing")
    out = {k: str(p.get(k) or "").strip()[:1500 if k == "description" else 200] for k in PRODUCT_TEXT_KEYS}
    out["features"] = [str(f).strip()[:200] for f in (p.get("features") or []) if str(f).strip()][:12] if isinstance(p.get("features"), list) else []
    for k in ("image", "logo"):
        v = str(p.get(k) or "")
        out[k] = v if v.startswith(("/api/uploads/", "https://")) and len(v) < 500 else ""
    if p.get("custom"):
        out["custom"] = True
    if not out["name"]:
        raise ValueError("Product or service name is required")
    return out


class ScriptRequest(BaseModel):
    product: dict
    target_audience: str = Field(default="General consumers", max_length=300)
    marketing_objective: str = Field(default="Drive awareness and conversions", max_length=300)
    platform: str = Field(default="Instagram Reels", max_length=60)
    duration: int = Field(default=30, ge=6, le=90)
    tone: str = Field(default="Energetic", max_length=60)
    language: str = Field(default="English", max_length=40)
    call_to_action: str = Field(default="Learn more today", max_length=200)
    key_selling_points: str = Field(default="", max_length=2000)
    instructions: str = Field(default="", max_length=2000)
    project_id: Optional[int] = None

    @field_validator("product")
    @classmethod
    def product_ok(cls, v):
        return clean_product(v)


class ScriptSave(BaseModel):
    script: dict


class ScenesSave(BaseModel):
    scenes: list[dict]


class RenderRequest(BaseModel):
    scenes: list[dict]
    aspect_ratio: str = "9:16"
    resolution: str = "720p"
    voice: str = "none"
    captions: bool = True
    use_product_image: bool = True
    use_logo: bool = True


# --------------------------------------------------------------------------- helpers
def own_project(pid, uid):
    with conn() as c:
        r = c.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, uid)).fetchone()
    if not r:
        raise HTTPException(404, "Project not found")
    return dict(r)


def project_out(r):
    keys = ["id", "title", "status", "video_filename", "render_status", "render_error", "render_progress", "created_at", "updated_at"]
    return {**{k: r.get(k) for k in keys},
            "product": jload(r["product_json"], {}), "config": jload(r["config_json"], {}), "script": jload(r["script_json"], None)}


def safe_name(name):
    return Path(str(name or "")).name


# --------------------------------------------------------------------------- auth + account
@app.post("/api/auth/register")
def register(data: Register):
    if data.password != data.confirm_password:
        raise HTTPException(422, "Passwords do not match")
    try:
        with conn() as c:
            c.execute("INSERT INTO users(full_name,email,password_hash,created_at) VALUES(?,?,?,?)",
                      (data.full_name.strip(), data.email.lower(), hash_password(data.password), now()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with this email already exists")
    return {"message": "Registration successful. Please log in."}


@app.post("/api/auth/login")
def login(data: Login, request: Request):
    key = f"{request.client.host if request.client else '?'}|{data.email.lower()}"
    throttle_check(key)
    with conn() as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (data.email.lower(),)).fetchone()
    ok = verify_password(data.password, u["password_hash"] if u else DUMMY_HASH)
    if not u or not ok:
        throttle_fail(key)
        raise HTTPException(401, "Incorrect email or password")
    _FAILS.pop(key, None)
    return {"access_token": make_token(u["id"]), "token_type": "bearer",
            "user": {"id": u["id"], "full_name": u["full_name"], "email": u["email"]}}


@app.post("/api/auth/logout")
def logout(user=Depends(current_user)):
    # Tokens are stateless; the client discards its token.
    return {"message": "Logged out"}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {**user, "preferences": jload(user["preferences"], {})}


@app.put("/api/me")
def update_me(data: ProfileUpdate, user=Depends(current_user)):
    with conn() as c:
        c.execute("UPDATE users SET full_name=?,preferences=? WHERE id=?", (data.full_name.strip(), json.dumps(data.preferences), user["id"]))
    return {"message": "Profile updated"}


@app.put("/api/me/password")
def update_password(data: PasswordUpdate, user=Depends(current_user)):
    try:
        check_password_strength(data.new_password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    with conn() as c:
        u = c.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if not verify_password(data.current_password, u["password_hash"]):
            raise HTTPException(400, "Current password is incorrect")
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(data.new_password), user["id"]))
    return {"message": "Password changed"}


@app.get("/api/capabilities")
def capabilities(user=Depends(current_user)):
    llm = bool(os.getenv("LLM_API_KEY"))
    return {"llm": llm, "llm_model": os.getenv("LLM_MODEL", "gpt-4o-mini") if llm else None,
            "demo_languages": list(scriptgen.DEMO_LANGS), "lang_codes": scriptgen.LANG_CODES,
            "ffmpeg": renderer.ffmpeg_available(), "voices": renderer.available_voices(),
            "tts_api": bool(os.getenv("TTS_API_KEY")), "stt": bool(os.getenv("STT_API_KEY")),
            "fonts": renderer.script_support(),
            "max_image_mb": MAX_UPLOAD // (1024 * 1024), "max_video_mb": MAX_VIDEO_UPLOAD // (1024 * 1024)}


# --------------------------------------------------------------------------- products
CATALOG = json.loads((BASE / "catalog.json").read_text(encoding="utf-8"))


def split_features(raw):
    """One feature per line (or ';'); commas separate features only when there are no line breaks."""
    raw = raw or ""
    parts = re.split(r"[\n;]", raw) if re.search(r"[\n;]", raw) else raw.split(",")
    return [x.strip() for x in parts if x.strip()]


def custom_out(r):
    return {"id": f"custom-{r['id']}", "name": r["name"], "brand": r["brand"] or "", "category": r["category"], "description": r["description"],
            "features": split_features(r["features"]), "target_audience": r["target_audience"] or "",
            "price": r["price"] or "", "campaign_objective": r["campaign_objective"] or "", "custom": True,
            "image": f"/api/uploads/{r['image_path']}" if r["image_path"] else "",
            "logo": f"/api/uploads/{r['logo_path']}" if r["logo_path"] else ""}


@app.get("/api/products")
def products(q: str = "", category: str = "", brand: str = "", user=Depends(current_user)):
    q = q.strip().lower()
    out = [p for p in CATALOG
           if (not q or q in " ".join([p["name"], p["brand"], p["description"], p["category"], " ".join(p.get("features", []))]).lower())
           and (not category or p["category"] == category)
           and (not brand or brand.lower() in p["brand"].lower())]
    return {"items": out, "total": len(out), "categories": sorted({p["category"] for p in CATALOG})}


@app.get("/api/products/mine")
def my_products(user=Depends(current_user)):
    with conn() as c:
        rows = c.execute("SELECT * FROM custom_products WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    return {"items": [custom_out(r) for r in rows]}


@app.get("/api/products/{product_id}")
def product(product_id: str, user=Depends(current_user)):
    p = next((x for x in CATALOG if x["id"] == product_id), None)
    if not p:
        raise HTTPException(404, "Product not found")
    return p


async def save_image(upload, uid, what):
    """Validate and store an uploaded JPG/PNG/WebP; returns the stored file name or None."""
    if upload is None or not upload.filename:
        return None
    if upload.content_type not in IMAGE_TYPES:
        raise HTTPException(415, f"{what}: only JPG, PNG and WebP images are allowed")
    data = await upload.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"{what} is too large (limit {MAX_UPLOAD // (1024 * 1024)} MB)")
    name = f"u{uid}_{uuid.uuid4().hex}.{IMAGE_TYPES[upload.content_type]}"
    path = UPLOADS / name
    path.write_bytes(data)
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
    except Exception:
        path.unlink(missing_ok=True)
        raise HTTPException(422, f"{what} could not be read as a valid image")
    return name


@app.post("/api/products/custom")
async def custom_product(name: str = Form(...), category: str = Form(...), description: str = Form(...), brand: str = Form(""),
                         features: str = Form(""), target_audience: str = Form(""), price: str = Form(""), campaign_objective: str = Form(""),
                         image: UploadFile | None = File(None), logo: UploadFile | None = File(None), user=Depends(current_user)):
    name, category, description = name.strip()[:120], category.strip()[:120], description.strip()[:1500]
    if not (name and category and description):
        raise HTTPException(422, "Name, category and description are required")
    brand, features, target_audience, price, campaign_objective = brand[:120], features[:2000], target_audience[:300], price[:60], campaign_objective[:300]
    image_path = await save_image(image, user["id"], "Product image")
    logo_path = await save_image(logo, user["id"], "Logo")
    with conn() as c:
        cur = c.execute("INSERT INTO custom_products(user_id,name,brand,category,description,features,target_audience,price,image_path,campaign_objective,created_at,logo_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (user["id"], name, brand, category, description, features, target_audience, price, image_path, campaign_objective, now(), logo_path))
        row = c.execute("SELECT * FROM custom_products WHERE id=?", (cur.lastrowid,)).fetchone()
    return custom_out(row)


@app.delete("/api/products/custom/{cid}")
def delete_custom_product(cid: int, user=Depends(current_user)):
    with conn() as c:
        row = c.execute("SELECT image_path,logo_path FROM custom_products WHERE id=? AND user_id=?", (cid, user["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Product not found")
        c.execute("DELETE FROM custom_products WHERE id=?", (cid,))
    for f in (row["image_path"], row["logo_path"]):
        if f:
            (UPLOADS / safe_name(f)).unlink(missing_ok=True)
    return {"message": "Product deleted"}


# --------------------------------------------------------------------------- uploads
@app.get("/api/uploads/{filename}")
def upload_file(filename: str, user=Depends(current_user)):
    safe = safe_name(filename)
    if not safe.startswith(f"u{user['id']}_"):
        raise HTTPException(403, "Not authorized")
    p = UPLOADS / safe
    if not p.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(p)


@app.post("/api/media")
async def upload_media(file: UploadFile = File(...), user=Depends(current_user)):
    """Upload an image or video clip that can be used as a scene background."""
    ctype = file.content_type or ""
    if ctype in IMAGE_TYPES:
        kind, ext, limit = "image", IMAGE_TYPES[ctype], MAX_UPLOAD
    elif ctype in VIDEO_TYPES:
        kind, ext, limit = "video", VIDEO_TYPES[ctype], MAX_VIDEO_UPLOAD
    else:
        raise HTTPException(415, "Upload a JPG, PNG, WebP image or an MP4, WebM or MOV video")
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File is too large (limit {limit // (1024 * 1024)} MB)")
    name = f"u{user['id']}_{uuid.uuid4().hex}.{ext}"
    path = UPLOADS / name
    path.write_bytes(data)
    try:
        if kind == "image":
            from PIL import Image
            with Image.open(path) as im:
                im.verify()
        elif not (renderer.ffmpeg_available() and renderer.has_video_stream(path)):
            raise ValueError("not a readable video (or FFmpeg is missing)")
    except Exception:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "That file could not be read as a valid image or video")
    return {"filename": name, "type": kind, "url": f"/api/uploads/{name}"}


# --------------------------------------------------------------------------- speech to text
@app.post("/api/speech/transcribe")
async def transcribe(file: UploadFile = File(...), language: str = Form(""), user=Depends(current_user)):
    """Turn a recorded voice note into text with an OpenAI-compatible transcription API. Never fakes a result."""
    key = os.getenv("STT_API_KEY")
    if not key:
        raise HTTPException(503, "Speech-to-text is not configured on this server. Set STT_API_KEY (and optionally STT_API_URL, STT_MODEL) "
                                 "in backend/.env and restart. You can still type the description.")
    ctype = (file.content_type or "").split(";")[0].strip().lower()
    if not (ctype.startswith("audio/") or ctype == "video/webm"):
        raise HTTPException(415, "Upload an audio recording")
    data = await file.read(MAX_AUDIO + 1)
    if len(data) > MAX_AUDIO:
        raise HTTPException(413, "Recording is too large")
    if len(data) < 800:
        raise HTTPException(422, "The recording is empty. Hold the microphone button a little longer and try again.")
    ext = {"audio/webm": "webm", "video/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav",
           "audio/x-wav": "wav"}.get(ctype, "webm")
    form = {"model": os.getenv("STT_MODEL", "whisper-1")}
    code = scriptgen.LANG_CODES.get(language)
    if code:
        form["language"] = code
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(os.getenv("STT_API_URL", "https://api.openai.com/v1/audio/transcriptions"),
                                  headers={"Authorization": f"Bearer {key}"}, files={"file": (f"speech.{ext}", data, ctype)}, data=form)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not reach the speech-to-text provider: {e.__class__.__name__}")
    if r.status_code >= 400:
        raise HTTPException(502, f"Speech-to-text provider error ({r.status_code}): {r.text[:200]}")
    try:
        text = str(r.json().get("text", "")).strip()
    except ValueError:
        text = ""
    if not text:
        raise HTTPException(422, "No speech was detected in the recording.")
    return {"text": text}


# --------------------------------------------------------------------------- script generation
def normalize_script(raw, d: ScriptRequest, mode):
    try:
        return scriptgen.normalize_script(raw, d.model_dump(), mode)
    except ValueError as e:
        raise HTTPException(502, str(e))


async def ai_script(d: ScriptRequest):
    key = os.getenv("LLM_API_KEY")
    if not key:
        return scriptgen.demo_script(d.model_dump())
    payload = {"model": os.getenv("LLM_MODEL", "gpt-4o-mini"), "temperature": 0.8,
               "messages": [{"role": "system", "content": "You are an expert short-form advertising scriptwriter. Reply with valid JSON only."},
                            {"role": "user", "content": scriptgen.build_prompt(d.model_dump())}]}
    url = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(url, headers=headers, json={**payload, "response_format": {"type": "json_object"}})
            if r.status_code == 400:  # some providers reject response_format
                r = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not reach the AI provider: {e.__class__.__name__}")
    if r.status_code >= 400:
        raise HTTPException(502, f"AI provider error ({r.status_code}): {r.text[:200]}")
    try:
        text = r.json()["choices"][0]["message"]["content"]
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        raw = json.loads(text)
    except (KeyError, IndexError, ValueError):
        raise HTTPException(502, "The AI provider did not return valid JSON. Please try again.")
    return normalize_script(raw, d, "live")


def delete_video_file(filename):
    if filename:
        (RENDERS / safe_name(filename)).unlink(missing_ok=True)


@app.post("/api/scripts/generate")
async def generate_script(data: ScriptRequest, user=Depends(current_user)):
    if data.project_id:
        own_project(data.project_id, user["id"])
    result = await ai_script(data)  # demo scripts are already complete; live ones were validated in ai_script()
    ts = now()
    with conn() as c:
        if data.project_id:
            old = c.execute("SELECT video_filename FROM projects WHERE id=?", (data.project_id,)).fetchone()
            delete_video_file(old["video_filename"])  # a new script invalidates the old video
            c.execute("UPDATE projects SET title=?,product_json=?,config_json=?,script_json=?,status='script_generated',video_filename=NULL,render_status='not_started',render_error=NULL,render_progress=NULL,updated_at=? WHERE id=?",
                      (result["campaign_title"], json.dumps(data.product), data.model_dump_json(), json.dumps(result), ts, data.project_id))
            pid = data.project_id
        else:
            cur = c.execute("INSERT INTO projects(user_id,title,status,product_json,config_json,script_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                            (user["id"], result["campaign_title"], "script_generated", json.dumps(data.product), data.model_dump_json(), json.dumps(result), ts, ts))
            pid = cur.lastrowid
    return {"project_id": pid, "script": result}


def clean_script(script, uid, stored):
    """Whitelist and validate an edited script. mode/language/notice always come from the server copy, never the client."""
    if not isinstance(script, dict):
        raise HTTPException(422, "Invalid script")
    def txt(k, n=2000):
        return str(script.get(k) or "").strip()[:n]
    scenes = clean_scenes(script.get("scenes") if isinstance(script.get("scenes"), list) else [], uid)
    tags = script.get("hashtags") or []
    tags = [t if str(t).startswith("#") else f"#{t}" for t in (tags if isinstance(tags, list) else []) if str(t).strip()][:20]
    benefits = script.get("benefits") or []
    benefits = [str(b).strip()[:200] for b in (benefits if isinstance(benefits, list) else []) if str(b).strip()][:10]
    return {"mode": stored.get("mode", "demo"), "language": stored.get("language", "English"), "notice": stored.get("notice"),
            "campaign_title": txt("campaign_title", 200) or "Untitled campaign", "hook": txt("hook"), "introduction": txt("introduction"),
            "benefits": benefits, "complete_script": txt("complete_script", 8000), "scenes": scenes, "caption": txt("caption"),
            "hashtags": [str(t)[:60] for t in tags], "tagline": txt("tagline", 300), "final_cta": txt("final_cta", 300)}


@app.put("/api/projects/{pid}/script")
def save_script(pid: int, data: ScriptSave, user=Depends(current_user)):
    p = own_project(pid, user["id"])
    if not isinstance(data.script.get("scenes"), list) or not data.script["scenes"]:
        raise HTTPException(422, "A script needs at least one scene")
    script = clean_script(data.script, user["id"], jload(p["script_json"], {}) or {})
    with conn() as c:
        c.execute("UPDATE projects SET title=?,script_json=?,status='script_edited',updated_at=? WHERE id=?",
                  (script["campaign_title"][:200], json.dumps(script), now(), pid))
    return {"message": "Script saved"}


@app.post("/api/projects/{pid}/approve")
def approve(pid: int, user=Depends(current_user)):
    p = own_project(pid, user["id"])
    if not p["script_json"]:
        raise HTTPException(400, "Generate a script first")
    with conn() as c:
        c.execute("UPDATE projects SET status='approved',updated_at=? WHERE id=?", (now(), pid))
    return {"message": "Script approved", "project_id": pid}


# --------------------------------------------------------------------------- rendering
SCENE_LIMITS = {"title": 80, "visual": 600, "voiceover": 1500, "onscreen_text": 160, "caption": 1500}


def clean_scenes(scenes, uid):
    """Validate user-supplied scenes: sane sizes, and media may only be this user's own uploads."""
    if not 1 <= len(scenes) <= 12:
        raise HTTPException(422, "A video needs between 1 and 12 scenes")
    out = []
    for i, s in enumerate(scenes):
        if not isinstance(s, dict):
            raise HTTPException(422, f"Scene {i + 1} is invalid")
        try:
            dur = max(1, min(int(round(float(s.get("duration", 3)))), 20))
        except (TypeError, ValueError):
            raise HTTPException(422, f"Scene {i + 1} has an invalid duration")
        media = s.get("media")
        if media:
            media = safe_name(media)
            if not media.startswith(f"u{uid}_") or not (UPLOADS / media).is_file():
                raise HTTPException(422, f"Scene {i + 1} refers to a media file that does not exist")
            ext = media.rsplit(".", 1)[-1].lower()
            if ext not in set(IMAGE_TYPES.values()) | set(VIDEO_TYPES.values()):
                raise HTTPException(422, f"Scene {i + 1} has an unsupported media type")
        item = {k: str(s.get(k) or "")[:n] for k, n in SCENE_LIMITS.items()}
        item["caption"] = item["caption"] or item["voiceover"]
        out.append({**item, "id": i + 1, "duration": dur, "transition": "cut" if str(s.get("transition", "")).lower() == "cut" else "fade", "media": media or None})
    start = 0
    for s in out:
        s["start"], s["end"] = start, start + s["duration"]
        start = s["end"]
    return out


def studio_scenes(scenes, stored_script, uid):
    """Video Studio may change only timing, transitions and media. Wording always comes from the APPROVED script copy."""
    stored = (stored_script or {}).get("scenes") or []
    if len(scenes) != len(stored):
        raise HTTPException(409, "The storyboard changed. Go back to the script editor, save and approve it again.")
    merged = [{**st, "duration": new.get("duration", st.get("duration")), "transition": new.get("transition", st.get("transition")),
               "media": new.get("media")} for st, new in zip(stored, scenes) if isinstance(new, dict)]
    return clean_scenes(merged, uid)


def persist_scenes(pid, scenes):
    with conn() as c:
        row = c.execute("SELECT script_json FROM projects WHERE id=?", (pid,)).fetchone()
        script = jload(row["script_json"], {})
        script["scenes"] = scenes
        c.execute("UPDATE projects SET script_json=?,updated_at=? WHERE id=?", (json.dumps(script), now(), pid))


@app.put("/api/projects/{pid}/scenes")
def save_scenes(pid: int, data: ScenesSave, user=Depends(current_user)):
    p = own_project(pid, user["id"])
    persist_scenes(pid, studio_scenes(data.scenes, jload(p["script_json"], {}), user["id"]))
    return {"message": "Scenes saved"}


def render_worker(pid, uid, scenes, req, extras):
    def progress(msg):
        with conn() as c:
            c.execute("UPDATE projects SET render_progress=? WHERE id=?", (msg, pid))

    try:
        progress("Waiting for another render to finish" if RENDER_SLOT.locked() else "Starting render")
        with RENDER_SLOT:
            job = []
            for s in scenes:
                item = dict(s)
                if s.get("media"):
                    item["media_path"] = UPLOADS / s["media"]
                    item["media_type"] = "image" if s["media"].rsplit(".", 1)[-1].lower() in IMAGE_TYPES.values() else "video"
                elif extras.get("product_image"):
                    item["media_path"], item["media_type"] = extras["product_image"], "image"
                job.append(item)
            filename = render_video(pid, job, req.aspect_ratio, req.resolution, voice=req.voice, captions=req.captions, progress=progress,
                                    logo_path=extras.get("logo"), brand_text=extras.get("brand", ""))
        with conn() as c:
            old = c.execute("SELECT video_filename FROM projects WHERE id=?", (pid,)).fetchone()
            c.execute("UPDATE projects SET video_filename=?,render_status='completed',status='video_ready',render_error=NULL,render_progress=NULL,updated_at=? WHERE id=? AND user_id=?",
                      (filename, now(), pid, uid))
        if old and old["video_filename"] and old["video_filename"] != filename:
            delete_video_file(old["video_filename"])
    except RenderError as e:
        fail(pid, uid, str(e))
    except Exception as e:  # noqa: BLE001 - surface anything unexpected to the user rather than hanging in 'rendering'
        fail(pid, uid, f"Unexpected rendering error: {e.__class__.__name__}: {e}")


def fail(pid, uid, msg):
    with conn() as c:
        c.execute("UPDATE projects SET render_status='failed',render_error=?,render_progress=NULL,updated_at=? WHERE id=? AND user_id=?", (msg[:1000], now(), pid, uid))


def own_upload(url, uid, kinds=("image",)):
    """Resolve '/api/uploads/<file>' to a real path if (and only if) it is this user's own existing upload."""
    if not url or not str(url).startswith("/api/uploads/"):
        return None
    name = safe_name(url)
    ext = name.rsplit(".", 1)[-1].lower()
    allowed = set()
    if "image" in kinds:
        allowed |= set(IMAGE_TYPES.values())
    if name.startswith(f"u{uid}_") and ext in allowed and (UPLOADS / name).is_file():
        return UPLOADS / name
    return None


@app.post("/api/projects/{pid}/render")
def render(pid: int, data: RenderRequest, user=Depends(current_user)):
    p = own_project(pid, user["id"])
    if p["status"] not in ("approved", "video_ready"):
        raise HTTPException(400, "Approve the script before rendering a video")
    if p["render_status"] == "rendering":
        raise HTTPException(409, "A render is already in progress for this project")
    if data.aspect_ratio not in {"9:16", "16:9", "1:1"} or data.resolution not in {"720p", "1080p"}:
        raise HTTPException(422, "Unsupported render settings")
    if data.voice != "none" and not renderer.voice_ok(data.voice):
        raise HTTPException(422, "That voiceover option is not available on this server")
    if not renderer.ffmpeg_available():
        raise HTTPException(503, "FFmpeg is not installed or not on PATH. Install FFmpeg, restart the server and try again.")
    scenes = studio_scenes(data.scenes, jload(p["script_json"], {}), user["id"])
    try:
        renderer.check_fonts(scenes, data.captions)  # fail now, with a clear message, instead of drawing empty boxes
    except RenderError as e:
        raise HTTPException(422, str(e))
    product = jload(p["product_json"], {})
    extras = {"brand": product.get("brand") or product.get("name") or "",
              "product_image": own_upload(product.get("image"), user["id"]) if data.use_product_image else None,
              "logo": own_upload(product.get("logo"), user["id"]) if data.use_logo else None}
    with conn() as c:  # atomic claim: two quick clicks cannot start two renders of one project
        cur = c.execute("UPDATE projects SET render_status='rendering',render_error=NULL,render_progress='Starting render',updated_at=? WHERE id=? AND user_id=? AND render_status!='rendering'",
                        (now(), pid, user["id"]))
        if cur.rowcount == 0:
            raise HTTPException(409, "A render is already in progress for this project")
    persist_scenes(pid, scenes)
    threading.Thread(target=render_worker, args=(pid, user["id"], scenes, data, extras), daemon=True).start()
    return {"status": "rendering"}


@app.get("/api/projects/{pid}/render-status")
def render_status(pid: int, user=Depends(current_user)):
    p = own_project(pid, user["id"])
    has = bool(p["video_filename"]) and (RENDERS / safe_name(p["video_filename"])).is_file()
    return {"status": p["render_status"], "error": p["render_error"], "progress": p["render_progress"],
            "video_url": f"/api/projects/{pid}/video" if has else None}


@app.get("/api/projects/{pid}/video")
def video(pid: int, download: bool = False, user=Depends(current_user)):
    p = own_project(pid, user["id"])
    if not p["video_filename"]:
        raise HTTPException(404, "Video not ready")
    path = RENDERS / safe_name(p["video_filename"])
    if not path.is_file():
        raise HTTPException(404, "Video file is missing")
    title = re.sub(r"[^A-Za-z0-9]+", "-", p["title"]).strip("-")[:50] or "promo"
    return FileResponse(path, media_type="video/mp4", filename=f"{title}.mp4" if download else None,
                        content_disposition_type="attachment" if download else "inline")


# --------------------------------------------------------------------------- projects + dashboard
@app.get("/api/projects")
def projects(q: str = "", status: str = "", kind: str = "", user=Depends(current_user)):
    sql, args = "SELECT * FROM projects WHERE user_id=?", [user["id"]]
    if q:
        sql += " AND title LIKE ?"
        args.append(f"%{q}%")
    if status:
        sql += " AND status=?"
        args.append(status)
    if kind == "videos":
        sql += " AND video_filename IS NOT NULL"
    elif kind == "scripts":
        sql += " AND script_json IS NOT NULL"
    sql += " ORDER BY updated_at DESC"
    with conn() as c:
        rows = c.execute(sql, args).fetchall()
    return {"items": [project_out(dict(x)) for x in rows], "total": len(rows)}


@app.get("/api/projects/{pid}")
def get_project(pid: int, user=Depends(current_user)):
    return project_out(own_project(pid, user["id"]))


@app.delete("/api/projects/{pid}")
def delete_project(pid: int, user=Depends(current_user)):
    p = own_project(pid, user["id"])
    delete_video_file(p["video_filename"])
    for s in (jload(p["script_json"], {}) or {}).get("scenes", []):
        if isinstance(s, dict) and s.get("media") and safe_name(s["media"]).startswith(f"u{user['id']}_"):
            (UPLOADS / safe_name(s["media"])).unlink(missing_ok=True)
    with conn() as c:
        c.execute("DELETE FROM projects WHERE id=? AND user_id=?", (pid, user["id"]))
    return {"message": "Project deleted"}


@app.get("/api/dashboard")
def dashboard(user=Depends(current_user)):
    uid = user["id"]
    with conn() as c:
        def count(where=""):
            return c.execute(f"SELECT COUNT(*) n FROM projects WHERE user_id=? {where}", (uid,)).fetchone()["n"]
        stats = {"projects": count(), "scripts": count("AND script_json IS NOT NULL"), "videos": count("AND video_filename IS NOT NULL"),
                 "approved": count("AND status IN ('approved','video_ready')"),
                 "products": c.execute("SELECT COUNT(*) n FROM custom_products WHERE user_id=?", (uid,)).fetchone()["n"]}
        recent = c.execute("SELECT * FROM projects WHERE user_id=? ORDER BY updated_at DESC LIMIT 6", (uid,)).fetchall()
        scripts = c.execute("SELECT * FROM projects WHERE user_id=? AND script_json IS NOT NULL ORDER BY updated_at DESC LIMIT 5", (uid,)).fetchall()
        videos = c.execute("SELECT * FROM projects WHERE user_id=? AND video_filename IS NOT NULL ORDER BY updated_at DESC LIMIT 5", (uid,)).fetchall()
    return {"stats": stats, "recent": [project_out(dict(x)) for x in recent], "scripts": [project_out(dict(x)) for x in scripts],
            "videos": [project_out(dict(x)) for x in videos]}


# --------------------------------------------------------------------------- frontend
app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/{path:path}")
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "Unknown API route")
    candidate = (FRONTEND / path).resolve()
    # never serve anything outside the frontend folder (e.g. ../backend/.env or the database)
    if candidate.is_file() and FRONTEND.resolve() in candidate.parents:
        return FileResponse(candidate)
    return FileResponse(FRONTEND / "index.html")
