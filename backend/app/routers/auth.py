from fastapi import APIRouter, HTTPException, Request, status

from app.rate_limit import login_retry_after
from app.roles import Role
from app.schemas import CurrentUser, DemoLoginRequest, LoginRequest, LoginResponse, SessionUser
from app.security import CurrentUserDep, SettingsDep
from app.supabase import (
    InvalidCredentials,
    SupabaseError,
    lookup_synthetic_email,
    passwordless_session,
    sign_in_with_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# The seeded demo accounts (scripts/demo_users.example.json), one per role.
_DEMO_USERNAMES: dict[Role, str] = {
    Role.INVESTIGATING_OFFICER: "io.demo",
    Role.SUPERVISOR: "supervisor.demo",
    Role.ANALYST: "analyst.demo",
    Role.ADMIN: "admin.demo",
}


@router.post("/login")
async def login(body: LoginRequest, settings: SettingsDep) -> LoginResponse:
    """Backend-mediated username+password login (AUTH_ARCHITECTURE_4.md,
    Login Identifier - Path A). The client never sees or constructs the
    synthetic email; it only ever sends/receives username/password and a
    session.
    """
    try:
        email = await lookup_synthetic_email(body.username, settings)
        if email is None:
            # Same generic error as a wrong password -- do not leak whether
            # the username exists.
            raise InvalidCredentials()
        session = await sign_in_with_password(email, body.password, settings)
    except InvalidCredentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    except SupabaseError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Authentication service unavailable",
        )

    role = (session["user"].get("app_metadata") or {}).get("role")
    return LoginResponse(
        access_token=session["access_token"],
        refresh_token=session["refresh_token"],
        expires_in=session["expires_in"],
        token_type=session["token_type"],
        user=SessionUser(id=session["user"]["id"], role=role),
    )


@router.post("/demo-login")
async def demo_login(body: DemoLoginRequest, request: Request, settings: SettingsDep) -> LoginResponse:
    """Passwordless demo sign-in: signs in as the seeded demo user for the
    requested role. The role still comes from the server-verified JWT in the
    response, never from this request. Off unless PASSWORDLESS_LOGIN_ENABLED.
    """
    if not settings.passwordless_login_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    retry_after = login_retry_after(request, settings)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many sign-in attempts. Please wait a moment and try again.",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        email = await lookup_synthetic_email(_DEMO_USERNAMES[body.role], settings)
        if email is None:
            raise SupabaseError(f"demo user for {body.role.value} is not provisioned")
        session = await passwordless_session(email, settings)
    except SupabaseError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Authentication service unavailable",
        )

    role = (session["user"].get("app_metadata") or {}).get("role")
    return LoginResponse(
        access_token=session["access_token"],
        refresh_token=session["refresh_token"],
        expires_in=session["expires_in"],
        token_type=session["token_type"],
        user=SessionUser(id=session["user"]["id"], role=role),
    )


@router.get("/me")
async def me(current_user: CurrentUserDep) -> CurrentUser:
    """Returns the server-verified identity for the caller's JWT.

    Intended for the frontend's post-login redirect check ("If verified
    role != picked shell, redirect to correct shell" -- Role Authority Rule)
    - not as an authorization gate. Actual data access control lives at the
    RLS layer, not here.
    """
    return current_user
