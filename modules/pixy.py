"""Compatibility import; implementation lives in app.pixy.pixy."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.pixy.pixy")
