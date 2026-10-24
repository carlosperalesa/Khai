"""Compatibility import; implementation lives in app.mind.state."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.mind.state")
