import re, time, warnings, logging, supabase, fake_sb, fake_gemini
logging.disable(logging.WARNING); warnings.filterwarnings("ignore")
supabase.create_client = fake_sb.create_client
fake_gemini.install()
from streamlit.testing.v1 import AppTest
DB = fake_sb.DB
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)


def app(storage=None, sid_url=None):
    at = AppTest.from_file("app_under_test.py", default_timeout=90)
    at.session_state["_browser_storage"] = storage if storage is not None else {}
    if sid_url:
        at.query_params["session_id"] = sid_url
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def click(at, key):
    at.button(key=key).click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def fill(at, **kv):
    for k, v in kv.items():
        at.text_input(key=k).input(v)


def say(at, text):
    at.chat_input[0].set_value(text).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def user_of(at):
    return at.session_state["_auth_user"] if "_auth_user" in at.session_state else None


def token_from(at):
    m = re.search(r'"([^"]+)"', at.session_state["_auth_ls_js"] or "")
    return m.group(1) if m else None
