import os
import unittest
from unittest import mock

from nina import connections, secrets

GEMINI = {"id": "gemini", "kind": "openai", "name": "Gemini", "model": "gemini-x",
          "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key": {"source": "env", "name": "GK"}}
LOCAL = {"id": "local", "kind": "openai", "name": "Ollama", "model": "qwen3:8b", "base_url": "http://localhost:11434/v1"}
ROLE = {"id": "writing-reviewer", "model": "sonnet"}


def cfg(**kw):
    return {"connections": [GEMINI, LOCAL], "default_connection": "", "role_connections": {}, **kw}


def resolve(c, explicit=None, role=ROLE, active="", sdk=True, **kw):
    return connections.resolve(c, ["work", "home"], explicit=explicit, role_id=role["id"], role=role,
                               active_account=active, sdk=sdk, **kw)


class ConnectionsTest(unittest.TestCase):
    def test_claude_connections_from_accounts(self):
        got = connections.claude_connections(["work"])
        self.assertEqual([(c["id"], c["kind"], c["account"]) for c in got], [("claude", "claude", ""), ("claude:work", "claude", "work")])

    def test_custom_connections_are_normalised_and_invalid_ones_skipped(self):
        c = cfg(connections=[LOCAL, {"id": "nourl", "kind": "openai", "name": "x"}, {"id": "nokind", "base_url": "http://x"},
                             "junk", {**LOCAL, "id": "claude"}])
        got = connections.all_connections(c, [])
        custom = [x for x in got if x["kind"] == "openai"]
        self.assertEqual([x["id"] for x in custom], ["local"])                 # invalid and reserved ids are skipped
        self.assertEqual((custom[0]["timeout_s"], custom[0]["key"], custom[0]["price_in"], custom[0]["context_window"]),
                         (300, {"source": "none"}, 0.0, 0))
        self.assertEqual(connections.get(c, [], "local")["model"], "qwen3:8b")
        self.assertIsNone(connections.get(c, [], "missing"))

    def test_resolution_order(self):
        role = {**ROLE, "connection": "local"}
        c = cfg(default_connection="claude:home", role_connections={"writing-reviewer": "gemini"})
        self.assertEqual(resolve(c, explicit="claude:work", role=role)[0]["id"], "claude:work")
        self.assertEqual(resolve(c, role=role)[0]["id"], "gemini")             # binding beats the role file
        del c["role_connections"]["writing-reviewer"]
        self.assertEqual(resolve(c, role=role)[0]["id"], "local")              # role file beats the default
        self.assertEqual(resolve(c)[0]["id"], "claude:home")                   # the default
        self.assertEqual(resolve(cfg(), active="work")[0]["id"], "claude:work")  # unset: follows the active account
        self.assertEqual(resolve(cfg())[0]["id"], "claude")

    def test_binding_with_model_override(self):
        c = cfg(role_connections={"writing-reviewer": {"connection": "local", "model": "llama3"}})
        conn, model, _note = resolve(c)
        self.assertEqual((conn["id"], model), ("local", "llama3"))
        self.assertEqual(resolve(c, explicit="local", explicit_model="mistral")[1], "mistral")

    def test_role_model_only_applies_to_claude(self):
        self.assertEqual(resolve(cfg(), explicit="gemini")[1], "gemini-x")     # never "sonnet" on Gemini
        self.assertEqual(resolve(cfg(), explicit="claude")[1], "sonnet")

    def test_unknown_connection_falls_back_with_note(self):
        conn, _model, note = resolve(cfg(), explicit="gone")
        self.assertEqual(conn["id"], "claude")
        self.assertIn("gone", note)

    def test_without_sdk_claude_resolves_to_first_custom(self):
        conn, _model, note = resolve(cfg(), sdk=False)
        self.assertEqual(conn["id"], "gemini")
        self.assertIn("Gemini", note)                                          # never a silent switch
        self.assertEqual(resolve(cfg(connections=[]), sdk=False)[0]["id"], "claude")   # nothing else: still Claude

    def test_account_to_connection(self):
        f = connections.account_to_connection
        self.assertEqual((f(None), f(""), f("work")), (None, None, "claude:work"))

    def test_presets_are_complete(self):
        for name in ("openai", "gemini", "ollama", "openrouter", "groq", "lmstudio", "custom"):
            p = connections.PRESETS[name]
            self.assertEqual(set(p), {"name", "base_url", "needs_key", "key_url"}, name)


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


class SecretsTest(unittest.TestCase):
    def test_env_key_and_fake_keyring(self):
        with mock.patch.dict(os.environ, {"GK": "from-env"}):
            self.assertEqual(secrets.get_key(GEMINI), "from-env")
        fake = FakeKeyring()
        with mock.patch.object(secrets, "keyring", fake):
            conn = {**LOCAL, "key": {"source": "keyring"}}
            self.assertEqual(secrets.get_key(conn), "")
            secrets.set_key("local", "s3cret")
            self.assertEqual(fake.store, {("nina", "local"): "s3cret"})
            self.assertEqual(secrets.get_key(conn), "s3cret")
            secrets.delete_key("local")
            self.assertEqual(secrets.get_key(conn), "")
        self.assertEqual(secrets.get_key(LOCAL), "")                           # no key at all

    def test_missing_env_var_gives_empty(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NINA_SURELY_MISSING", None)
            with mock.patch.object(secrets, "_windows_user_env", return_value=None):
                self.assertEqual(secrets.get_key({"key": {"source": "env", "name": "NINA_SURELY_MISSING"}}), "")

    def test_without_keyring_storing_raises_a_clear_error(self):
        with mock.patch.object(secrets, "keyring", None):
            self.assertFalse(secrets.keyring_usable())
            with self.assertRaises(RuntimeError):
                secrets.set_key("x", "y")


if __name__ == "__main__":
    unittest.main()
