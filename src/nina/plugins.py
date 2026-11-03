"""
Local plugins: Python files in ~/.config/nina/plugins/*.py, loaded when NINA starts. Standard library only.

A plugin is a plain module with two optional functions:

    def setup(nina):          # called once, after the mascot window exists
        ...
    def menu(nina, menu):     # called every time the right-click menu is built; `menu` is a QMenu
        ...

`nina` is the mascot widget. Its public plugin API:

    nina.cfg                                         the loaded config.json (dict)
    nina.say(text, seconds=2.5)                      speech bubble
    nina.touch()                                     wake up / reset the idle timer
    nina.set_alert(key, text, count=1, on_click=None, ttl_s=1200)   raise a persistent alert (!) with a sticky bubble
    nina.get_alert(key) / nina.clear_alert(key)
    nina.launch(directory, account=None, profile=None)               open Claude Code in a terminal
    nina.run_process(program, args, done, timeout_s=60)              non-blocking; done(code, stdout, stderr)
    nina.show_text(title, text, ok=True)                             simple message box

A plugin that raises an exception never takes NINA down: the error is printed to stderr and the plugin is skipped.
Plugins are executed as you (they are your own code): only put files you trust in that folder.
"""
import importlib.util
import sys

from nina import config


def load_plugins(nina) -> list:
    folder = config.plugins_dir()
    modules = []
    if not nina.cfg.get("plugins", True) or not folder.is_dir():
        return modules
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"nina_plugin_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            setup = getattr(module, "setup", None)
            if callable(setup):
                setup(nina)
            modules.append(module)
        except Exception as e:
            print(f"NINA: plugin {path.name} failed to load: {e}", file=sys.stderr)
    return modules


def build_menus(modules: list, nina, menu) -> None:
    for module in modules:
        fn = getattr(module, "menu", None)
        if callable(fn):
            try:
                fn(nina, menu)
            except Exception as e:
                print(f"NINA: plugin {module.__name__} menu failed: {e}", file=sys.stderr)
