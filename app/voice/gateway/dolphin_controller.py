"""Backward-compatibility module. Controller logic moved to app.mind.brain."""
import requests
from app.mind.brain.controller import (
    ModelController,
    ModelController as DolphinController,
    PROFILES,
    PROFILE_LABELS,
    _format_error_reason,
    INACTIVITY_TIMEOUT_SEC,
)

__all__ = [
    'ModelController',
    'DolphinController',
    'PROFILES',
    'PROFILE_LABELS',
    '_format_error_reason',
    'INACTIVITY_TIMEOUT_SEC',
    'requests',
]
