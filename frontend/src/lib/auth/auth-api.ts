import type { Role } from "@/lib/types/auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class AuthApiError extends Error {}

interface LoginResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  token_type: string;
  user: { id: string; role: Role };
}

/**
 * Passwordless demo sign-in: the backend signs in as the seeded demo user for
 * `role`. The role the app trusts is still the one in the response's
 * server-verified JWT, not the one requested here.
 */
export async function demoLogin(role: Role): Promise<LoginResponse> {
  const res = await fetch(`${API_URL}/auth/demo-login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role }),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new AuthApiError(body?.detail ?? "Sign-in failed. Try again.");
  }

  return res.json();
}
