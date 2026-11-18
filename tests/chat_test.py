from common import *
from fake_gemini import CALLS, RULES, REPLY, api_error, _Files
at = app({})
sid = at.session_state["session_id"]

REPLY["text"] = "Uite planul <svg viewBox='0 0 10 10'><rect onload='alert(1)' width='5' height='5'/></svg> gata"
say(at, "Arată-mi un desen")
md = " ".join(m.value for m in at.markdown)
ok("data:image/svg+xml;base64" in md and "onload" not in md, "SVG randat ca imagine, fără handler-e în pagină")
REPLY["text"] = None

RULES[("*", "gemini-3.1-flash-lite")] = "empty"
say(at, "Întrebare blocată")
ok(any("reformulezi" in w.value for w in at.warning), "răspuns gol → mesaj prietenos")
ok(not any(h["role"] == "assistant" and not h["content"].strip() for h in DB["gym_history"]), "răspunsul gol NU se salvează")
RULES.clear()
say(at, "Altă întrebare")
roles = [c.role for c in CALLS[-1][3]]
ok(roles[-1] == "user" and all(a != b for a, b in zip(roles, roles[1:])), f"istoric cu roluri alternante după eșec ({roles})")
ok("Întrebare blocată" in CALLS[-1][3][-1].parts[-1].text and "Altă întrebare" in CALLS[-1][3][-1].parts[-1].text,
   "mesajul fără răspuns e lipit de cel nou")

RULES[("*", "gemini-3.1-flash-lite")] = api_error(400, "Invalid argument", "INVALID_ARGUMENT")
n = len(CALLS); say(at, "Cerere stricată")
ok(len(CALLS) == n + 1 and any("400" in e.value for e in at.error), "400 (cerere greșită) nu rotește cheile/modelele")
RULES.clear()

for i in range(10):
    say(at, f"mesaj {i}")
contents = CALLS[-1][3]
texts = [p.text for c in contents for p in c.parts if p.text]
ok(contents[0].role == "user" and texts.count("mesaj 9") == 1, "după 12+ mesaje: istoricul începe cu user și mesajul curent apare o singură dată")
ok(len(contents) <= 13, f"istoricul e limitat ({len(contents)} conținuturi)")


# reset chat
click(at, "_reset_chat")
ok(not at.session_state["messages"] and not any(h["session_id"] == sid for h in DB["gym_history"]), "Reset Chat golește conversația și din DB")
