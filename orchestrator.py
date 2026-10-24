"""Compatibility entrypoint for existing deployment commands."""
if __name__ == '__main__':
    import runpy
    runpy.run_module('app.voice.voice', run_name='__main__')
else:
    import importlib, sys
    sys.modules[__name__] = importlib.import_module('app.voice.voice')
