"""Compatibility import; implementation lives in app.voice.gateway.telegram_gateway."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.voice.gateway.telegram_gateway")
