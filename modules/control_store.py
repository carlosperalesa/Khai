"""Compatibility import; implementation lives in app.pixy.control."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.pixy.control")
