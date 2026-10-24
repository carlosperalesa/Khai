"""Compatibility import; implementation lives in app.senses.legacy_eyes."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.senses.legacy_eyes")
