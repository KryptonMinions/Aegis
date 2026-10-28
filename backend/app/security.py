import threading
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.roles import Role
from app.schemas import CurrentUser

bearer_scheme = HTTPBearer(auto_error=True)

BearerCredentialsDep = Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# This project issues tokens signed with modern asymmetric JWT signing keys
# (ES256), not the legacy HS256 shared secret -- verified against Supabase's
# public JWKS instead of a static secret. Hand-rolled rather than PyJWT's
# PyJWKClient: that class was observed to intermittently fail signature
# verification for valid tokens under FastAPI's threadpool (sync dependencies
# run one per thread), so keys are fetched and cached here explicitly under
# a lock instead of relying on its internal cache/thread-safety.
_jwks_lock = threading.Lock()
_jwks_by_url: dict[str, dict[str, jwt.PyJWK]] = {}


def _fetch_jwks(supabase_url: str) -> dict[str, jwt.PyJWK]:
    resp = httpx.get(f"{supabase_url}/auth/v1/.well-known/jwks.json")
    resp.raise_for_status()
    key_set = jwt.PyJWKSet.from_dict(resp.json())
    return {key.key_id: key for key in key_set.keys if key.key_id}


def _get_signing_key(supabase_url: str, kid: str) -> jwt.PyJWK:
    with _jwks_lock:
        keys = _jwks_by_url.get(supabase_url)
        if keys is None or kid not in keys:
            keys = _fetch_jwks(supabase_url)
            _jwks_by_url[supabase_url] = keys
    if kid not in keys:
        raise jwt.InvalidKeyError(f"No JWKS key found for kid={kid}")
    return keys[kid]


def verify_jwt(token: str, settings: Settings) -> CurrentUser:
    """Verify a raw Supabase JWT string and extract `sub` + `app_metadata.role`.

    Per AUTH_ARCHITECTURE_6.md's Role Authority Rule, this JWT is the only
    trusted role source. Nothing in this app should ever read a role from
    the request body, a query string, or any other client-supplied field.

    Raises `HTTPException(401)` on any verification failure. Split out from
    `get_current_user` so routes that need a differently-shaped 401 body
    (e.g. `/ask`'s `{"error": {...}}` envelope, ASK_ENDPOINT_CONTRACT.md
    §2.5) can call this directly instead of going through the FastAPI
    dependency, which fixes its own error response shape.
    """
    try:
        kid = jwt.get_unverified_header(token)["kid"]
        signing_key = _get_signing_key(settings.supabase_url, kid)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256"],
            audience="authenticated",
            # Tokens are used right after sign-in, so a few seconds of clock
            # skew vs Supabase would otherwise reject them as "not yet valid".
            leeway=10,
        )
    except (jwt.PyJWTError, httpx.HTTPError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc

    app_metadata = payload.get("app_metadata") or {}
    role_value = app_metadata.get("role")
    try:
        role = Role(role_value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has no recognized role",
        )

    officer_id = app_metadata.get("officer_id")
    return CurrentUser(id=payload["sub"], role=role, officer_id=officer_id)


def get_current_user(
    credentials: BearerCredentialsDep,
    settings: SettingsDep,
) -> CurrentUser:
    return verify_jwt(credentials.credentials, settings)


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
