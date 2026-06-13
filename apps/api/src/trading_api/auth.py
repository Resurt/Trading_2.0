"""Small auth placeholder with role separation for BFF endpoints."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import Header, HTTPException, status

from trading_api.schemas import ApiRole


def role_from_header(x_api_role: str | None = Header(default=None)) -> ApiRole:
    """Parse a local placeholder role from `X-API-Role`.

    This is intentionally not production auth. It gives the frontend/API contract
    a role boundary before real authentication is introduced.
    """

    if x_api_role is None:
        return ApiRole.OBSERVER
    try:
        return ApiRole(x_api_role.strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown API role",
        ) from exc


def require_role(role: ApiRole, allowed: Iterable[ApiRole]) -> ApiRole:
    if role not in set(allowed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role {role.value} is not allowed for this endpoint",
        )
    return role
