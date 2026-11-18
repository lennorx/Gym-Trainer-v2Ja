# GymBro AI

Antrenor personal virtual pentru adolescenți (Streamlit + Gemini).

## Rulare locală

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Secrets (`.streamlit/secrets.toml` sau Streamlit Cloud → Settings → Secrets)

```toml
# una sau mai multe chei Gemini; se rotesc automat la 429/401/403
GOOGLE_API_KEYS = ["cheia-1", "cheia-2"]

# opțional: modelele încercate în ordine (dacă unul dă 404, se trece la următorul)
GEMINI_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash"]
```

## Notă despre istoric

Conversațiile se salvează în `chat_history.db` (SQLite). Pe Streamlit Community Cloud
fișierul se pierde la fiecare restart/redeploy; pentru istoric permanent e nevoie de o
bază de date externă (ex. Supabase).
