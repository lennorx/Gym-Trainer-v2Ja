from common import *
from fake_gemini import CALLS, RULES, api_error
K1, K2 = "AIzaFAKE1111111111111111", "AIzaFAKE2222222222222222"
DAY = api_error(429, "Quota exceeded for metric: generate_content_free_tier_requests, limit: 500, quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier", "RESOURCE_EXHAUSTED")
MIN = api_error(429, "quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier. Please retry in 36.2s", "RESOURCE_EXHAUSTED")
NOT_FOUND = api_error(404, "models/gemini-3.5-flash-lite is not found for API version v1beta", "NOT_FOUND")
at = app({})
def ask(q):
    n = len(CALLS); say(at, q); return [(c[0], c[1]) for c in CALLS[n:]]

c = ask("Salut")
ok(c == [("1", "gemini-3.1-flash-lite")], f"primul model din lanț: gemini-3.1-flash-lite ({c})")
RULES[(K1, "gemini-3.1-flash-lite")] = DAY; RULES[(K1, "gemini-3.5-flash-lite")] = DAY
c = ask("Program pentru piept")
ok(c[-1] == ("1", "gemini-3.6-flash") and len(c) == 3, f"cotă zilnică pe 2 modele → 3.6 Flash, aceeași cheie ({c})")
c = ask("Și pentru spate?")
ok(c == [("1", "gemini-3.6-flash")], f"al doilea mesaj sare direct peste combinațiile epuizate ({c})")
for m in ["gemini-3.6-flash", "gemini-3-flash-preview", "gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite"]:
    RULES[(K1, m)] = DAY
c = ask("Ce mănânc după sală?")
ok(c[-1] == ("2", "gemini-3.1-flash-lite") and at.session_state["key_index"] == 1, f"cheia 1 epuizată → cheia 2 ({c})")
c = ask("Câte serii la genuflexiuni?")
ok(c == [("2", "gemini-3.1-flash-lite")], "rămâne pe cheia 2 fără reîncercări inutile")
RULES[(K2, "gemini-3.1-flash-lite")] = MIN
c = ask("Cât să mă odihnesc între serii?")
ok(c[-1] == ("2", "gemini-3.5-flash-lite"), f"limită pe minut → modelul următor ({c})")
RULES[("*", "gemini-3.5-flash-lite")] = NOT_FOUND
for m in ["gemini-3.6-flash", "gemini-3-flash-preview", "gemini-3.8-flash"]:
    RULES[(K2, m)] = DAY
c = ask("Exerciții pentru abdomen acasă?")
ok(c[-1] == ("2", "gemini-2.5-flash"), f"model inexistent (404) sărit; 2.5 Flash răspunde ({c})")
tc = CALLS[-1][2]
ok(tc.thinking_budget == 0 and tc.thinking_level is None, "Gemini 2.5 primește thinking_budget, nu thinking_level")
ok(CALLS[0][2].thinking_level is not None, "Gemini 3.x primește thinking_level")
RULES[(K2, "gemini-2.5-flash")] = DAY; RULES[(K2, "gemini-2.5-flash-lite")] = DAY
say(at, "Cum fac flotări corect?")
errs = [e.value for e in at.error]
ok(any("epuizate" in e for e in errs), f"totul epuizat → mesaj clar ({errs[:1]})")
n = len(CALLS); say(at, "Și abdomene?")
ok(len(CALLS) == n, "mesajul următor nu mai face niciun apel (toate combinațiile sunt ținute minte)")

# cheie invalidă (Gemini întoarce 400 API_KEY_INVALID) → ocolită pe toate modelele
RULES.clear()
import streamlit as st
st.cache_resource.clear()      # golim memoria comună a cotelor (simulează un server repornit)
b = app({})
RULES[(K1, "*")] = api_error(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT")
b.session_state["key_index"] = 0
n = len(CALLS); say(b, "Salut"); c = [(x[0], x[1]) for x in CALLS[n:]]
ok(c == [("1", "gemini-3.1-flash-lite"), ("2", "gemini-3.1-flash-lite")], f"cheie invalidă → direct cheia 2, nu alt model ({c})")
