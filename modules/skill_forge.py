"""Compatibility import; implementation lives in app.hand.forge."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.hand.forge")
