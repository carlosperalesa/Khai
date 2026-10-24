"""Compatibility import; implementation lives in app.voice.runtime.skill_runner."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("app.voice.runtime.skill_runner")
