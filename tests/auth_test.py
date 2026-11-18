from common import *
S1, S2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"

# A. dispozitiv 1, anonim, un mesaj
a = app({"sid": S1, "token": ""})
ok(a.session_state["session_id"] == S1, "dispozitivul 1 restaurează conversația din browser")
say(a, "Vreau un program pentru picioare")
ok(any(h["session_id"] == S1 and h["role"] == "assistant" for h in DB["gym_history"]), "mesajul + răspunsul salvate în gym_history")
ok(next(s for s in DB["gym_sessions"] if s["session_id"] == S1)["title"].startswith("Vreau un program"), "titlul conversației = primul mesaj")
fill(a, _auth_reg_email="Elev@Test.ro", _auth_reg_pw="parola123", _auth_reg_pw2="parola123")
click(a, "_auth_reg_btn")
u = user_of(a); uid = u["id"]
ok(u["email"] == "elev@test.ro", "cont creat, email normalizat")
ok(DB["gym_users"][0]["password_hash"].startswith("scrypt$") and "parola123" not in DB["gym_users"][0]["password_hash"], "parola stocată ca hash scrypt")
ok(next(s for s in DB["gym_sessions"] if s["session_id"] == S1)["user_id"] == uid, "conversația anonimă a trecut în cont")
tok = token_from(a)
ok(tok and len(DB["gym_tokens"]) == 1 and DB["gym_tokens"][0]["token_hash"] != tok, "token creat; în DB doar hash-ul")

# B. dispozitiv 2: login automat din token, vede conversația
b = app({"sid": S2, "token": tok})
ok((user_of(b) or {}).get("id") == uid, "dispozitivul 2 logat automat din token")
labels = " ".join(str(x.label) for x in b.sidebar.button)
ok("Vreau un program" in labels, "dispozitivul 2 vede conversația de pe dispozitivul 1 în listă")
ok(next(s for s in DB["gym_sessions"] if s["session_id"] == S2)["user_id"] == uid, "conversația nouă de pe dispozitivul 2 e a contului")
click(b, f"_conv_{S1}")
ok(b.session_state["session_id"] == S1 and any("picioare" in m["content"] for m in b.session_state["messages"]), "click în listă deschide conversația")
ok(b.query_params["session_id"] == [S1] or b.query_params["session_id"] == S1, "URL-ul se actualizează la schimbarea conversației")
click(b, "_new_conv")
ok(b.session_state["session_id"] not in (S1, S2) and not b.session_state["messages"], "„Conversație nouă” pornește una goală")

# C. anonim cu ID-ul conversației contului (link copiat) → NU o primește
c = app({"sid": S1, "token": ""})
ok(c.session_state["session_id"] != S1, "anonim cu SID-ul unui cont din localStorage primește conversație nouă")
c2 = app({}, sid_url=S1)
ok(c2.session_state["session_id"] != S1 and not any("picioare" in m["content"] for m in c2.session_state["messages"]), "anonim cu ?session_id= al unui cont nu vede istoricul")

# D. parolă greșită / corectă
fill(c, _auth_login_email="elev@test.ro", _auth_login_pw="gresit000"); click(c, "_auth_login_btn")
ok(user_of(c) is None and any("greșită" in e.value for e in c.error), "parolă greșită refuzată")
fill(c, _auth_login_pw="parola123"); click(c, "_auth_login_btn")
ok((user_of(c) or {}).get("id") == uid and len(DB["gym_tokens"]) == 2, "login corect, token propriu")
ok("Vreau un program" in " ".join(str(x.label) for x in c.sidebar.button), "după login vede conversațiile contului")

# E. ieșire pe dispozitivul 2
old = b.session_state["session_id"]
click(b, "_auth_logout")
ok(user_of(b) is None and len(DB["gym_tokens"]) == 1, "ieșire: delogat, tokenul lui șters")
ok("removeItem" in b.session_state["_auth_ls_js"], "tokenul se șterge și din browser")
ok(b.session_state["session_id"] != old, "după ieșire pornește o conversație anonimă nouă")

# F. token revocat
f = app({"token": tok})
ok(user_of(f) is None, "tokenul revocat nu mai loghează")
ok("removeItem" in (f.session_state["_auth_ls_js"] or ""), "tokenul revocat e scos din browser")

# G. brute force
g = app({})
for _ in range(6):
    fill(g, _auth_login_email="elev@test.ro", _auth_login_pw="x" * 9); click(g, "_auth_login_btn")
ok(any("Prea multe" in e.value for e in g.error), "blocare după 5 încercări greșite")

# H. ieșire peste tot
click(c, "_auth_logout_all")
ok(len(DB["gym_tokens"]) == 0, "„Ieșire peste tot” șterge toate tokenurile")

# I. fără „Ține-mă minte”
h = app({})   # (contul elev@ e blocat 10 min după pasul G → folosim alt cont)
fill(h, _auth_reg_email="fara.token@test.ro", _auth_reg_pw="parola123", _auth_reg_pw2="parola123")
h.checkbox(key="_auth_reg_remember").uncheck().run()
click(h, "_auth_reg_btn")
ok(user_of(h) and len(DB["gym_tokens"]) == 0 and "removeItem" in h.session_state["_auth_ls_js"], "fără „Ține-mă minte”: niciun token")

# J. cont duplicat / parolă scurtă / parole diferite
j = app({})
fill(j, _auth_reg_email="elev@test.ro", _auth_reg_pw="parola123", _auth_reg_pw2="parola123"); click(j, "_auth_reg_btn")
ok(any("Există deja" in e.value for e in j.error), "email deja folosit refuzat")
fill(j, _auth_reg_email="alt@test.ro", _auth_reg_pw="scurta", _auth_reg_pw2="scurta"); click(j, "_auth_reg_btn")
ok(any("cel puțin 8" in e.value for e in j.error), "parolă scurtă refuzată")
fill(j, _auth_reg_pw="parola123", _auth_reg_pw2="parola124"); click(j, "_auth_reg_btn")
ok(any("nu coincid" in e.value for e in j.error), "parole diferite refuzate")
