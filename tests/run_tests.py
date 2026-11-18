#!/usr/bin/env python3
"""Rulează testele GymBro cu Supabase și Gemini SIMULATE (fără rețea, fără chei reale).

    pip install -r requirements.txt
    python3 tests/run_tests.py             # toate
    python3 tests/run_tests.py auth_test   # doar unul
    python3 tests/run_tests.py -v          # arată fiecare verificare

Fiecare test rulează într-un folder temporar cu copia aplicației (app_under_test.py),
session_bridge/, fișierele fake_*.py și un .streamlit/secrets.toml de test (chei false).
"""
import os, shutil, subprocess, sys, tempfile, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = pathlib.Path(__file__).resolve().parent
APP = os.environ.get("APP_FILE", "app.py")

SUPABASE = '''SUPABASE_URL = "https://example.invalid"
SUPABASE_KEY = "sb_secret_fakefakefakefake"
SMTP_USER = "gymbro.test@gmail.com"
SMTP_PASSWORD = "abcd efgh ijkl mnop"
'''
KEYS = 'GOOGLE_API_KEYS = ["AIzaFAKE1111111111111111", "AIzaFAKE2222222222222222"]\n'

# test -> secrets
SUITE = {
    "auth_test": SUPABASE + KEYS,
    "reset_test": SUPABASE + KEYS,
    "rot_test": SUPABASE + KEYS,
    "chat_test": SUPABASE + KEYS,
    "local_test": KEYS,            # fără Supabase: SQLite, fără conturi
}


def run(name):
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        shutil.copy(ROOT / APP, d / "app_under_test.py")
        if (ROOT / "session_bridge").exists():
            shutil.copytree(ROOT / "session_bridge", d / "session_bridge")
        for f in ("fake_sb.py", "fake_gemini.py", "common.py", f"{name}.py"):
            shutil.copy(TESTS / f, d / f)
        (d / ".streamlit").mkdir()
        (d / ".streamlit" / "secrets.toml").write_text(SUITE[name])
        p = subprocess.run([sys.executable, f"{name}.py"], cwd=d, capture_output=True, text=True, timeout=900)
        out = p.stdout + p.stderr
        lines = [l for l in out.splitlines() if l.startswith(("PASS", "FAIL"))]
        passed = sum(l.startswith("PASS") for l in lines)
        failed = sum(l.startswith("FAIL") for l in lines) + (1 if "Traceback" in out else 0)
        report = "\n".join("   " + l for l in lines)
        if "Traceback" in out:
            report += "\n   " + "\n   ".join(out[out.index("Traceback"):].splitlines()[-8:])
        return passed, failed, report


def main():
    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
    names = [n for n in SUITE if not wanted or n in wanted]
    tot_p = tot_f = 0
    for n in names:
        p, f, rep = run(n)
        tot_p += p; tot_f += f
        print(f"{'✅' if not f else '❌'} {n}: {p} trec, {f} pică")
        if f or "-v" in sys.argv:
            print(rep)
    print(f"\nTotal: {tot_p} trec, {tot_f} pică")
    sys.exit(1 if tot_f else 0)


if __name__ == "__main__":
    main()
