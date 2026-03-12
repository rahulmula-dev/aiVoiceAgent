import os
from enum import Enum
from typing import Optional, List
from fastapi import Header, HTTPException, status, Depends, Request, Query

class Role(str, Enum):
    IT = "IT"
    ADMIN = "ADMIN"

def validate_token(token: str) -> Optional[Role]:
    """Identifies the role associated with the provided token."""
    it_token = os.getenv("AUTH_TOKEN_IT", "default-it-token")
    admin_token = os.getenv("AUTH_TOKEN_ADMIN", "default-admin-token")
    
    # Backwards compatibility for the original reload-config token
    legacy_admin_token = os.getenv("ADMIN_RELOAD_TOKEN")
    
    if token == it_token:
        return Role.IT
    if token == admin_token or (legacy_admin_token and token == legacy_admin_token):
        return Role.ADMIN
        
    return None

async def get_current_user(x_auth_token: Optional[str] = Header(None)) -> Role:
    """FastAPI Dependency: Fetches role from Header (Standard API)."""
    if not x_auth_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authentication Token (X-Auth-Token Header)"
        )
    
    role = validate_token(x_auth_token)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Authentication Token"
        )
    
    return role

async def get_current_user_flexible(
    x_auth_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
) -> Role:
    """FastAPI Dependency: Checks both Header and Query Param (GUI/Dashboards)."""
    target_token = x_auth_token or token
    if not target_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Token required in Header (X-Auth-Token) or Query (?token=...)"
        )
    
    role = validate_token(target_token)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Authentication Token"
        )
    return role

async def get_current_user_ws(token: Optional[str] = Query(None)) -> Role:
    """WebSocket Helper: Fetches role from Query Parameter (token)."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: WebSocket requires 'token' query parameter."
        )
        
    role = validate_token(token)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Invalid WebSocket Token."
        )
        
    return role

def require_role(allowed_roles: List[Role], flexible: bool = False):
    """Dynamic Role Check for FastAPI Dependencies."""
    async def role_checker(role: Role = Depends(get_current_user_flexible if flexible else get_current_user)):
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access Denied: Required one of {allowed_roles}, got {role}."
            )
        return role
    return role_checker
