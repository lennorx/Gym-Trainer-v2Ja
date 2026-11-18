import ast
import base64
import hashlib
import hmac
import json
import pathlib
import re
import secrets
import smtplib
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from contextlib import closing
from email.message import EmailMessage
from io import BytesIO

import streamlit as st
import streamlit.components.v1 as components
from google import genai
from google.genai import errors, types
from gtts import gTTS
from PIL import Image

try:
    from supabase import create_client
except ImportError:          # rulare locală fără supabase instalat → doar SQLite, fără conturi
    create_client = None

# ==========================================
# 1. CONFIGURARE PAGINĂ & CSS
# ==========================================
st.set_page_config(page_title="GymBro AI - Antrenor Virtual", page_icon="💪", layout="centered")

st.markdown("""
<style>
    .stChatMessage { font-size: 16px; border-radius: 10px; }
    div.stButton > button:first-child { background-color: #ff4b4b; color: white; border-radius: 20px; }
    footer {visibility: hidden;}

    .svg-container {
        background-color: white;
        padding: 20px;
        border-radius: 10px;
        border: 1px solid #ddd;
        text-align: center;
        margin: 10px 0;
        overflow: auto;
        box-shadow: 0 2px 5px rgba(0,0,0,0.1);
    }
    .svg-container img { max-width: 100%; height: auto; }

    /* Tabel styling pentru programe */
    table { width: 100%; border-collapse: collapse; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background-color: #f2f2f2; color: black; }
</style>
""", unsafe_allow_html=True)

# Câte mesaje anterioare trimitem modelului ca context
MAX_HISTORY_MESSAGES = 12
# Fișierele urcate la Gemini expiră după 48h; le reîncărcăm înainte de asta
FILE_MAX_AGE_SECONDS = 47 * 3600
SVG_PATTERN = re.compile(r"<svg\b.*?</svg>", re.IGNORECASE | re.DOTALL)
APP_NAME = "GymBro AI"


def _log(msg, err=None):
    # Nu scriem niciodată chei / parole în log — doar tipul și textul scurt al erorii
    print(f"[GymBro] {msg}" + (f": {type(err).__name__}: {str(err)[:200]}" if err else ""))


def is_valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


# Pod localStorage -> Python (ID-ul conversației + tokenul „ține-mă minte”), portat din Profesor ETTI.
_SESSION_BRIDGE_DIR = pathlib.Path(__file__).resolve().parent / "session_bridge"
_session_bridge = (
    components.declare_component("gymbro_session_bridge", path=str(_SESSION_BRIDGE_DIR))
    if (_SESSION_BRIDGE_DIR / "index.html").exists() else None
)

# ==========================================
# 2. STOCARE: Supabase (cu conturi) sau SQLite (local, fără conturi)
# ==========================================
DB_PATH = "chat_history.db"


@st.cache_resource(show_spinner=False)
def _create_supabase_client(url, key):
    return create_client(url, key)


def get_supabase_client():
    if create_client is None:
        return None
    try:
        url = st.secrets.get("SUPABASE_URL", "")
        key = st.secrets.get("SUPABASE_KEY", "")
    except Exception:
        return None
    if not url or not key:
        return None
    try:
        return _create_supabase_client(url, key)
    except Exception as e:
        _log("Supabase: clientul nu a putut fi creat", e)
        return None


SB = get_supabase_client()
ACCOUNTS_ENABLED = SB is not None


def _sqlite():
    return sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)


if not ACCOUNTS_ENABLED:
    with closing(_sqlite()) as _conn, _conn:
        _conn.execute("""CREATE TABLE IF NOT EXISTS history
                         (session_id TEXT, role TEXT, content TEXT, timestamp REAL)""")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_history_session ON history (session_id, timestamp)")


def session_register(session_id, user_id=None):
    if not ACCOUNTS_ENABLED:
        return
    now = time.time()
    try:
        SB.table("gym_sessions").upsert(
            {"session_id": session_id, "user_id": user_id, "created_at": now, "last_active": now},
            ignore_duplicates=True,
        ).execute()
    except Exception as e:
        _log("Înregistrare conversație eșuată", e)


def session_owner(session_id):
    """(există, user_id_proprietar_sau_None)."""
    if not ACCOUNTS_ENABLED:
        return False, None
    try:
        res = SB.table("gym_sessions").select("user_id").eq("session_id", session_id).limit(1).execute()
        if not res.data:
            return False, None
        return True, res.data[0].get("user_id")
    except Exception as e:
        _log("Citire proprietar conversație eșuată", e)
        return False, None


def save_message_to_db(session_id, role, content):
    if not ACCOUNTS_ENABLED:
        try:
            with closing(_sqlite()) as conn, conn:
                conn.execute("INSERT INTO history VALUES (?, ?, ?, ?)", (session_id, role, content, time.time()))
        except sqlite3.Error as e:
            _log("Eroare DB (save)", e)
        return
    now = time.time()
    try:
        SB.table("gym_history").insert(
            {"session_id": session_id, "role": role, "content": content, "created_at": now}
        ).execute()
        SB.table("gym_sessions").update({"last_active": now}).eq("session_id", session_id).execute()
        if role == "user":
            SB.table("gym_sessions").update({"title": content.strip()[:60]}) \
                .eq("session_id", session_id).is_("title", "null").execute()
    except Exception as e:
        _log("Eroare DB (save)", e)


def load_history_from_db(session_id):
    try:
        if not ACCOUNTS_ENABLED:
            with closing(_sqlite()) as conn:
                rows = conn.execute(
                    "SELECT role, content FROM history WHERE session_id=? ORDER BY timestamp ASC",
                    (session_id,),
                ).fetchall()
            pairs = rows
        else:
            res = SB.table("gym_history").select("role, content, created_at") \
                .eq("session_id", session_id).order("created_at").execute()
            pairs = [(r["role"], r["content"]) for r in res.data or []]
        return [{"role": role, "content": content} for role, content in pairs if content and content.strip()]
    except Exception as e:
        _log("Eroare DB (load)", e)
        return []


def clear_history_db(session_id):
    try:
        if not ACCOUNTS_ENABLED:
            with closing(_sqlite()) as conn, conn:
                conn.execute("DELETE FROM history WHERE session_id=?", (session_id,))
        else:
            SB.table("gym_history").delete().eq("session_id", session_id).execute()
            SB.table("gym_sessions").update({"title": None}).eq("session_id", session_id).execute()
    except Exception as e:
        _log("Eroare DB (clear)", e)


def list_user_sessions(user_id, limit=15):
    if not ACCOUNTS_ENABLED or not user_id:
        return []
    try:
        res = SB.table("gym_sessions").select("session_id, title, last_active") \
            .eq("user_id", user_id).order("last_active", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        _log("Lista conversațiilor eșuată", e)
        return []


def load_profile(user_id=None, session_id=None):
    """Profilul contului (gym_users.profile) sau, fără cont, al conversației (gym_sessions.profile)."""
    if not ACCOUNTS_ENABLED:
        return None
    try:
        if user_id:
            res = SB.table("gym_users").select("profile").eq("id", user_id).limit(1).execute()
        else:
            res = SB.table("gym_sessions").select("profile").eq("session_id", session_id).limit(1).execute()
        return (res.data[0].get("profile") if res.data else None) or None
    except Exception as e:
        _log("Citire profil eșuată", e)
        return None


def store_profile(profile, user_id=None, session_id=None):
    if not ACCOUNTS_ENABLED:
        return False
    try:
        if user_id:
            SB.table("gym_users").update({"profile": profile}).eq("id", user_id).execute()
        else:
            SB.table("gym_sessions").update({"profile": profile}).eq("session_id", session_id).execute()
        return True
    except Exception as e:
        _log("Salvare profil eșuată", e)
        return False


# ==========================================
# 3. AUTENTIFICARE OPȚIONALĂ (portată din Profesor ETTI)
# ==========================================
# Fără cont: aplicația merge ca înainte (conversația stă în acest browser).
# Cu cont: conversațiile sunt legate de cont (gym_sessions.user_id) și apar pe orice dispozitiv.
# „Ține-mă minte”: token aleator în localStorage; în DB doar sha256(token). Parolele: scrypt (stdlib).
AUTH_TOKEN_DAYS = 180
AUTH_MIN_PASSWORD = 8
AUTH_MAX_FAILS = 5            # încercări greșite per email ...
AUTH_FAIL_WINDOW = 600        # ... în 10 minute
RESET_CODE_MINUTES = 15
RESET_MAX_ATTEMPTS = 5
RESET_EMAIL_COOLDOWN = 60     # secunde între două emailuri către aceeași adresă
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # fără 0/O, 1/I
_EMAIL_RE = re.compile(r"^[^@\s:<>,;]{1,64}@[^@\s:<>,;]{1,255}\.[a-zA-Z]{2,24}$")
DB_DOWN = "Baza de date nu e disponibilă acum. Încearcă mai târziu."


def _normalize_email(email):
    """Litere mici, fără spații, fără „mailto:”, <> sau ?… (apar când emailul e copiat dintr-un link)."""
    e = (email or "").strip().strip("<>").strip()
    e = re.sub(r"^mailto:", "", e, flags=re.IGNORECASE)
    return e.split("?")[0].strip().lower()


def current_user():
    return st.session_state.get("_auth_user")


def current_user_id():
    u = current_user()
    return u.get("id") if u else None


def _hash_password(password):
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def _verify_password(password, stored):
    try:
        algo, n, r, p, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(dk_hex) // 2)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def _token_hash(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@st.cache_resource
def _get_auth_fail_store():
    return {"lock": threading.Lock(), "fails": defaultdict(list)}


def _auth_blocked(email):
    store = _get_auth_fail_store()
    with store["lock"]:
        recent = [t for t in store["fails"][email] if t > time.time() - AUTH_FAIL_WINDOW]
        store["fails"][email] = recent
        return len(recent) >= AUTH_MAX_FAILS


def _auth_note_fail(email):
    store = _get_auth_fail_store()
    with store["lock"]:
        store["fails"][email].append(time.time())


def _public_user(row):
    return {"id": row["id"], "email": row["email"],
            "name": row.get("display_name") or row["email"].split("@")[0]}


def _new_recovery_code():
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def _normalize_recovery(code):
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def _reset_code_hash(user_id, code):
    return hashlib.sha256(f"{user_id}:{code}".encode("utf-8")).hexdigest()


def auth_register(email, password, display_name=""):
    email = _normalize_email(email)
    if not _EMAIL_RE.match(email):
        return None, "Adresa de email nu pare validă."
    if len(password or "") < AUTH_MIN_PASSWORD:
        return None, f"Parola trebuie să aibă cel puțin {AUTH_MIN_PASSWORD} caractere."
    if not ACCOUNTS_ENABLED:
        return None, DB_DOWN
    try:
        if SB.table("gym_users").select("id").eq("email", email).limit(1).execute().data:
            return None, "Există deja un cont cu acest email. Folosește „Intră”."
        now = time.time()
        rec = _new_recovery_code()
        res = SB.table("gym_users").insert({
            "email": email,
            "password_hash": _hash_password(password),
            "recovery_hash": _hash_password(_normalize_recovery(rec)),
            "display_name": (display_name or "").strip()[:60] or None,
            "created_at": now,
            "last_login": now,
        }).execute()
        st.session_state["_auth_new_recovery"] = rec     # afișat o singură dată după creare
        return _public_user(res.data[0]), ""
    except Exception as e:
        _log("Înregistrare cont eșuată", e)
        return None, "Contul nu a putut fi creat. Încearcă din nou."


def auth_login(email, password):
    email = _normalize_email(email)
    if _auth_blocked(email):
        return None, "Prea multe încercări greșite. Așteaptă 10 minute."
    if not ACCOUNTS_ENABLED:
        return None, DB_DOWN
    try:
        res = SB.table("gym_users").select("id, email, display_name, password_hash") \
            .eq("email", email).limit(1).execute()
    except Exception as e:
        _log("Autentificare: eroare DB", e)
        return None, "Autentificarea nu a mers acum. Încearcă din nou."
    row = res.data[0] if res.data else None
    if not row or not _verify_password(password or "", row["password_hash"]):
        _auth_note_fail(email)
        return None, "Email sau parolă greșită."
    try:
        SB.table("gym_users").update({"last_login": time.time()}).eq("id", row["id"]).execute()
    except Exception:
        pass
    return _public_user(row), ""


def _smtp_config():
    try:
        user = st.secrets.get("SMTP_USER", "")
        pw = st.secrets.get("SMTP_PASSWORD", "")
        if not user or not pw:
            return None
        return {"user": user, "password": str(pw).replace(" ", ""),
                "host": st.secrets.get("SMTP_HOST", "smtp.gmail.com"),
                "port": int(st.secrets.get("SMTP_PORT", 465)),
                "sender": st.secrets.get("SMTP_FROM_NAME", APP_NAME)}
    except Exception:
        return None


def _send_email(to, subject, body):
    cfg = _smtp_config()
    if not cfg:
        return False
    msg = EmailMessage()
    msg["From"] = f"{cfg['sender']} <{cfg['user']}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20) as srv:
            srv.login(cfg["user"], cfg["password"])
            srv.send_message(msg)
        return True
    except Exception as e:
        _log("Trimiterea emailului a eșuat", e)
        return False


@st.cache_resource
def _get_reset_mail_store():
    return {"lock": threading.Lock(), "last": {}}


def auth_request_reset_email(email):
    """Cod de 6 cifre pe email. Același mesaj dacă emailul există sau nu."""
    email = _normalize_email(email)
    if not _EMAIL_RE.match(email):
        return False, "Adresa de email nu pare validă."
    if not _smtp_config():
        return False, "Trimiterea de emailuri nu e configurată. Folosește codul de recuperare."
    store = _get_reset_mail_store()
    with store["lock"]:
        if time.time() - store["last"].get(email, 0) < RESET_EMAIL_COOLDOWN:
            return False, "Ți-am trimis deja un cod. Verifică emailul (și Spam) sau încearcă peste un minut."
        store["last"][email] = time.time()
    generic = (f"Dacă există un cont cu {email}, am trimis un cod de 6 cifre (valabil "
               f"{RESET_CODE_MINUTES} minute). Verifică și folderul Spam.")
    if not ACCOUNTS_ENABLED:
        return False, DB_DOWN
    try:
        res = SB.table("gym_users").select("id").eq("email", email).limit(1).execute()
        if not res.data:
            return True, generic
        uid = res.data[0]["id"]
        code = f"{secrets.randbelow(10 ** 6):06d}"
        now = time.time()
        SB.table("gym_password_resets").upsert({
            "user_id": uid, "code_hash": _reset_code_hash(uid, code),
            "expires_at": now + RESET_CODE_MINUTES * 60, "attempts": 0, "created_at": now,
        }).execute()
    except Exception as e:
        _log("Cerere resetare parolă: eroare DB", e)
        return False, "Nu am putut genera codul acum. Încearcă din nou."
    body = (f"Salut!\n\nCodul tău pentru resetarea parolei în {APP_NAME} este:\n\n    {code}\n\n"
            f"Codul e valabil {RESET_CODE_MINUTES} minute. Dacă nu ai cerut tu resetarea, "
            "ignoră acest email: parola ta rămâne neschimbată.\n")
    if not _send_email(email, f"Codul tău {APP_NAME}: {code}", body):
        return False, "Emailul nu a putut fi trimis acum. Încearcă din nou sau folosește codul de recuperare."
    return True, generic


def auth_reset_password(email, code, new_password):
    """Parolă nouă cu codul din email (6 cifre) SAU codul de recuperare. Deconectează toate dispozitivele."""
    email = _normalize_email(email)
    code = (code or "").strip()
    if len(new_password or "") < AUTH_MIN_PASSWORD:
        return None, f"Parola nouă trebuie să aibă cel puțin {AUTH_MIN_PASSWORD} caractere."
    if _auth_blocked(email):
        return None, "Prea multe încercări greșite. Așteaptă 10 minute."
    if not ACCOUNTS_ENABLED:
        return None, DB_DOWN
    bad = "Email sau cod greșit (ori codul a expirat)."
    try:
        res = SB.table("gym_users").select("id, email, display_name, recovery_hash") \
            .eq("email", email).limit(1).execute()
        row = res.data[0] if res.data else None
        if not row:
            _auth_note_fail(email)
            return None, bad
        uid = row["id"]
        used_recovery = False
        if re.fullmatch(r"\d{6}", code):
            r = SB.table("gym_password_resets").select("code_hash, expires_at, attempts") \
                .eq("user_id", uid).limit(1).execute()
            rr = r.data[0] if r.data else None
            if (not rr or rr["expires_at"] < time.time() or rr["attempts"] >= RESET_MAX_ATTEMPTS
                    or not hmac.compare_digest(rr["code_hash"], _reset_code_hash(uid, code))):
                if rr:
                    SB.table("gym_password_resets").update({"attempts": rr["attempts"] + 1}) \
                        .eq("user_id", uid).execute()
                _auth_note_fail(email)
                return None, bad
        else:
            if not row.get("recovery_hash") or not _verify_password(_normalize_recovery(code), row["recovery_hash"]):
                _auth_note_fail(email)
                return None, bad
            used_recovery = True
        update = {"password_hash": _hash_password(new_password), "last_login": time.time()}
        new_rec = None
        if used_recovery:
            new_rec = _new_recovery_code()          # codul de recuperare e de unică folosință
            update["recovery_hash"] = _hash_password(_normalize_recovery(new_rec))
        SB.table("gym_users").update(update).eq("id", uid).execute()
        SB.table("gym_password_resets").delete().eq("user_id", uid).execute()
        SB.table("gym_tokens").delete().eq("user_id", uid).execute()      # deconectăm peste tot
        if new_rec:
            st.session_state["_auth_new_recovery"] = new_rec
        return _public_user(row), ""
    except Exception as e:
        _log("Resetare parolă eșuată", e)
        return None, "Parola nu a putut fi schimbată acum. Încearcă din nou."


def auth_regenerate_recovery():
    uid = current_user_id()
    if not uid or not ACCOUNTS_ENABLED:
        return False
    rec = _new_recovery_code()
    try:
        SB.table("gym_users").update({"recovery_hash": _hash_password(_normalize_recovery(rec))}) \
            .eq("id", uid).execute()
        st.session_state["_auth_new_recovery"] = rec
        return True
    except Exception as e:
        _log("Cod de recuperare nou eșuat", e)
        return False


def _dismiss_recovery():
    st.session_state.pop("_auth_new_recovery", None)


def _issue_remember_token(user_id):
    raw = secrets.token_urlsafe(32)
    now = time.time()
    try:
        SB.table("gym_tokens").insert({
            "token_hash": _token_hash(raw), "user_id": user_id, "created_at": now,
            "expires_at": now + AUTH_TOKEN_DAYS * 86400, "last_used": now,
        }).execute()
        return raw
    except Exception as e:
        _log("Token „ține-mă minte” nu a putut fi creat", e)
        return None


def _user_from_token(raw):
    """Utilizatorul pentru token (și prelungește valabilitatea), sau None."""
    if not ACCOUNTS_ENABLED or not raw or not isinstance(raw, str) or len(raw) > 200:
        return None
    try:
        th = _token_hash(raw)
        res = SB.table("gym_tokens").select("user_id, expires_at").eq("token_hash", th).limit(1).execute()
        if not res.data or res.data[0]["expires_at"] < time.time():
            return None
        u = SB.table("gym_users").select("id, email, display_name") \
            .eq("id", res.data[0]["user_id"]).limit(1).execute()
        if not u.data:
            return None
        now = time.time()
        SB.table("gym_tokens").update({"last_used": now, "expires_at": now + AUTH_TOKEN_DAYS * 86400}) \
            .eq("token_hash", th).execute()
        SB.table("gym_users").update({"last_login": now}).eq("id", u.data[0]["id"]).execute()
        return _public_user(u.data[0])
    except Exception as e:
        _log("Restaurare login din token eșuată", e)
        return None


def _set_browser_token(raw):
    """Programează scrierea/ștergerea tokenului în localStorage (vezi sync_browser_storage)."""
    st.session_state["_auth_ls_js"] = (
        f"localStorage.setItem('gymbro_auth_token', {json.dumps(raw)});" if raw
        else "localStorage.removeItem('gymbro_auth_token');"
    )
    st.session_state["_auth_raw_token"] = raw


def switch_session(new_sid):
    st.session_state.session_id = new_sid
    st.session_state.pop("messages", None)
    st.session_state.pop("media", None)
    st.session_state["uploader_key"] = st.session_state.get("uploader_key", 0) + 1
    st.query_params["session_id"] = new_sid


def new_session():
    sid = str(uuid.uuid4())
    session_register(sid, current_user_id())
    switch_session(sid)


def auth_complete_login(user, remember):
    st.session_state["_auth_user"] = user
    # Conversația anonimă curentă din acest browser trece în cont
    sid = st.session_state.get("session_id")
    if ACCOUNTS_ENABLED and sid:
        try:
            SB.table("gym_sessions").update({"user_id": user["id"]}) \
                .eq("session_id", sid).is_("user_id", "null").execute()
        except Exception as e:
            _log("Atașarea conversației la cont a eșuat", e)
    _set_browser_token(_issue_remember_token(user["id"]) if remember else None)


def auth_logout(all_devices=False):
    user = current_user()
    raw = st.session_state.get("_auth_raw_token") or (st.session_state.get("_browser_storage") or {}).get("token")
    try:
        if user and all_devices:
            SB.table("gym_tokens").delete().eq("user_id", user["id"]).execute()
        elif raw:
            SB.table("gym_tokens").delete().eq("token_hash", _token_hash(raw)).execute()
    except Exception as e:
        _log("Ștergere token la deconectare eșuată", e)
    st.session_state.pop("_auth_user", None)
    st.session_state.pop("profile", None)
    st.session_state.pop("_profile_owner", None)
    if isinstance(st.session_state.get("_browser_storage"), dict):
        st.session_state["_browser_storage"]["token"] = ""
    _set_browser_token(None)
    # Conversația curentă aparține contului: pe acest browser pornim una nouă, anonimă
    new_session()


def load_browser_storage_and_login():
    """O dată per sesiune Streamlit: citește din browser SID-ul și tokenul (session_bridge),
    apoi restaurează automat contul din token."""
    if "_browser_storage" not in st.session_state:
        val = _session_bridge(key="_session_bridge", default=None) if _session_bridge is not None else {}
        if val is None:
            st.markdown(
                '<div style="display:flex;align-items:center;justify-content:center;'
                'min-height:40vh;color:#888;font-size:15px;">💪 Se încarcă...</div>',
                unsafe_allow_html=True,
            )
            st.caption("Dacă pagina rămâne aici mai mult de câteva secunde, apasă mai jos.")
            if st.button("🔄 Continuă", key="_session_gate_manual_continue"):
                st.session_state["_browser_storage"] = {}
                st.rerun()
            st.stop()
        st.session_state["_browser_storage"] = val if isinstance(val, dict) else {}
    if not st.session_state.get("_auth_restore_done"):
        st.session_state["_auth_restore_done"] = True
        raw = (st.session_state["_browser_storage"] or {}).get("token") or ""
        if raw and not current_user() and ACCOUNTS_ENABLED:
            user = _user_from_token(raw)
            if user:
                st.session_state["_auth_user"] = user
                st.session_state["_auth_raw_token"] = raw
            else:
                _set_browser_token(None)        # token expirat/revocat → îl scoatem din browser


def resolve_session():
    """Alege conversația: ?session_id= din URL, apoi cea salvată în browser, altfel una nouă.
    O conversație care aparține ALTUI cont nu se deschide (link-ul nu mai e suficient)."""
    if "session_id" in st.session_state:
        st.query_params["session_id"] = st.session_state.session_id
        return
    storage = st.session_state.get("_browser_storage") or {}
    candidate = st.query_params.get("session_id") or storage.get("sid") or ""
    sid = candidate if is_valid_uuid(candidate) else str(uuid.uuid4())
    exists, owner = session_owner(sid)
    if exists and owner and owner != current_user_id():
        sid, exists = str(uuid.uuid4()), False
    if not exists:
        session_register(sid, current_user_id())
    st.session_state.session_id = sid
    st.query_params["session_id"] = sid


def sync_browser_storage():
    """Scrie în localStorage SID-ul curent și (o singură dată) tokenul. Conținut identic la rerun =
    iframe-ul nu se re-execută."""
    js = f"localStorage.setItem('gymbro_session_id', {json.dumps(st.session_state.session_id)});"
    js += st.session_state.get("_auth_ls_js") or ""
    components.html(f"<script>try{{{js}}}catch(e){{}}</script>", height=0)


PROFILE_WIDGET_KEYS = {"age_group": "_prof_age", "experience": "_prof_experience", "goal": "_prof_goal",
                       "days": "_prof_days", "equipment": "_prof_equipment", "limits": "_prof_limits"}


def _put_profile_in_widgets(profile):
    # Valorile widget-urilor se setează DOAR prin session_state, înainte de randare (fără value= + key=)
    profile = profile or {}
    for field, key in PROFILE_WIDGET_KEYS.items():
        value = profile.get(field)
        if field == "limits":
            st.session_state[key] = (value or "")[:300]
            continue
        options = list(AGE_GROUPS) if field == "age_group" else PROFILE_FIELDS[field][1]
        st.session_state[key] = value if value in options else None   # opțiuni redenumite → gol, nu eroare


def ensure_profile_loaded():
    """Încarcă profilul când se schimbă proprietarul (intrare/ieșire din cont, altă conversație anonimă).
    La intrarea în cont, profilul completat anonim trece în cont dacă acesta nu are încă unul."""
    uid = current_user_id()
    owner = f"user:{uid}" if uid else f"sid:{st.session_state.session_id}"
    previous = st.session_state.get("_profile_owner")
    if previous == owner:
        return
    anon_profile = st.session_state.get("profile") if (previous or "").startswith("sid:") else None
    profile = load_profile(user_id=uid) if uid else load_profile(session_id=st.session_state.session_id)
    if uid and not profile and anon_profile:
        store_profile(anon_profile, user_id=uid)
        profile = anon_profile
    if not uid and not profile and previous is None:
        profile = st.session_state.get("profile")      # rulare locală fără Supabase
    st.session_state["profile"] = profile or {}
    st.session_state["_profile_owner"] = owner
    _put_profile_in_widgets(profile)


def save_profile_from_widgets():
    profile = {}
    for field, key in PROFILE_WIDGET_KEYS.items():
        value = st.session_state.get(key)
        if isinstance(value, str):
            value = value.strip()[:300]
        if value:
            profile[field] = value
    st.session_state["profile"] = profile
    uid = current_user_id()
    stored = store_profile(profile, user_id=uid) if uid else \
        store_profile(profile, session_id=st.session_state.session_id)
    st.session_state["_profile_saved_msg"] = (
        "✅ Profil salvat. Îl folosesc de la următorul mesaj." if stored or not ACCOUNTS_ENABLED
        else "⚠️ Profilul e folosit acum, dar nu l-am putut salva în baza de date."
    )


load_browser_storage_and_login()
resolve_session()

# ==========================================
# 4. CHEI API & LANȚ DE MODELE (rotație chei × modele, ca în Profesor ETTI)
# ==========================================


def parse_secret_list(raw):
    """Acceptă listă TOML, string cu listă Python sau un singur string."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            raw = [raw]
    if isinstance(raw, str):
        raw = [raw]
    result = []
    for item in raw:
        if item and isinstance(item, str):
            clean = item.strip().strip('"').strip("'")
            if clean and clean not in result:
                result.append(clean)
    return result


keys = []
if "GOOGLE_API_KEYS" in st.secrets:
    keys = parse_secret_list(st.secrets["GOOGLE_API_KEYS"])
elif "GOOGLE_API_KEY" in st.secrets:
    keys = parse_secret_list(st.secrets["GOOGLE_API_KEY"])

# Ordinea = cota zilnică mare întâi, apoi rezervele mici (cote per PROIECT Google și per model).
# Se poate suprascrie din secrets cu GEMINI_MODELS = ["...", "..."].
DEFAULT_MODEL_CHAIN = [
    "gemini-3.1-flash-lite",    # 500 RPD
    "gemini-3.5-flash-lite",    # 500 RPD
    "gemini-3.6-flash",         # 20 RPD
    "gemini-3-flash-preview",   # 20 RPD
    "gemini-3.8-flash",         # 20 RPD
    "gemini-2.5-flash",         # 20 RPD (gândire prin thinking_budget)
    "gemini-2.5-flash-lite",    # 20 RPD
]
MODEL_CHAIN = (parse_secret_list(st.secrets.get("GEMINI_MODELS")) if "GEMINI_MODELS" in st.secrets else []) \
    or list(DEFAULT_MODEL_CHAIN)

# Dacă nu găsim chei în secrets, cerem manual
if not keys:
    with st.sidebar:
        manual_key = st.text_input("API Key (Manual):", type="password")
        if manual_key and manual_key.strip():
            keys.append(manual_key.strip())

if "key_index" not in st.session_state or st.session_state.key_index >= max(len(keys), 1):
    st.session_state.key_index = 0


@st.cache_resource(show_spinner=False)
def get_client(api_key):
    # Un client separat pe cheie (fără configurare globală): utilizatorii simultani nu se încurcă.
    return genai.Client(api_key=api_key)


# --- Memorie COMUNĂ (pentru toți utilizatorii, cât trăiește serverul) a combinațiilor epuizate ---
#   limită pe zi → ocolită până la resetare (miezul nopții Pacific ≈ 10:00 ora României)
#   limită pe minut → retryDelay (5 s – 5 min); supraîncărcare → 30 s
#   model inexistent (404) → 6 ore pe toate cheile; cheie invalidă → 1 oră pe toate modelele
@st.cache_resource
def _get_quota_state():
    return {"lock": threading.Lock(), "blocked": {}}   # (amprentă_cheie | "*", model | "*") -> până_la


def _key_fp(api_key):
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _next_quota_reset():
    from datetime import datetime, timedelta, timezone
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0).timestamp()
    except Exception:
        return (datetime.now(timezone.utc) + timedelta(hours=24)).timestamp()


def _quota_block(api_key, model, until):
    state = _get_quota_state()
    k = (_key_fp(api_key) if api_key else "*", model or "*")
    with state["lock"]:
        state["blocked"][k] = max(until, state["blocked"].get(k, 0))


def _quota_blocked_until(api_key, model):
    state = _get_quota_state()
    now = time.time()
    fp = _key_fp(api_key)
    with state["lock"]:
        b = state["blocked"]
        for k in [k for k, v in b.items() if v <= now]:
            del b[k]
        return max(b.get((fp, model), 0), b.get((fp, "*"), 0), b.get(("*", model), 0))


def _quota_until_from_error(msg):
    low = msg.lower()
    if "perday" in low or "per_day" in low or "per day" in low or "requests_per_day" in low or "rpd" in low:
        return _next_quota_reset()
    m = re.search(r"retry(?:_delay|delay)?[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*s", low)
    wait = float(m.group(1)) if m else 60.0
    return time.time() + min(max(wait, 5.0), 300.0)


def _pick_key_and_model():
    """Prima combinație liberă: cheia curentă întâi (toate modelele), apoi celelalte chei."""
    n = len(keys)
    start = st.session_state.get("key_index", 0) % max(n, 1)
    for off in range(n):
        ki = (start + off) % n
        for mi, m in enumerate(MODEL_CHAIN):
            if _quota_blocked_until(keys[ki], m) <= time.time():
                return ki, mi
    return None


def _thinking_config_for(model_name):
    """Gemini 3.x: thinking_level; Gemini 2.5: thinking_budget (nu acceptă thinking_level)."""
    if model_name.startswith("gemini-2.5"):
        return types.ThinkingConfig(thinking_budget=0)
    return types.ThinkingConfig(thinking_level="low")


# --- PROMPT-UL SISTEMULUI: bază comună + reguli după vârstă + profilul utilizatorului ---
BASE_PROMPT = """
Ești un Antrenor Personal Virtual și Nutriționist numit "GymBro AI". Lucrezi cu oameni de TOATE vârstele
(copii, adolescenți, adulți, vârstnici) și îți adaptezi sfaturile la vârsta, nivelul și obiectivul fiecăruia.

IDENTITATE:
1. Folosește EXCLUSIV genul masculin când vorbești despre tine.
   - Corect: "Sunt sigur", "Sunt pregătit", "Am fost atent", "Sunt bucuros".
   - GREȘIT: "Sunt sigură", "Sunt pregătită".
2. Te prezinți ca "Antrenorul tău personal virtual".

TON ȘI ADRESARE:
3. Vorbește DIRECT, la persoana I singular ("Eu cred", "Te ajut", "Sunt pregătit").
   - GREȘIT: "Domnul Antrenor este aici." / "Antrenorul te va ajuta."
4. Adresează-te utilizatorului direct, la persoana a II-a ("Tu trebuie să faci").
5. Prietenos, motivațional, clar, responsabil. Folosește emoji-uri 💪🥗🔥 cu măsură.
   Potrivește tonul cu vârsta: mai relaxat cu tinerii, mai așezat cu adulții și vârstnicii.
6. Fii cald, natural și scurt. Evită introducerile pompoase.
7. NU SALUTA în fiecare mesaj. Salută DOAR la începutul unei conversații noi.

REGULI DE AUR:
1. Pentru programe de antrenament folosește OBLIGATORIU tabele Markdown.
2. Forma corectă înaintea greutății. Progresie treptată: crești încărcătura doar când toate seriile
   ies curat, cu tehnică bună.
3. Nutriție: mâncare reală și variată, cu exemple concrete de mese și gustări (inclusiv mâncare
   obișnuită în România și variante ieftine de proteine: ouă, iaurt, brânză, fasole, linte, pui, pește).
4. Fii realist și nu fi ca influencerii de fitness: fără promisiuni de transformări rapide.
5. Dacă nu știi vârsta, obiectivul sau echipamentul și contează pentru răspuns, întreabă scurt.

REPERE GENERALE (ghidurile OMS pentru activitate fizică):
- 5–17 ani: în medie 60 de minute pe zi de activitate moderată sau intensă; exerciții intense și
  pentru mușchi și oase de cel puțin 3 ori pe săptămână.
- 18–64 ani: 150–300 de minute pe săptămână de activitate moderată (sau 75–150 intensă) și exerciții
  de forță pentru toate grupele mari de mușchi de cel puțin 2 ori pe săptămână.
- 65+ ani: la fel ca adulții, plus exerciții de echilibru și forță funcțională de cel puțin 3 ori pe săptămână.

SIGURANȚĂ (TOATE VÂRSTELE):
1. Dacă utilizatorul pomenește durere, o accidentare, amețeli, leșin, dureri în piept, lipsă de aer
   neobișnuită sau o boală, spune-i să oprească efortul și să meargă la medic sau kinetoterapeut.
   La dureri în piept sau leșin: 112. Nu pune diagnostice.
2. Fără diete extreme, fără post prelungit, fără metode de "slăbit rapid" sau de deshidratare.
3. Dacă observi semne de relație nesănătoasă cu mâncarea sau cu propriul corp (vrea să slăbească extrem
   de repede, sare peste mese, vomită după masă, face sport ca să "ardă" ce a mâncat, se urăște pentru
   cum arată), răspunde cu căldură, NU oferi planuri de slăbit și încurajează-l să vorbească cu un medic
   sau un psiholog (iar un minor și cu un părinte sau consilierul școlar).
4. Niciodată steroizi anabolizanți, SARMs, prohormoni, "fat burners", diuretice sau laxative pentru
   slăbit, la nicio vârstă.

OBIECTIVELE TALE:
1. Să creezi planuri de antrenament organizate pe ZILE și SĂPTĂMÂNI, potrivite vârstei și nivelului.
2. Să explici corect execuția exercițiilor pentru a evita accidentările.
3. Să oferi sfaturi nutriționale sănătoase și sustenabile.
4. Să răspunzi la întrebări despre sală, exerciții acasă, sport și recuperare.
"""

AGE_GROUPS = {
    "u14": "sub 14 ani",
    "14-17": "14–17 ani",
    "18-39": "18–39 ani",
    "40-64": "40–64 ani",
    "65+": "65 de ani sau peste",
}

_MINOR_RULES = """
2. (CRITIC) Nu recomanda NICIODATĂ suplimente: creatină, proteine pudră, pre-workout, băuturi energizante
   sau orice altă pastilă/pulbere. Doar mâncare reală.
3. Fără deficit caloric, fără numărat obsesiv de calorii sau cântărit zilnic, fără ținte de greutate
   corporală: corpul încă se dezvoltă.
4. La durere sau orice problemă de sănătate: să anunțe și un părinte.
5. La semne de relație nesănătoasă cu mâncarea sau corpul, menționează și Telefonul Copilului:
   116 111 (gratuit).
"""

AGE_RULES = {
    "u14": """
REGULI PENTRU VÂRSTA UTILIZATORULUI (sub 14 ani):
1. Accent pe joacă, sport și mișcare variată. Forța se lucrează mai ales cu greutatea corpului
   (genuflexiuni, flotări ușurate, plank, sărituri); greutăți libere DOAR sub supravegherea unui
   antrenor sau a unui adult, cu încărcătură mică și multe repetări corecte.
""" + _MINOR_RULES,
    "14-17": """
REGULI PENTRU VÂRSTA UTILIZATORULUI (14–17 ani):
1. Antrenamentul de forță e sigur și util dacă tehnica e corectă și progresia treptată. Pentru cei sub 16
   ani, la greutăți libere recomandă supravegherea unui antrenor sau a unui adult.
""" + _MINOR_RULES,
    "18-39": """
REGULI PENTRU VÂRSTA UTILIZATORULUI (18–39 ani):
1. Poți folosi programe clasice (full body, upper/lower, push/pull/legs), suprasarcină progresivă,
   perioade de descărcare.
2. Suplimente: mâncarea vine întâi. Dacă te întreabă, poți explica obiectiv proteina pudră (o simplă
   comoditate) și creatina monohidrat (cel mai studiat supliment, dozele uzuale), fără să insiști.
   Recomandă medicul înainte de orice supliment dacă are o boală (ex. de rinichi) sau ia medicamente.
3. Slăbit: deficit moderat, sustenabil (fără regimuri extreme), cu proteine suficiente și antrenament
   de forță ca să-și păstreze masa musculară.
""",
    "40-64": """
REGULI PENTRU VÂRSTA UTILIZATORULUI (40–64 ani):
1. Încălzire mai lungă, accent pe mobilitate, articulații și recuperare (somn, zile de pauză).
2. Dacă a fost sedentar mult timp, are tensiune mare, boli de inimă, diabet sau alte afecțiuni,
   recomandă un control medical înainte de a crește intensitatea.
3. Forța e foarte importantă la această vârstă (masă musculară, oase); progresie treptată.
4. Suplimente: mâncarea vine întâi; informații obiective doar dacă întreabă, plus discuția cu medicul
   dacă ia medicamente.
5. Slăbit: deficit moderat și sustenabil, nu regimuri extreme.
""",
    "65+": """
REGULI PENTRU VÂRSTA UTILIZATORULUI (65+ ani):
1. Prioritate: echilibru, prevenirea căderilor, forță funcțională (ridicatul de pe scaun, urcatul
   scărilor, mersul), mobilitate.
2. Recomandă acordul medicului înainte de un program nou, mai ales cu boli cronice sau medicamente.
3. Început foarte blând, progresie lentă, sprijin la nevoie (scaun, perete). Fără sărituri sau
   mișcări bruște până nu e sigur pe echilibru.
4. Fără slăbit agresiv: la această vârstă pierderea de mușchi e un risc; proteine suficiente.
5. Suplimente doar după discuția cu medicul.
""",
    None: """
VÂRSTA UTILIZATORULUI NU E CUNOSCUTĂ:
1. La începutul conversației, întreabă scurt ce vârstă are (sau să completeze "🏋️ Profilul meu" din
   bară), ca să-ți adaptezi sfaturile.
2. Până afli vârsta, aplică regulile prudente: fără suplimente, fără deficit caloric, fără ținte de
   greutate, progresie foarte treptată.
""",
}

PROFILE_FIELDS = {
    "experience": ("Nivel", ["Începător", "Intermediar", "Avansat"]),
    "goal": ("Obiectiv", ["Sănătate și condiție fizică", "Forță", "Masă musculară", "Slăbit sănătos",
                          "Rezistență / cardio", "Mobilitate și postură", "Pregătire pentru un sport"]),
    "days": ("Zile de antrenament pe săptămână", ["1", "2", "3", "4", "5", "6"]),
    "equipment": ("Unde te antrenezi", ["Sală complet echipată", "Acasă, cu gantere / benzi",
                                        "Doar cu greutatea corpului", "În aer liber"]),
}


def build_system_prompt(profile):
    profile = profile or {}
    group = profile.get("age_group") if profile.get("age_group") in AGE_GROUPS else None
    prompt = BASE_PROMPT + AGE_RULES[group]
    lines = []
    if group:
        lines.append(f"- Vârstă: {AGE_GROUPS[group]}")
    for field, (label, _options) in PROFILE_FIELDS.items():
        if profile.get(field):
            lines.append(f"- {label}: {profile[field]}")
    if (profile.get("limits") or "").strip():
        lines.append(f"- Accidentări / limitări / afecțiuni spuse de utilizator: {profile['limits'].strip()[:300]}")
    if lines:
        prompt += ("\nPROFILUL UTILIZATORULUI (completat de el; folosește-l, nu-l mai întreba ce știi deja):\n"
                   + "\n".join(lines) + "\n")
    return prompt


# Filtre de siguranță: aplicația are și utilizatori minori, deci NU le dezactivăm
SAFETY_SETTINGS = [
    types.SafetySetting(category=category, threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE)
    for category in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]


class EmptyResponseError(Exception):
    """Modelul n-a întors niciun text (de obicei răspuns blocat de filtre)."""


class FatalRequestError(Exception):
    """Eroare care nu se rezolvă schimbând cheia sau modelul."""


def get_media_part(client, key_idx):
    """Materialul atașat, în forma potrivită pentru cheia dată (sau None)."""
    media = st.session_state.get("media")
    if not media:
        return None
    if media["kind"] == "image":
        return types.Part.from_bytes(data=media["data"], mime_type=media["mime"])

    # PDF: fișierele urcate aparțin proiectului cheii → îl urcăm o dată pe fiecare cheie folosită
    cached = media["files"].get(key_idx)
    if cached and time.time() - cached["uploaded_at"] < FILE_MAX_AGE_SECONDS:
        return types.Part.from_uri(file_uri=cached["file"].uri, mime_type="application/pdf")
    uploaded = client.files.upload(
        file=BytesIO(media["data"]),
        config=types.UploadFileConfig(mime_type="application/pdf", display_name=media["name"]),
    )
    deadline = time.time() + 120
    while uploaded.state == types.FileState.PROCESSING:
        if time.time() > deadline:
            raise FatalRequestError("Procesarea PDF-ului durează prea mult. Încearcă un fișier mai mic.")
        time.sleep(1)
        uploaded = client.files.get(name=uploaded.name)
    if uploaded.state == types.FileState.FAILED:
        raise FatalRequestError("Gemini nu a putut procesa PDF-ul.")
    media["files"][key_idx] = {"file": uploaded, "uploaded_at": time.time()}
    return types.Part.from_uri(file_uri=uploaded.uri, mime_type="application/pdf")


def _error_text(e):
    details = getattr(e, "details", None)
    try:
        details = json.dumps(details, ensure_ascii=False)
    except (TypeError, ValueError):
        details = str(details)
    return f"{e} {details}"


def run_chat_with_rotation(history, user_text):
    """Generează răspunsul în stream. La fiecare încercare alege prima combinație (cheie, model)
    neepuizată; o eroare de cotă o marchează și trece imediat la următoarea."""
    if not keys:
        raise FatalRequestError("Nicio cheie API Gemini configurată.")
    deadline = time.time() + 60
    last_kind = None

    for _ in range(len(keys) * len(MODEL_CHAIN) + 6):
        pick = _pick_key_and_model()
        if pick is None:
            if last_kind == "overload":
                raise FatalRequestError("Serviciul AI e supraîncărcat. Încearcă din nou în câteva secunde. 🐢")
            raise FatalRequestError(
                "Toate cheile API sunt epuizate sau invalide pentru azi. "
                "Cotele se resetează în jurul orei 10:00 (ora României). 🔑"
            )
        key_idx, model_idx = pick
        if key_idx != st.session_state.key_index:
            st.session_state.key_index = key_idx
            st.toast(f"🔄 Trec pe cheia {key_idx + 1}/{len(keys)}...", icon="🔑")
        api_key = keys[key_idx]
        model_name = MODEL_CHAIN[model_idx]
        client = get_client(api_key)
        started = False
        try:
            parts = []
            media_part = get_media_part(client, key_idx)
            if media_part is not None:
                parts.append(media_part)
            parts.append(types.Part(text=user_text))
            contents = history + [types.UserContent(parts=parts)]
            config = types.GenerateContentConfig(
                system_instruction=build_system_prompt(st.session_state.get("profile")),
                safety_settings=SAFETY_SETTINGS,
                thinking_config=_thinking_config_for(model_name),
            )
            for chunk in client.models.generate_content_stream(model=model_name, contents=contents, config=config):
                text = chunk.text
                if text:
                    started = True
                    yield text
            if not started:
                raise EmptyResponseError()
            if model_idx > 0:
                st.toast(f"ℹ️ Răspuns generat cu modelul de rezervă ({model_name})", icon="🔄")
            return

        except (EmptyResponseError, FatalRequestError):
            raise
        except Exception as e:
            if started:
                # Utilizatorul a primit deja o parte din răspuns: o reîncercare ar dubla textul
                raise FatalRequestError("Răspunsul s-a întrerupt. Te rog trimite din nou mesajul.") from e
            code = getattr(e, "code", None)
            msg = _error_text(e)
            low = msg.lower()
            is_invalid_key = ("api key not valid" in low or "api_key_invalid" in low
                              or "invalid api key" in low or code == 401)
            is_quota = code == 429 or re.search(r"\b429\b", msg) is not None or "resource_exhausted" in low \
                or "quota" in low
            is_missing_model = code == 404 or (code is None and re.search(r"\b404\b", msg) is not None) \
                or "is not supported for generatecontent" in low
            is_overload = (code is not None and code >= 500) or "overloaded" in low or "unavailable" in low

            if is_invalid_key or (code == 403 and "permission" in low and "file" not in low):
                last_kind = "invalid"
                _quota_block(api_key, None, time.time() + 3600)
                _log(f"cheia {key_idx + 1} invalidă: ocolită 1 oră")
                continue
            if is_quota:
                last_kind = "quota"
                _quota_block(api_key, model_name, _quota_until_from_error(msg))
                st.toast(f"⚠️ Limită atinsă pe {model_name}, încerc altă variantă...", icon="🔄")
                continue
            if is_missing_model:
                last_kind = "missing"
                _quota_block(None, model_name, time.time() + 6 * 3600)
                _log(f"modelul {model_name} nu e disponibil: scos din lanț 6 ore")
                continue
            if is_overload:
                last_kind = "overload"
                _quota_block(api_key, model_name, time.time() + 30)
                if time.time() >= deadline:
                    raise FatalRequestError("Serviciul AI e supraîncărcat. Încearcă din nou în câteva secunde. 🐢") from e
                st.toast(f"🐢 {model_name} ocupat, încerc altă variantă...", icon="⏳")
                time.sleep(0.5)
                continue
            if isinstance(e, errors.APIError):
                # 400 și alte erori de cerere: altă cheie sau alt model nu ajută
                raise FatalRequestError(f"Cerere respinsă de Gemini ({code}): {getattr(e, 'message', e)}") from e
            # Erori de rețea etc.: încercăm altă combinație
            last_kind = "overload"
            _quota_block(api_key, model_name, time.time() + 30)
            _log("eroare de rețea", e)
            continue

    raise FatalRequestError("Serviciul AI e momentan supraîncărcat. Încearcă din nou în câteva secunde. 🙏")


def build_history(messages):
    """Istoric valid pentru Gemini: fără mesaje goale, roluri alternante, începe cu 'user'.
    Întoarce (istoric, text_user_rămas_fără_răspuns)."""
    contents = []
    for msg in messages:
        text = (msg.get("content") or "").strip()
        if not text:
            continue
        role = "model" if msg["role"] == "assistant" else "user"
        if contents and contents[-1].role == role:
            contents[-1].parts.append(types.Part(text=text))
        else:
            contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
    while contents and contents[0].role != "user":
        contents.pop(0)
    # Ultimul mesaj al userului fără răspuns (eroare anterioară) se lipește de mesajul curent
    pending = ""
    if contents and contents[-1].role == "user":
        pending = "\n\n".join(p.text for p in contents.pop().parts if p.text)
    return contents, pending


# ==========================================
# 5. RANDARE MESAJE & AUDIO
# ==========================================


def render_svg(svg_code):
    if "xmlns" not in svg_code[:200]:
        svg_code = svg_code.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    # Randat ca imagine: scripturile/handler-ele din SVG nu se execută într-un <img>
    encoded = base64.b64encode(svg_code.encode("utf-8")).decode("ascii")
    st.markdown(
        f'<div class="svg-container"><img src="data:image/svg+xml;base64,{encoded}" alt="desen"/></div>',
        unsafe_allow_html=True,
    )


def render_message(content):
    content = content.replace("[[DESEN_SVG]]", "").replace("[[/DESEN_SVG]]", "")
    position = 0
    for match in SVG_PATTERN.finditer(content):
        text_before = content[position:match.start()]
        if text_before.strip():
            st.markdown(text_before)
        render_svg(match.group(0))
        position = match.end()
    text_after = content[position:]
    if text_after.strip():
        st.markdown(text_after)


def text_for_speech(content, limit=1000):
    text = SVG_PATTERN.sub(" ", content)
    text = text.replace("[[DESEN_SVG]]", "").replace("[[/DESEN_SVG]]", "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^\s*\|?\s*:?-{3,}.*$", " ", text, flags=re.MULTILINE)  # separatori de tabel
    text = text.replace("|", ", ")
    text = re.sub(r"[*_#`>~]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        sentence_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[:sentence_end + 1] if sentence_end > limit // 2 else cut[:cut.rfind(" ")]
    return text


def make_audio(content):
    text = text_for_speech(content)
    if not text:
        return None
    try:
        sound_file = BytesIO()
        gTTS(text=text, lang="ro").write_to_fp(sound_file)
        return sound_file.getvalue()
    except Exception as e:
        _log("Eroare audio", e)
        return None


# ==========================================
# 6. SIDEBAR: CONT, CONVERSAȚII, SETĂRI, UPLOAD
# ==========================================


def render_account_ui():
    if not ACCOUNTS_ENABLED:
        st.caption("👤 Conturile nu sunt disponibile acum (baza de date nu e configurată).")
        return

    rec = st.session_state.get("_auth_new_recovery")
    if rec:
        st.warning("🔑 **Codul tău de recuperare** (îl vezi o singură dată). Cu el îți poți schimba "
                   "parola dacă o uiți. Notează-l sau descarcă-l.")
        st.code(rec, language=None)
        st.download_button("⬇️ Descarcă codul (.txt)", f"Cod de recuperare {APP_NAME}: {rec}\n",
                           file_name="gymbro-cod-recuperare.txt", key="_auth_rec_download")
        st.button("✅ Am notat codul", key="_auth_rec_dismiss", on_click=_dismiss_recovery)

    user = current_user()
    with st.expander(f"👤 {user['name']}" if user else "👤 Cont (opțional)", expanded=False):
        if user:
            st.caption(f"Conectat ca **{user['email']}**. Conversațiile tale apar pe orice dispozitiv.")
            c1, c2 = st.columns(2)
            if c1.button("🚪 Ieșire", key="_auth_logout"):
                auth_logout(False)
                st.rerun()
            if c2.button("Ieșire peste tot", key="_auth_logout_all"):
                auth_logout(True)
                st.rerun()
            if st.button("Generează cod nou", key="_auth_regen_recovery",
                         help="Un cod de recuperare nou; cel vechi nu mai merge."):
                if auth_regenerate_recovery():
                    st.rerun()
                st.error("Nu am putut genera codul acum.")
            return

        st.caption("Fără cont, conversația rămâne doar în acest browser. Cu un cont o vezi pe orice dispozitiv.")
        tab_login, tab_register, tab_reset = st.tabs(["Intră", "Cont nou", "Am uitat parola"])
        with tab_login:
            email = st.text_input("Email", key="_auth_login_email")
            pw = st.text_input("Parolă", type="password", key="_auth_login_pw")
            remember = st.checkbox("Ține-mă minte", value=True, key="_auth_login_remember")
            if st.button("Intră", key="_auth_login_btn"):
                u, err = auth_login(email, pw)
                if u:
                    auth_complete_login(u, remember)
                    st.rerun()
                st.error(err)
        with tab_register:
            name = st.text_input("Nume (opțional)", key="_auth_reg_name")
            email = st.text_input("Email", key="_auth_reg_email")
            pw = st.text_input("Parolă (min. 8 caractere)", type="password", key="_auth_reg_pw")
            pw2 = st.text_input("Repetă parola", type="password", key="_auth_reg_pw2")
            remember = st.checkbox("Ține-mă minte", value=True, key="_auth_reg_remember")
            if st.button("Creează cont", key="_auth_reg_btn"):
                if pw != pw2:
                    st.error("Parolele nu coincid.")
                else:
                    u, err = auth_register(email, pw, name)
                    if u:
                        auth_complete_login(u, remember)
                        st.rerun()
                    st.error(err)
        with tab_reset:
            st.caption("1️⃣ Cere un cod pe email **sau** folosește codul de recuperare primit la crearea contului.")
            mail = st.text_input("Email", key="_auth_reset_mail_email")
            if st.button("📧 Trimite codul", key="_auth_reset_send"):
                okay, info = auth_request_reset_email(mail)
                (st.success if okay else st.error)(info)
            st.caption("2️⃣ Schimbă parola")
            email = st.text_input("Email", key="_auth_reset_email")
            code = st.text_input("Cod din email sau cod de recuperare", key="_auth_reset_code")
            pw = st.text_input("Parolă nouă", type="password", key="_auth_reset_pw")
            pw2 = st.text_input("Repetă parola nouă", type="password", key="_auth_reset_pw2")
            if st.button("Schimbă parola", key="_auth_reset_btn"):
                if pw != pw2:
                    st.error("Parolele nu coincid.")
                else:
                    u, err = auth_reset_password(email, code, pw)
                    if u:
                        auth_complete_login(u, True)
                        st.rerun()
                    st.error(err)


def render_profile_ui():
    profile = st.session_state.get("profile") or {}
    title = "🏋️ Profilul meu" + ("" if profile.get("age_group") else " (completează vârsta)")
    with st.expander(title, expanded=False):
        st.caption("Antrenorul își adaptează sfaturile după vârstă, nivel și obiectiv. Toate câmpurile sunt opționale.")
        st.selectbox("Vârstă", list(AGE_GROUPS), format_func=AGE_GROUPS.get, index=None,
                     placeholder="Alege grupa de vârstă", key=PROFILE_WIDGET_KEYS["age_group"])
        for field, (label, options) in PROFILE_FIELDS.items():
            st.selectbox(label, options, index=None, placeholder="Alege", key=PROFILE_WIDGET_KEYS[field])
        st.text_area("Accidentări, dureri sau afecțiuni de care să țin cont", max_chars=300,
                     placeholder="ex. mă doare genunchiul stâng la genuflexiuni", key=PROFILE_WIDGET_KEYS["limits"])
        st.button("💾 Salvează profilul", key="_prof_save", on_click=save_profile_from_widgets)
        msg = st.session_state.pop("_profile_saved_msg", None)
        if msg:
            st.caption(msg)
        if not current_user():
            st.caption("Fără cont, profilul rămâne la această conversație. Cu un cont îl ai pe orice dispozitiv.")


def render_conversations_ui():
    uid = current_user_id()
    if not uid:
        return
    st.header("💬 Conversațiile mele")
    st.button("➕ Conversație nouă", key="_new_conv", width="stretch", on_click=new_session)
    for s in list_user_sessions(uid):
        sid = s["session_id"]
        title = (s.get("title") or "Conversație nouă").replace("\n", " ")
        current = sid == st.session_state.session_id
        st.button(("▶️ " if current else "") + title[:40], key=f"_conv_{sid}", width="stretch",
                  disabled=current, on_click=switch_session, args=(sid,))


st.title("💪 GymBro AI")
st.caption("Antrenorul tău personal virtual • pentru orice vârstă • 24/7 • Gratuit")

if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0


def _reset_chat():
    clear_history_db(st.session_state.session_id)
    st.session_state.messages = []
    st.session_state.pop("media", None)
    st.session_state.uploader_key += 1


ensure_profile_loaded()

with st.sidebar:
    render_account_ui()
    render_profile_ui()
    render_conversations_ui()

    st.header("⚙️ Setări")
    st.button("🗑️ Reset Chat", type="primary", width="stretch", key="_reset_chat", on_click=_reset_chat)
    enable_audio = st.toggle("🔊 Activează Vocea", value=False)

    st.divider()
    st.header("📁 Materiale")
    uploaded_file = st.file_uploader(
        "Încarcă Poză/PDF",
        type=["jpg", "jpeg", "png", "pdf"],
        key=f"uploader_{st.session_state.uploader_key}",
    )
    if uploaded_file is None:
        st.session_state.pop("media", None)
    else:
        signature = (uploaded_file.name, uploaded_file.size)
        media = st.session_state.get("media")
        if not media or media["signature"] != signature:
            data = uploaded_file.getvalue()
            if "image" in (uploaded_file.type or ""):
                try:
                    Image.open(BytesIO(data)).verify()
                    st.session_state.media = {"kind": "image", "signature": signature, "name": uploaded_file.name,
                                              "data": data, "mime": uploaded_file.type or "image/jpeg"}
                except Exception as e:
                    st.session_state.pop("media", None)
                    st.error(f"Imaginea nu poate fi citită: {e}")
            else:
                st.session_state.media = {"kind": "pdf", "signature": signature,
                                          "name": uploaded_file.name, "data": data, "files": {}}
        media = st.session_state.get("media")
        if media and media["kind"] == "image":
            st.image(media["data"], caption="Imagine atașată")
        elif media:
            st.success(f"📄 Atașat: {media['name']}")
        if media:
            st.caption("Materialul e trimis împreună cu fiecare mesaj până îl ștergi din listă.")

    st.divider()
    if current_user():
        st.caption("🔒 Conversațiile contului tău le poți deschide doar tu, după ce intri în cont.")
    else:
        st.caption("🔒 Conversația e salvată în acest browser și pe link-ul din bara de adrese. "
                   "Nu trimite link-ul altor persoane sau fă-ți un cont.")

sync_browser_storage()

if not keys:
    st.error("❌ Nu am găsit nicio cheie API validă. Adaugă 'GOOGLE_API_KEYS' în secrets sau introdu una manual.")
    st.stop()

# ==========================================
# 7. CHAT
# ==========================================

if "messages" not in st.session_state:
    st.session_state.messages = load_history_from_db(st.session_state.session_id)

for msg in st.session_state.messages:
    with st.chat_message(msg["role"], avatar="💪" if msg["role"] == "assistant" else "👤"):
        if msg["role"] == "assistant":
            render_message(msg["content"])
            if msg.get("audio"):
                st.audio(msg["audio"], format="audio/mp3")
        else:
            st.markdown(msg["content"])

if user_input := st.chat_input("Salut! Vreau un program pentru spate și biceps..."):
    user_input = user_input.strip()

if user_input:
    st.chat_message("user", avatar="👤").markdown(user_input)

    # Istoricul ÎNAINTE de mesajul curent, ultimele MAX_HISTORY_MESSAGES
    history, pending_text = build_history(st.session_state.messages[-MAX_HISTORY_MESSAGES:])
    text_to_send = f"{pending_text}\n\n{user_input}" if pending_text else user_input

    st.session_state.messages.append({"role": "user", "content": user_input})
    save_message_to_db(st.session_state.session_id, "user", user_input)

    with st.chat_message("assistant", avatar="💪"):
        message_placeholder = st.empty()
        message_placeholder.markdown("💭 Mă gândesc...")
        full_response = ""
        try:
            last_update = 0.0
            for text_chunk in run_chat_with_rotation(history, text_to_send):
                full_response += text_chunk
                now = time.time()
                if now - last_update > 0.1:
                    message_placeholder.markdown(full_response + "▌")
                    last_update = now

            message_placeholder.empty()
            render_message(full_response)

            assistant_msg = {"role": "assistant", "content": full_response}
            if enable_audio:
                audio_bytes = make_audio(full_response)
                if audio_bytes:
                    st.audio(audio_bytes, format="audio/mp3")
                    assistant_msg["audio"] = audio_bytes

            st.session_state.messages.append(assistant_msg)
            save_message_to_db(st.session_state.session_id, "assistant", full_response)

        except EmptyResponseError:
            message_placeholder.empty()
            st.warning("🤔 Nu am putut formula un răspuns la asta. Încearcă să reformulezi întrebarea.")
        except FatalRequestError as e:
            message_placeholder.empty()
            st.error(f"Eroare: {e}")
