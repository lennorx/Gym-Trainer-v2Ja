# GymBro AI

Antrenor personal virtual pentru orice vârstă (Streamlit + Gemini + Supabase).

Sfaturile se adaptează după profil („🏋️ Profilul meu”): grupa de vârstă (sub 14, 14–17, 18–39, 40–64, 65+),
nivel, obiectiv, zile pe săptămână, echipament și limitări. Fără vârstă completată, antrenorul o cere și
aplică regulile prudente (fără suplimente, fără deficit caloric). Profilul stă pe cont (`gym_users.profile`)
sau, fără cont, pe conversație (`gym_sessions.profile`).

## Rulare locală

```bash
pip install -r requirements.txt
streamlit run app.py
```

Fără `SUPABASE_URL` / `SUPABASE_KEY` aplicația merge în continuare, dar istoricul stă în
`chat_history.db` (SQLite, se pierde la restart pe Streamlit Cloud) și conturile nu apar.

## Secrets (`.streamlit/secrets.toml` sau Streamlit Cloud → Settings → Secrets)

```toml
# chei Gemini; rotația chei × modele trece automat peste cele epuizate / invalide
GOOGLE_API_KEYS = ["cheia-1", "cheia-2"]

# Supabase (proiectul „Profesor Liceu”), cheia SECRETĂ sb_secret_... — conversații + conturi
SUPABASE_URL = "https://<proiect>.supabase.co"
SUPABASE_KEY = "sb_secret_..."

# Resetarea parolei prin email (parolă de aplicație Google, cu 2FA activat pe cont)
SMTP_USER = "adresa@gmail.com"
SMTP_PASSWORD = "xxxx xxxx xxxx xxxx"
# SMTP_FROM_NAME = "GymBro AI"   # opțional

# opțional: lanțul de modele (implicit același ca în Profesor ETTI)
# GEMINI_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.6-flash", ...]
```

## Cont (opțional)
- Email + parolă (scrypt), „Ține-mă minte” 180 de zile (token în localStorage, în DB doar hash-ul).
- Conversațiile contului apar pe orice dispozitiv; la intrarea în cont, conversația anonimă curentă trece în cont.
  O conversație a unui cont nu se deschide din link de altcineva.
- Resetare parolă: cod de 6 cifre pe email (15 min, 5 încercări) sau codul de recuperare primit la creare.
- Blocare 10 minute după 5 parole greșite; „Ieșire peste tot”.
- Tabele: `gym_users`, `gym_tokens`, `gym_password_resets`, `gym_sessions`, `gym_history` (vezi `supabase_gym_auth.sql`).

## Teste

```bash
python3 tests/run_tests.py        # Supabase, Gemini și SMTP simulate; fără rețea
```
