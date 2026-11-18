from common import *
from fake_gemini import CALLS

def prompt_last():
    return CALLS[-1][4].system_instruction

def set_profile(at, **kv):
    keys = {"age_group": "_prof_age", "experience": "_prof_experience", "goal": "_prof_goal",
            "days": "_prof_days", "equipment": "_prof_equipment"}
    for field, value in kv.items():
        if field == "limits":
            at.text_area(key="_prof_limits").input(value)
        else:
            at.selectbox(key=keys[field]).set_value(value)
    at.run()
    return click(at, "_prof_save")

a = app({})
sid = a.session_state["session_id"]
say(a, "Vreau să încep sala")
p = prompt_last()
ok("TOATE vârstele" in p and "VÂRSTA UTILIZATORULUI NU E CUNOSCUTĂ" in p, "fără profil: promptul general + întreabă vârsta")
ok("fără suplimente" in p and "PROFILUL UTILIZATORULUI" not in p, "fără profil: reguli prudente, niciun profil inventat")
ok(not any("adolescenți" in c.value for c in a.caption), "textul din pagină nu mai spune „pentru adolescenți”")

set_profile(a, age_group="18-39", experience="Intermediar", goal="Masă musculară", days="4",
            equipment="Sală complet echipată", limits="mă doare umărul drept la presa deasupra capului")
ok(any("Profil salvat" in c.value for c in a.sidebar.caption), "profil salvat (mesaj de confirmare)")
row = next(s for s in DB["gym_sessions"] if s["session_id"] == sid)
ok((row.get("profile") or {}).get("age_group") == "18-39", "fără cont: profilul e salvat pe conversație")
say(a, "Fă-mi un program")
p = prompt_last()
ok("18–39 ani" in p and "creatina monohidrat" in p and "116 111" not in p, "18–39: reguli de adult, informații despre creatină doar la cerere")
ok("Intermediar" in p and "Masă musculară" in p and "4" in p and "umărul drept" in p, "nivelul, obiectivul, zilele și limitările ajung la model")

set_profile(a, age_group="14-17")
say(a, "Ce să mănânc?")
p = prompt_last()
ok("14–17 ani" in p and "NICIODATĂ suplimente" in p and "116 111" in p, "14–17: fără suplimente + Telefonul Copilului")
set_profile(a, age_group="u14")
say(a, "Pot ridica greutăți?")
ok("greutatea corpului" in prompt_last() and "sub 14 ani" in prompt_last(), "sub 14: accent pe greutatea corpului, supraveghere")
set_profile(a, age_group="65+")
say(a, "Vreau să fiu mai în formă")
p = prompt_last()
ok("echilibru" in p and "prevenirea căderilor" in p and "acordul medicului" in p, "65+: echilibru, căderi, acordul medicului")
set_profile(a, age_group="40-64")
say(a, "Program pentru mine")
ok("control medical" in prompt_last(), "40–64: control medical dacă a fost sedentar / are afecțiuni")
ok("steroizi anabolizanți" in prompt_last(), "steroizii sunt interziși la orice vârstă")

# conversația redeschisă își păstrează profilul
b = app({}, sid_url=sid)
ok(b.selectbox(key="_prof_age").value == "40-64" and "umărul drept" in b.text_area(key="_prof_limits").value,
   "conversația redeschisă își reîncarcă profilul în formular")

# intrare în cont: profilul anonim trece în cont și apare pe alt dispozitiv
fill(b, _auth_reg_email="sportiv@test.ro", _auth_reg_pw="parola123", _auth_reg_pw2="parola123")
click(b, "_auth_reg_btn")
u = DB["gym_users"][0]
ok((u.get("profile") or {}).get("age_group") == "40-64", "la crearea contului, profilul completat trece în cont")
tok = token_from(b)
c = app({"token": tok})
ok(c.selectbox(key="_prof_age").value == "40-64" and c.selectbox(key="_prof_goal").value == "Masă musculară",
   "alt dispozitiv: profilul contului e încărcat")
say(c, "Salut")
ok("40–64 ani" in prompt_last(), "alt dispozitiv: modelul primește profilul contului")
set_profile(c, age_group="65+")
ok(DB["gym_users"][0]["profile"]["age_group"] == "65+", "cu cont: profilul se salvează pe cont")

# ieșire: profilul contului nu rămâne pe dispozitiv
click(c, "_auth_logout")
ok(c.selectbox(key="_prof_age").value is None, "după ieșire, formularul de profil e gol")
say(c, "Salut din nou")
ok("NU E CUNOSCUTĂ" in prompt_last(), "după ieșire, modelul nu mai primește profilul contului")

# valoare veche care nu mai e în listă → gol, fără eroare
DB["gym_users"][0]["profile"]["goal"] = "Obiectiv vechi redenumit"
d = app({})   # tokenul `tok` a fost șters la „Ieșire” → intrăm din nou
fill(d, _auth_login_email="sportiv@test.ro", _auth_login_pw="parola123"); click(d, "_auth_login_btn")
ok(d.selectbox(key="_prof_goal").value is None and d.selectbox(key="_prof_age").value == "65+",
   "opțiune veche/redenumită în profil: câmp gol, fără eroare")
