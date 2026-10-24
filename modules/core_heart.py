"""Compatibility import; implementation lives in app.heart.legacy_heart."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.heart.legacy_heart")
