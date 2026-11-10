import unittest

from tests.support import IsolatedHome  # noqa: F401  (sets up the offscreen Qt application)

from nina.agents import roles
from nina.agents.openai_client import ApiError
from nina.connections_view import ConnectionForm, ConnectionsDialog

LOCAL = {"id": "local", "kind": "openai", "name": "Ollama", "base_url": "http://localhost:11434/v1", "model": "qwen3:8b",
         "key": {"source": "none"}}


class FakeClient:
    def __init__(self, models=None, error=None, answer="OK"):
        self.models, self.error, self.answer, self.calls = models or ["m-a", "m-b"], error, answer, []

    def list_models(self, base_url, key, timeout=20, headers=None):
        self.calls.append(("models", base_url, key))
        if self.error:
            raise self.error
        return list(self.models)

    def chat(self, base_url, key, model, messages, tools, timeout, headers):
        self.calls.append(("chat", base_url, key, model))
        if self.error:
            raise self.error
        return {"choices": [{"message": {"role": "assistant", "content": self.answer}}]}

    @staticmethod
    def friendly(err):
        return f"friendly: {err.message}"


class FakeKeystore:
    def __init__(self, usable=True):
        self.store, self.usable = {}, usable

    def keyring_usable(self):
        return self.usable

    def set_key(self, conn_id, value):
        self.store[conn_id] = value

    def delete_key(self, conn_id):
        self.store.pop(conn_id, None)

    def get_key(self, conn):
        return self.store.get(conn.get("id"), "") if (conn.get("key") or {}).get("source") == "keyring" else ""


def sync(fn, done):
    done(fn())


class ConnectionsDialogTest(IsolatedHome):
    def make(self, cfg=None, client=None, keystore=None):
        self.saved = []
        self.client, self.keys = client or FakeClient(), keystore or FakeKeystore()
        d = ConnectionsDialog(cfg or {"connections": [LOCAL]}, ["work"], self.saved.append, roles=roles.load_roles(),
                              client=self.client, keystore=self.keys, run_async=sync)
        self.addCleanup(d.close)
        return d

    def ids(self, d):
        return [d.listing.item(i).data(256) for i in range(d.listing.count())]

    def test_lists_claude_accounts_then_custom_connections(self):
        d = self.make()
        self.assertEqual(self.ids(d), ["claude", "claude:work", "local"])

    def test_adding_from_a_preset_stores_the_key_in_the_keyring_not_in_the_config(self):
        d = self.make()
        form = d.make_form(None)
        form.preset.setCurrentIndex(form.preset.findData("gemini"))
        self.assertEqual(form.base_url.text(), "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertEqual(form.key_source.currentData(), "keyring")
        form.name.setText("My Gemini")
        form.model.setCurrentText("gemini-x")
        form.secret.setText("sk-secret")
        d.accept_form(form, None)
        d.accept()
        cfg = self.saved[-1]
        conn = next(c for c in cfg["connections"] if c["id"] == "my-gemini")
        self.assertEqual((conn["kind"], conn["model"], conn["key"]), ("openai", "gemini-x", {"source": "keyring"}))
        self.assertNotIn("sk-secret", repr(cfg))
        self.assertEqual(self.keys.store, {"my-gemini": "sk-secret"})

    def test_ids_are_unique_and_never_claude(self):
        d = self.make()
        for name in ("Ollama", "local", "Claude"):
            form = d.make_form(None)
            form.preset.setCurrentIndex(form.preset.findData("ollama"))
            form.name.setText(name)
            d.accept_form(form, None)
        self.assertEqual(self.ids(d)[3:], ["ollama", "local-2", "claude-2"])

    def test_env_var_keys_and_validation(self):
        d = self.make()
        form = d.make_form(None)
        form.preset.setCurrentIndex(form.preset.findData("custom"))
        form.name.setText("Corp")
        self.assertIn("URL", form.problem())
        form.base_url.setText("https://ai.corp.example/v1/")
        form.model.setCurrentText("corp-1")
        form.key_source.setCurrentIndex(form.key_source.findData("env"))
        self.assertTrue(form.problem())                                   # the variable name is missing
        form.env_name.setText("CORP_KEY")
        self.assertEqual(form.problem(), "")
        conn = form.connection("corp")
        self.assertEqual((conn["base_url"], conn["key"]), ("https://ai.corp.example/v1", {"source": "env", "name": "CORP_KEY"}))

    def test_editing_keeps_the_id_and_an_empty_secret_keeps_the_stored_key(self):
        self.keys = FakeKeystore()
        self.keys.store["local"] = "old"
        d = self.make(cfg={"connections": [{**LOCAL, "key": {"source": "keyring"}}]}, keystore=self.keys)
        form = d.make_form(d.connection_by_id("local"))
        form.name.setText("Ollama at home")
        d.accept_form(form, "local")
        d.accept()
        conn = self.saved[-1]["connections"][0]
        self.assertEqual((conn["id"], conn["name"]), ("local", "Ollama at home"))
        self.assertEqual(self.keys.store, {"local": "old"})

    def test_removing_deletes_the_key_bindings_and_the_default(self):
        self.keys = FakeKeystore()
        self.keys.store["local"] = "k"
        cfg = {"connections": [{**LOCAL, "key": {"source": "keyring"}}], "default_connection": "local",
               "role_connections": {"writing-reviewer": "local", "general": {"connection": "local", "model": "x"},
                                    "architect": "claude"}}
        d = self.make(cfg=cfg, keystore=self.keys)
        d.remove("local")
        d.accept()
        cfg = self.saved[-1]
        self.assertEqual((cfg["connections"], cfg["default_connection"], cfg["role_connections"]), ([], "", {"architect": "claude"}))
        self.assertEqual(self.keys.store, {})

    def test_cancel_changes_nothing(self):
        d = self.make()
        d.remove("local")
        d.reject()
        self.assertEqual((self.saved, self.keys.store), ([], {}))

    def test_default_connection(self):
        d = self.make()
        d.set_default("local")
        d.accept()
        self.assertEqual(self.saved[-1]["default_connection"], "local")
        d2 = self.make(cfg={"connections": [LOCAL], "default_connection": "local"})
        d2.set_default(None)                                              # back to following the Claude account menu
        d2.accept()
        self.assertEqual(self.saved[-1]["default_connection"], "")

    def test_agents_tab_binds_roles_with_an_optional_model(self):
        d = self.make()
        d.set_binding("writing-reviewer", "local", "")
        d.set_binding("architect", "local", "big-model")
        d.set_binding("general", None, "")
        d.accept()
        self.assertEqual(self.saved[-1]["role_connections"],
                         {"writing-reviewer": "local", "architect": {"connection": "local", "model": "big-model"}})
        row = d.binding_row("architect")
        self.assertEqual((row["connection"].currentData(), row["model"].text()), ("local", "big-model"))

    def test_orchestration_settings_are_saved(self):
        d = self.make(cfg={"connections": [LOCAL], "orchestration": {"mode": "manual", "allow": {"general": ["architect"]}}})
        self.assertEqual(d.orch_mode.currentData(), "manual")
        d.orch_mode.setCurrentIndex(d.orch_mode.findData("collaborative"))
        d.orch_depth.setValue(3)
        d.orch_budget.setValue(5)
        d.orch_ask.setChecked(False)
        d.accept()
        self.assertEqual(self.saved[-1]["orchestration"], {"mode": "collaborative", "max_depth": 3, "max_delegations": 5,
                                                           "ask": False, "allow": {"general": ["architect"]}})

    def test_test_button_reports_the_answer_or_the_error(self):
        d = self.make()
        self.assertIn("OK", d.test_text(d.connection_by_id("local")))
        self.assertEqual(self.client.calls[-1], ("chat", "http://localhost:11434/v1", "", "qwen3:8b"))
        d = self.make(client=FakeClient(error=ApiError(401, "bad key")))
        self.assertIn("bad key", d.test_text(d.connection_by_id("local")))

    def test_fetch_models_fills_the_list_or_shows_why_not(self):
        d = self.make()
        form = d.make_form(d.connection_by_id("local"))
        form.fetch_models()
        self.assertEqual([form.model.itemText(i) for i in range(form.model.count())], ["m-a", "m-b"])
        self.assertEqual(form.model.currentText(), "qwen3:8b")             # the current choice stays
        d = self.make(client=FakeClient(error=ApiError(0, "refused")))
        form = d.make_form(d.connection_by_id("local"))
        form.fetch_models()
        self.assertIn("refused", form.status.text())

    def test_without_a_keyring_the_form_offers_environment_variables(self):
        d = self.make(keystore=FakeKeystore(usable=False))
        form = d.make_form(None)
        form.preset.setCurrentIndex(form.preset.findData("openai"))
        self.assertEqual(form.key_source.currentData(), "env")
        self.assertEqual(form.key_source.findData("keyring"), -1)
        self.assertIsInstance(form, ConnectionForm)


if __name__ == "__main__":
    unittest.main()
