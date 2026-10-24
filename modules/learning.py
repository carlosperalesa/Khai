"""Compatibility import; implementation lives in app.dreams.legacy_learning."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.dreams.legacy_learning")
