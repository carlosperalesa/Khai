"""Compatibility import; implementation lives in app.common.utils."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.common.utils")
