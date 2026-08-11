"""
limiter.py — Shared slowapi Limiter instance.

WHY THIS FILE EXISTS:
  Previously auth/router.py created its OWN Limiter() instance which was
  never attached to app.state. The @limiter.limit() decorator silently did
  nothing, leaving /auth/login completely unprotected against bcrypt burnout.

  This module defines ONE limiter.  main.py attaches it to app.state.
  Every router imports from here so they all share the same state store.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200/minute"],
)
