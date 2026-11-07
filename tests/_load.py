"""Load the integration's pure modules as a package WITHOUT running its __init__ (which needs Home Assistant)."""
import importlib
import os
import sys
import types

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "schwinn_bike")


def load(name):
    if "schwinn_pkg" not in sys.modules:
        pkg = types.ModuleType("schwinn_pkg")
        pkg.__path__ = [os.path.normpath(_DIR)]
        sys.modules["schwinn_pkg"] = pkg
    return importlib.import_module(f"schwinn_pkg.{name}")
