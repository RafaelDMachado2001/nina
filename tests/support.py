"""Shared test helpers: every test runs with its own HOME, config and state, so nothing real is read or changed."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from nina import config  # noqa: E402

app = QApplication.instance() or QApplication([])


class IsolatedHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.config_file = root / "config" / "nina" / "config.json"
        self.accounts = self.home / ".claude" / "accounts"
        for p in (mock.patch.object(config, "CONFIG_FILE", self.config_file),
                  mock.patch.object(config, "ACCOUNTS_ROOT", self.accounts),
                  mock.patch.object(config, "AUTOSTART_FILE", root / "config" / "autostart" / "nina.desktop"),
                  mock.patch("pathlib.Path.home", return_value=self.home),
                  mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                               "XDG_CONFIG_HOME": str(root / "config")})):
            p.start()
            self.addCleanup(p.stop)

    def write_config(self, data: dict):
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(json.dumps(data), encoding="utf-8")
