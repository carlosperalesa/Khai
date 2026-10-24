"""Temporary deployment compatibility. New code imports app.bootstrap."""


def __getattr__(name):
    if name == 'ModuleManager':
        from app.bootstrap import ModuleManager
        return ModuleManager
    raise AttributeError(name)
