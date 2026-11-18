import ast
import base64
import re
import sqlite3
import time
import uuid
from contextlib import closing
from io import BytesIO

import streamlit as st
from google import genai
from google.genai import errors, types
from gtts import gTTS
from PIL import Image

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
# Modelele încercate în ordine (primul care merge rămâne activ).
# Se pot suprascrie din secrets cu GEMINI_MODELS = ["...", "..."]
DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash"]
# Fișierele urcate la Gemini expiră după 48h; le reîncărcăm înainte de asta
FILE_MAX_AGE_SECONDS = 47 * 3600
SVG_PATTERN = re.compile(r"<svg\b.*?</svg>", re.IGNORECASE | re.DOTALL)

# ==========================================
# 2. SISTEM DE MEMORIE (Bază de date)
# ==========================================
DB_PATH = "chat_history.db"


def get_db_connection():
    return sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)


def init_db():
    with closing(get_db_connection()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS history
                        (session_id TEXT, role TEXT, content TEXT, timestamp REAL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_session ON history (session_id, timestamp)")


def save_message_to_db(session_id, role, content):
    try:
        with closing(get_db_connection()) as conn, conn:
            conn.execute("INSERT INTO history VALUES (?, ?, ?, ?)", (session_id, role, content, time.time()))
    except sqlite3.Error as e:
        print(f"Eroare DB (save): {e}")


def load_history_from_db(session_id):
    try:
        with closing(get_db_connection()) as conn:
            rows = conn.execute(
                "SELECT role, content FROM history WHERE session_id=? ORDER BY timestamp ASC",
                (session_id,),
            ).fetchall()
        return [{"role": role, "content": content} for role, content in rows if content and content.strip()]
    except sqlite3.Error as e:
        print(f"Eroare DB (load): {e}")
        return []


def clear_history_db(session_id):
    try:
        with closing(get_db_connection()) as conn, conn:
            conn.execute("DELETE FROM history WHERE session_id=?", (session_id,))
    except sqlite3.Error as e:
        print(f"Eroare DB (clear): {e}")


init_db()


def is_valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


session_param = st.query_params.get("session_id")
if session_param and is_valid_uuid(session_param):
    st.session_state.session_id = session_param
else:
    new_id = str(uuid.uuid4())
    st.query_params["session_id"] = new_id
    st.session_state.session_id = new_id

# ==========================================
# 3. CHEI API, MODELE & ROTIRE
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

models = parse_secret_list(st.secrets.get("GEMINI_MODELS")) if "GEMINI_MODELS" in st.secrets else []
if not models:
    models = list(DEFAULT_MODELS)

# Dacă nu găsim chei în secrets, cerem manual
if not keys:
    with st.sidebar:
        manual_key = st.text_input("API Key (Manual):", type="password")
        if manual_key and manual_key.strip():
            keys.append(manual_key.strip())

if not keys:
    st.error("❌ Nu am găsit nicio cheie API validă. Adaugă 'GOOGLE_API_KEYS' în secrets sau introdu una manual.")
    st.stop()

if "key_index" not in st.session_state or st.session_state.key_index >= len(keys):
    st.session_state.key_index = 0
if "model_index" not in st.session_state or st.session_state.model_index >= len(models):
    st.session_state.model_index = 0


@st.cache_resource(show_spinner=False)
def get_client(api_key):
    # Un client separat pe cheie: nu mai folosim configurarea globală,
    # deci utilizatorii simultani nu-și mai suprascriu cheia unul altuia.
    return genai.Client(api_key=api_key)


# --- PROMPT-UL SISTEMULUI (Optimizat pentru adolescenți) ---
SYSTEM_PROMPT = """
Ești un Antrenor Personal Virtual și Nutriționist numit "GymBro AI", specializat în lucrul cu adolescenții.

IDENTITATE:
1. Folosește EXCLUSIV genul masculin când vorbești despre tine.
   - Corect: "Sunt sigur", "Sunt pregătit", "Am fost atent", "Sunt bucuros".
   - GREȘIT: "Sunt sigură", "Sunt pregătită".
2. Te prezinți ca "Antrenorul tău personal virtual".

TON ȘI ADRESARE:
3. Vorbește DIRECT, la persoana I singular ("Eu cred", "Te ajut", "Sunt pregătit").
   - GREȘIT: "Domnul Antrenor este aici." / "Antrenorul te va ajuta."
4. Adresează-te utilizatorului direct, la persoana a II-a ("Tu trebuie să faci").
5. Prietenos, motivațional, clar, "cool" dar responsabil. Folosește emoji-uri 💪🥗🔥 cu măsură.
6. Fii cald, natural și scurt. Evită introducerile pompoase.
7. NU SALUTA în fiecare mesaj. Salută DOAR la începutul unei conversații noi.

REGULI DE AUR:
1. Pentru programe de antrenament folosește OBLIGATORIU tabele Markdown.
2. (CRITIC) Nu recomanda NICIODATĂ suplimente: creatină, steroizi, proteine pudră, pre-workout,
   "fat burners", băuturi energizante sau orice altă pastilă/pulbere. Doar mâncare reală.
3. Dacă utilizatorul e începător, insistă pe forma corectă, nu pe greutăți mari.
4. Nutriție: mâncare reală, naturală și variată, cu exemple concrete de mese și gustări.

SIGURANȚĂ (CRITIC, utilizatorii sunt adolescenți):
1. Progresia este treptată. Pentru cei mai mici de 16 ani recomandă în primul rând exerciții cu greutatea
   corpului, iar la greutăți libere spune-le să lucreze sub supravegherea unui antrenor sau a unui adult.
2. Fără diete restrictive, fără deficit caloric mare, fără post, fără numărat obsesiv de calorii
   sau cântărit zilnic. Nu da ținte de greutate corporală.
3. Dacă utilizatorul pomenește durere, o accidentare, amețeli, leșin, probleme cu inima sau o boală,
   spune-i să oprească efortul și să meargă la medic sau kinetoterapeut și să anunțe un părinte.
   Nu pune diagnostice.
4. Dacă observi semne de relație nesănătoasă cu mâncarea sau cu propriul corp (vrea să slăbească extrem
   de repede, sare peste mese, vomită după masă, face sport ca să "ardă" ce a mâncat, se urăște pentru
   cum arată), răspunde cu căldură, fără planuri de slăbit, încurajează-l să vorbească cu un părinte,
   un medic sau consilierul școlar și menționează Telefonul Copilului: 116 111 (gratuit).

OBIECTIVELE TALE:
1. Să creezi planuri de antrenament organizate pe ZILE și SĂPTĂMÂNI.
2. Să explici corect execuția exercițiilor pentru a evita accidentările.
3. Să oferi sfaturi nutriționale sănătoase (fără diete extreme, focus pe proteine și energie din mâncare).
4. Să răspunzi la întrebări despre sală sau exerciții acasă.
5. Să fii realist și să nu fii ca influencerii de fitness.
"""

# Filtre de siguranță: aplicația e pentru adolescenți, deci NU le dezactivăm
SAFETY_SETTINGS = [
    types.SafetySetting(category=category, threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE)
    for category in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]

GENERATION_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    safety_settings=SAFETY_SETTINGS,
)


class EmptyResponseError(Exception):
    """Modelul n-a întors niciun text (de obicei răspuns blocat de filtre)."""


class FatalRequestError(Exception):
    """Eroare care nu se rezolvă schimbând cheia (cerere greșită, stream întrerupt)."""


def get_media_part(client, key_idx):
    """Întoarce materialul atașat în forma potrivită pentru cheia dată (sau None)."""
    media = st.session_state.get("media")
    if not media:
        return None
    if media["kind"] == "image":
        return Image.open(BytesIO(media["data"]))

    # PDF: fișierele urcate aparțin proiectului cheii, deci îl urcăm o dată pe fiecare cheie folosită
    cached = media["files"].get(key_idx)
    if cached and time.time() - cached["uploaded_at"] < FILE_MAX_AGE_SECONDS:
        return cached["file"]

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
    return uploaded


def run_chat_with_rotation(history, user_text):
    """Generează răspunsul în stream, rotind cheile (și modelele, dacă unul nu mai există)."""
    start_key = st.session_state.key_index
    start_model = st.session_state.model_index
    last_error = None

    for m_offset in range(len(models)):
        model_idx = (start_model + m_offset) % len(models)
        model_name = models[model_idx]

        for k_offset in range(len(keys)):
            key_idx = (start_key + k_offset) % len(keys)
            client = get_client(keys[key_idx])
            yielded_any = False
            try:
                payload = []
                media_part = get_media_part(client, key_idx)
                if media_part is not None:
                    payload.append(media_part)
                payload.append(user_text)

                chat = client.chats.create(model=model_name, config=GENERATION_CONFIG, history=history)
                for chunk in chat.send_message_stream(payload):
                    text = chunk.text
                    if text:
                        yielded_any = True
                        yield text

                # Reținem combinația care a mers, ca să o folosim direct data viitoare
                st.session_state.key_index = key_idx
                st.session_state.model_index = model_idx
                if not yielded_any:
                    raise EmptyResponseError()
                return

            except (EmptyResponseError, FatalRequestError):
                raise
            except errors.APIError as e:
                last_error = e
                if yielded_any:
                    # Nu reîncercăm: textul deja afișat s-ar dubla
                    raise FatalRequestError("Răspunsul s-a întrerupt. Te rog trimite din nou mesajul.") from e
                if e.code == 404:
                    # Modelul nu (mai) există -> trecem la următorul model
                    st.toast(f"⚠️ Modelul {model_name} nu e disponibil, încerc altul...", icon="🔄")
                    break
                if e.code in (401, 403, 429):
                    st.toast(f"⚠️ Schimb cheia {key_idx + 1}...", icon="🔄")
                    continue
                if e.code is not None and e.code >= 500:
                    st.toast("🐢 Serverul e ocupat, reîncerc...", icon="⏳")
                    time.sleep(2)
                    continue
                # 400 și alte erori de cerere: altă cheie nu ajută
                raise FatalRequestError(f"Cerere respinsă de Gemini ({e.code}): {e.message}") from e
            except Exception as e:
                # Erori de rețea etc.
                last_error = e
                if yielded_any:
                    raise FatalRequestError("Răspunsul s-a întrerupt. Te rog trimite din nou mesajul.") from e
                time.sleep(1)
                continue

    raise FatalRequestError(
        "Serviciul este indisponibil momentan. Toate cheile și modelele au fost încercate."
        + (f" Ultima eroare: {last_error}" if last_error else "")
    )


def build_history(messages):
    """Transformă mesajele în istoric valid pentru Gemini: fără mesaje goale,
    roluri alternante, începe cu 'user'. Întoarce (istoric, text_user_rămas_fără_răspuns)."""
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

    # Dacă ultimul mesaj din istoric e al userului (răspunsul anterior a eșuat),
    # îl lipim de mesajul curent ca să nu trimitem două mesaje user la rând.
    pending = ""
    if contents and contents[-1].role == "user":
        pending = "\n\n".join(p.text for p in contents.pop().parts if p.text)
    return contents, pending


# ==========================================
# 4. RANDARE MESAJE & AUDIO
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
    text = re.sub(r"<[^>]+>", " ", text)                       # alte tag-uri HTML
    text = re.sub(r"^\s*\|?\s*:?-{3,}.*$", " ", text, flags=re.MULTILINE)  # separatori de tabel
    text = text.replace("|", ", ")
    text = re.sub(r"[*_#`>~]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        sentence_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        cut = cut[:sentence_end + 1] if sentence_end > limit // 2 else cut[:cut.rfind(" ")]
        text = cut
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
        print(f"Eroare audio: {e}")
        return None


# ==========================================
# 5. SIDEBAR & UPLOAD
# ==========================================
st.title("💪 GymBro AI")
st.caption("Antrenorul tău personal virtual • 24/7 • Gratuit")

if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0

with st.sidebar:
    st.header("⚙️ Setări")
    if st.button("🗑️ Reset Chat", type="primary", width="stretch"):
        clear_history_db(st.session_state.session_id)
        st.session_state.messages = []
        st.session_state.pop("media", None)
        st.session_state.uploader_key += 1
        st.rerun()

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
            if "image" in uploaded_file.type:
                try:
                    Image.open(BytesIO(data)).verify()
                    st.session_state.media = {"kind": "image", "signature": signature,
                                              "name": uploaded_file.name, "data": data}
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
    st.caption("🔒 Conversația ta e salvată pe link-ul din bara de adrese. Nu trimite link-ul altor persoane.")

# ==========================================
# 6. CHAT
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
