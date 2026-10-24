"""Compatibility import; Docker implementation belongs to Voice Runtime."""
import importlib, sys
sys.modules[__name__] = importlib.import_module("app.voice.runtime.docker_engine")
