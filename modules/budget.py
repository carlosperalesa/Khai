"""Compatibility import; implementation lives in app.pixy.budget."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.pixy.budget")
