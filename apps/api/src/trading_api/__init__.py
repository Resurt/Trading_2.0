"""FastAPI BFF service skeleton."""

from trading_api.app import app, create_fastapi_app, create_identity, health

__all__ = ["app", "create_fastapi_app", "create_identity", "health"]
