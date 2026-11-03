"""Minimal i18n. English is the primary language: message ids ARE the English texts.

    from nina.i18n import _, N_
    label.setText(_("Open folder..."))       # translated when called
    LABELS = {"x": N_("Editing...")}         # N_ only marks a constant; call _() when you display it

Catalogs live in `nina/locales/<code>.py` as `CATALOG = {english: translation}`. A missing language or a missing
message falls back to English, so partial catalogs are fine. Add a language by adding a catalog module.
"""
import importlib
import os
import pkgutil

from nina import locales

_catalog: dict = {}
_language = "en"


def normalize(code: str) -> str:
    code = (code or "").strip().replace("-", "_")
    if not code or code.lower() in ("en", "c", "posix") or code.lower().startswith("en_"):
        return "en"
    return code.split(".")[0]


def set_language(code: str = "en") -> str:
    """Select the UI language ('en', 'pt-BR', 'auto' = from $LANG). Returns the language actually in use."""
    global _catalog, _language
    if (code or "").lower() == "auto":
        code = os.environ.get("LC_ALL") or os.environ.get("LANG") or "en"
    lang = normalize(code)
    _catalog, _language = {}, "en"
    if lang != "en":
        family = lang.split("_")[0]
        # exact match first ("pt_BR"), then any catalog of the same language family ("pt" -> "pt_BR")
        available = [m.name for m in pkgutil.iter_modules(locales.__path__)]
        for candidate in [lang] + [name for name in available if name.split("_")[0] == family]:
            if candidate not in available:
                continue
            module = importlib.import_module(f"nina.locales.{candidate}")
            _catalog, _language = dict(module.CATALOG), candidate
            break
    return _language


def current_language() -> str:
    return _language


def _(msgid: str, **kwargs) -> str:
    text = _catalog.get(msgid, msgid)
    return text.format(**kwargs) if kwargs else text


def N_(msgid: str) -> str:
    """Mark a message for translation without translating it yet (module-level constants)."""
    return msgid
