"""Gemini simulat: reguli (cheie, model) -> excepție; înregistrează fiecare apel."""
import types as pytypes
import google.genai as genai
from google.genai import errors

CALLS = []      # (ultima cifră a cheii, model, thinking_config, contents)
RULES = {}      # (cheie | "*", model | "*") -> excepție sau "empty"
REPLY = {"text": None}


def api_error(code, message, status="X"):
    return errors.APIError(code, {"error": {"code": code, "message": message, "status": status}})


class _Models:
    def __init__(self, key): self.key = key

    def generate_content_stream(self, model, contents, config):
        CALLS.append((self.key[-1], model, config.thinking_config, contents))
        rule = RULES.get((self.key, model)) or RULES.get(("*", model)) or RULES.get((self.key, "*"))
        if rule == "empty":
            return iter([pytypes.SimpleNamespace(text=None)])
        if rule:
            raise rule
        text = REPLY["text"] or f"Răspuns de la {model} cu cheia {self.key[-1]}."
        return iter([pytypes.SimpleNamespace(text=text)])


class _Files:
    UPLOADS = []

    def __init__(self, key): self.key = key

    def upload(self, file, config=None):
        _Files.UPLOADS.append(self.key[-1])
        from google.genai import types
        return pytypes.SimpleNamespace(name=f"files/{len(_Files.UPLOADS)}", uri=f"https://f/{len(_Files.UPLOADS)}",
                                       state=types.FileState.ACTIVE)

    def get(self, name): raise AssertionError("nu ar trebui apelat")


class FakeGemini:
    def __init__(self, api_key=None):
        self.models = _Models(api_key)
        self.files = _Files(api_key)


def install():
    genai.Client = FakeGemini
