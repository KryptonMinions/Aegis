"use client";

import { createContext, useContext, useSyncExternalStore, type ReactNode } from "react";
import type { AuthUser, Role } from "@/lib/types/auth";
import { demoLogin } from "@/lib/auth/auth-api";

const SESSION_KEY = "ksp-ask-auth-session";

interface AuthContextValue {
  user: AuthUser | null;
  ready: boolean;
  loginAs: (role: Role) => Promise<AuthUser>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// Demo personas (References/KSP_Demo_Script.pdf), one per role.
const PERSONA_NAME: Record<Role, string> = {
  investigating_officer: "Inspector Kavitha Nair",
  supervisor: "SP Meena Kulkarni",
  analyst: "Analyst Prakash Rao",
  admin: "System Administrator",
};

const DEFAULT_UNIT: Record<Role, string> = {
  investigating_officer: "Indiranagar PS | Bengaluru Urban",
  supervisor: "Mysuru District",
  analyst: "DCRB Bengaluru Urban",
  admin: "System Administration",
};

// A tiny external store over sessionStorage, read via useSyncExternalStore
// (the primitive built for syncing with browser APIs — avoids the
// hydrate-then-setState-in-an-effect pattern entirely).
let listeners: Array<() => void> = [];
let cachedRaw: string | null | undefined;
let cachedUser: AuthUser | null = null;

function readUser(): AuthUser | null {
  const raw = window.sessionStorage.getItem(SESSION_KEY);
  if (raw === cachedRaw) return cachedUser;
  cachedRaw = raw;
  if (!raw) {
    cachedUser = null;
    return null;
  }
  try {
    cachedUser = JSON.parse(raw) as AuthUser;
  } catch {
    window.sessionStorage.removeItem(SESSION_KEY);
    cachedUser = null;
  }
  return cachedUser;
}

function subscribe(listener: () => void) {
  listeners.push(listener);
  return () => {
    listeners = listeners.filter((l) => l !== listener);
  };
}

function writeUser(user: AuthUser | null) {
  if (user) {
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify(user));
  } else {
    window.sessionStorage.removeItem(SESSION_KEY);
  }
  cachedRaw = user ? JSON.stringify(user) : null;
  cachedUser = user;
  listeners.forEach((l) => l());
}

const noopSubscribe = () => () => {};

/**
 * Presentation-only session context. This decides which shell/nav/screens
 * to present — it is NOT a security boundary. RBAC is enforced at the DB
 * view layer (steering-docs FRONTEND_UI_STEERING.md §4/§11); the real
 * backend must never trust this client-side role when scoping data.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const user = useSyncExternalStore(subscribe, readUser, () => null);
  // True once we're past the server-rendered pass and can trust `user`.
  const ready = useSyncExternalStore(noopSubscribe, () => true, () => false);

  const loginAs: AuthContextValue["loginAs"] = async (requestedRole) => {
    // Role Authority Rule (AUTH_ARCHITECTURE_5.md): the only trusted role
    // is the one FastAPI returns from the server-verified JWT. The tile the
    // user tapped only picks which demo account the backend signs in; the
    // name, unit and routing below all use the role from the response.
    const session = await demoLogin(requestedRole);
    const authUser: AuthUser = {
      id: session.user.id,
      name: PERSONA_NAME[session.user.role],
      role: session.user.role,
      unit: DEFAULT_UNIT[session.user.role],
      accessToken: session.access_token,
    };
    writeUser(authUser);
    return authUser;
  };

  const logout = () => writeUser(null);

  return (
    <AuthContext.Provider value={{ user, ready, loginAs, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

/**
 * Non-hook accessor for modules that aren't React components/hooks (e.g.
 * `real-ask-service.ts`, ASK_ENDPOINT_CONTRACT.md §3.3: "read access token
 * from the existing auth context"). Reads the same sessionStorage-backed
 * session `useAuth()` does, just without the subscription machinery.
 */
export function getAccessToken(): string | undefined {
  return readUser()?.accessToken;
}

/** Clears the session, e.g. after the backend rejects an expired/invalid
 * token (`UnauthenticatedError`) outside of a component's render cycle. */
export function clearSession(): void {
  writeUser(null);
}
