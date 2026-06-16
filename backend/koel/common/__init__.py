"""Shared boilerplate inherited by every KOEL service.

Section 08 of the PRD ("Backend boilerplate and API standards") is the definition
of done for this package: Richardson L2 REST, OpenAPI/Swagger, OpenTelemetry,
log-level discipline, structured error bodies, Redis sessions, and JWT auth on
both REST and the WebSocket upgrade.
"""

from koel.common.app_factory import create_app
from koel.common.config import CommonSettings, get_common_settings
from koel.common.logging import configure_logging, get_logger

__all__ = [
    "create_app",
    "CommonSettings",
    "get_common_settings",
    "configure_logging",
    "get_logger",
]
