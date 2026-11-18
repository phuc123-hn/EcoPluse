"""
Security middleware for EcoPulse.

Usage in backend/main.py:
    from . import security
    security.install(app)

This module handles HTTP response security headers and cache policy.
Authentication, authorization, session management, upload validation, and
production-environment detection should remain in their existing modules.
"""

from __future__ import annotations

from typing import Any


# Restrict which sites may embed EcoPulse without imposing a restrictive
# default-src policy that could unexpectedly block external map tiles or CDNs.
CONTENT_SECURITY_POLICY = "frame-ancestors 'self'"


async def _security_headers_middleware(request: Any, call_next: Any) -> Any:
    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )

    # This endpoint returns authentication-related name availability data.
    if request.url.path == "/auth/name-free":
        response.headers["Cache-Control"] = "no-store"

    # Cache policy for static map images. Names starting with land_/layer_/swi_ contain a content hash or data
    # version, so a given name never changes and can be cached for a year; other tiles are re-checked every 5 min.
    path = request.url.path
    if "cache-control" not in response.headers:
        if path.startswith("/tiles/"):
            hashed = path.rsplit("/", 1)[-1].startswith(("land_", "layer_", "swi_"))
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if hashed else "public, max-age=300"
        elif path.startswith("/static/img/"):
            response.headers["Cache-Control"] = "public, max-age=86400"

    return response


def install(app: Any) -> None:
    """Register the security headers middleware on a FastAPI application."""
    app.middleware("http")(_security_headers_middleware)
