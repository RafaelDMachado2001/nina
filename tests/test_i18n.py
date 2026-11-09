import ast
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from nina import i18n
from nina.i18n import N_, _
from nina.locales import pt_BR

SRC = Path(__file__).resolve().parent.parent / "src" / "nina"


def message_ids() -> set:
    ids = set()
    for f in SRC.rglob("*.py"):
        if "locales" in f.parts:
            continue
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("_", "N_") and n.args:
                a = n.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    ids.add(a.value)
    return ids


class I18nTest(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_english_is_the_default_and_passes_through(self):
        i18n.set_language("en")
        self.assertEqual((_("Quit"), _("Agents ({n} active)...", n=3), N_("Quit")), ("Quit", "Agents (3 active)...", "Quit"))

    def test_portuguese(self):
        for code in ("pt-BR", "pt_BR", "pt_BR.UTF-8", "pt"):
            self.assertEqual(i18n.set_language(code), "pt_BR", code)
            self.assertEqual(_("Quit"), "Sair")
        self.assertEqual(_("Agents ({n} active)...", n=2), "Agentes (2 ativo(s))...")

    def test_unknown_language_and_unknown_messages_fall_back_to_english(self):
        self.assertEqual(i18n.set_language("xx-YY"), "en")
        self.assertEqual(_("Quit"), "Quit")
        i18n.set_language("pt-BR")
        self.assertEqual(_("A message nobody translated {x}", x=1), "A message nobody translated 1")

    def test_auto_uses_the_environment(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "", "LANG": "pt_BR.UTF-8"}):
            self.assertEqual(i18n.set_language("auto"), "pt_BR")
        with mock.patch.dict(os.environ, {"LC_ALL": "", "LANG": "en_US.UTF-8"}):
            self.assertEqual(i18n.set_language("auto"), "en")

    def test_user_text_with_braces_is_never_formatted(self):
        i18n.set_language("pt-BR")
        self.assertEqual(_("Request: {text}", text="fix {this} {0}"), "Pedido: fix {this} {0}")

    def test_the_portuguese_catalog_is_complete_and_consistent(self):
        ids = message_ids()
        missing = sorted(ids - set(pt_BR.CATALOG))
        self.assertEqual(missing, [], "messages without a pt-BR translation")
        stale = sorted(set(pt_BR.CATALOG) - ids)
        self.assertEqual(stale, [], "pt-BR entries that no message uses anymore")
        field = re.compile(r"\{(\w+)\}")
        for msgid, text in pt_BR.CATALOG.items():
            self.assertEqual(set(field.findall(msgid)), set(field.findall(text)), f"placeholders differ: {msgid!r}")


if __name__ == "__main__":
    unittest.main()
