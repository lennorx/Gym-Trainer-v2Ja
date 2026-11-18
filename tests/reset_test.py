import smtplib
SENT = []
class FakeSMTP:
    def __init__(s, h, p, timeout=None): pass
    def __enter__(s): return s
    def __exit__(s, *a): pass
    def login(s, u, p): assert p == "abcdefghijklmnop", "spațiile din parola de aplicație trebuie eliminate"
    def send_message(s, m): SENT.append(m)
smtplib.SMTP_SSL = FakeSMTP
from common import *

a = app({})
fill(a, _auth_reg_email="elev@test.ro", _auth_reg_pw="parola123", _auth_reg_pw2="parola123"); click(a, "_auth_reg_btn")
rec = a.session_state["_auth_new_recovery"]
ok(re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", rec) is not None, "cod de recuperare generat la creare")
ok(any(rec in c.value for c in a.sidebar.code), "codul e afișat în bară")
click(a, "_auth_rec_dismiss")
ok("_auth_new_recovery" not in a.session_state and not a.sidebar.code, "codul dispare după „Am notat”")
tok = token_from(a)

b = app({})
fill(b, _auth_reset_mail_email="mailto:Elev@test.ro"); click(b, "_auth_reset_send")
ok(len(SENT) == 1 and SENT[0]["To"] == "elev@test.ro", "email trimis (mailto: și majusculele curățate)")
ok("GymBro" in SENT[0]["From"] and "GymBro" in SENT[0]["Subject"], "emailul e de la GymBro AI")
code = re.search(r"\b(\d{6})\b", SENT[0].get_content()).group(1)
ok(DB["gym_password_resets"][0]["code_hash"] != code, "în DB doar hash-ul codului")
click(b, "_auth_reset_send")
ok(len(SENT) == 1 and any("deja" in e.value for e in b.error), "al doilea email în <60 s refuzat")
b2 = app({})
fill(b2, _auth_reset_mail_email="nimeni@test.ro"); click(b2, "_auth_reset_send")
ok(len(SENT) == 1 and any("Dacă există" in s.value for s in b2.success), "email inexistent: același mesaj, niciun email")
fill(b, _auth_reset_email="elev@test.ro", _auth_reset_code="000000" if code != "000000" else "111111",
     _auth_reset_pw="parolanoua1", _auth_reset_pw2="parolanoua1")
click(b, "_auth_reset_btn")
ok(any("greșit" in e.value for e in b.error), "cod greșit refuzat")
fill(b, _auth_reset_code=code); click(b, "_auth_reset_btn")
ok((user_of(b) or {}).get("email") == "elev@test.ro", "parolă schimbată + logat")
ok(not DB["gym_password_resets"], "codul din email consumat")
ok(len(DB["gym_tokens"]) == 1, "celelalte dispozitive deconectate (doar tokenul nou)")
ok(user_of(app({"token": tok})) is None, "tokenul vechi nu mai merge")

g = app({})
fill(g, _auth_login_email="elev@test.ro", _auth_login_pw="parola123"); click(g, "_auth_login_btn")
ok(user_of(g) is None, "parola veche nu mai merge")
fill(g, _auth_login_pw="parolanoua1"); click(g, "_auth_login_btn")
ok((user_of(g) or {}).get("email") == "elev@test.ro", "parola nouă merge")

h = app({})
fill(h, _auth_reset_email="ELEV@test.ro", _auth_reset_code=rec.replace("-", "").lower(),
     _auth_reset_pw="altaparola9", _auth_reset_pw2="altaparola9")
click(h, "_auth_reset_btn")
ok((user_of(h) or {}).get("email") == "elev@test.ro", "resetare cu codul de recuperare (litere mici, fără liniuțe)")
rec2 = h.session_state["_auth_new_recovery"] if "_auth_new_recovery" in h.session_state else None
ok(rec2 and rec2 != rec, "cod de recuperare nou (cel vechi e consumat)")
i = app({})
fill(i, _auth_reset_email="elev@test.ro", _auth_reset_code=rec, _auth_reset_pw="inca1parola", _auth_reset_pw2="inca1parola")
click(i, "_auth_reset_btn")
ok(any("greșit" in e.value for e in i.error), "codul de recuperare vechi nu mai merge")
click(h, "_auth_rec_dismiss"); click(h, "_auth_regen_recovery")
ok(h.session_state["_auth_new_recovery"] not in (None, rec2), "„Generează cod nou” din cont")
