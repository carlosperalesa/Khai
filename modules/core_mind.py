"""Compatibility import; implementation lives in app.mind.mind."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.mind.mind")
