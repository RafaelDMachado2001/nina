"""API keys of connections. Never stored in config.json, never logged.

A connection's "key" says where its key lives:
- {"source": "keyring"}: the system keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service),
  service "nina", user = the connection id.
- {"source": "env", "name": "VAR"}: an environment variable. On Windows a variable saved with `setx` after NINA
  started is read from the user's registry, so no restart is needed.
- {"source": "none"}: no key (local servers such as Ollama).
"""
import os
import sys

try:
    import keyring
except Exception:      # optional at import time: env vars still work without it
    keyring = None

SERVICE = "nina"


def _windows_user_env(name: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return None


def keyring_usable() -> bool:
    if keyring is None:
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:
        return False
    # keyring's "fail" and "null" backends cannot store anything
    return type(backend).__module__.split(".")[-1] not in ("fail", "null")


def get_key(conn: dict) -> str:
    spec = conn.get("key") if isinstance(conn.get("key"), dict) else {}
    source = spec.get("source")
    if source == "env":
        name = str(spec.get("name") or "")
        return (os.environ.get(name) or _windows_user_env(name) or "") if name else ""
    if source == "keyring" and keyring is not None:
        try:
            return keyring.get_password(SERVICE, str(conn.get("id", ""))) or ""
        except Exception:
            return ""
    return ""


def set_key(conn_id: str, value: str) -> None:
    if keyring is None:
        raise RuntimeError("no system keyring is available; use an environment variable instead")
    keyring.set_password(SERVICE, conn_id, value)


def delete_key(conn_id: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(SERVICE, conn_id)
    except Exception:          # nothing stored
        pass
