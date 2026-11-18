# Fără Supabase în secrets: istoric în SQLite, conturile nu apar
from common import *
from fake_gemini import CALLS
a = app({})
sid = a.session_state["session_id"]
ok(any("Conturile nu sunt disponibile" in c.value for c in a.sidebar.caption), "fără Supabase: secțiunea Cont explică de ce lipsește")
say(a, "Salut din SQLite")
ok(len(CALLS) == 1, "chatul merge fără Supabase")
b = app({}, sid_url=sid)
ok(any(m["content"] == "Salut din SQLite" for m in b.session_state["messages"]), "istoricul se reîncarcă din SQLite")
ok(not DB["gym_sessions"], "nimic scris în Supabase")
