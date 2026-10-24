"""Compatibility import; implementation lives in app.mind.coordinator."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.mind.coordinator")
