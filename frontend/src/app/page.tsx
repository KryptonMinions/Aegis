"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Shield, Users, Search, Settings } from "lucide-react";
import { KspEmblem } from "@/components/shared/KspEmblem";
import { RoleTile } from "@/components/auth/RoleTile";
import { useAuth } from "@/lib/auth/auth-context";
import { AuthApiError } from "@/lib/auth/auth-api";
import type { Role } from "@/lib/types/auth";

const ROLE_TILES: { role: Role; label: string; description: string; icon: typeof Shield }[] = [
  { role: "investigating_officer", label: "Investigating Officer", description: "Field lookups on the go", icon: Shield },
  { role: "supervisor", label: "Supervisor", description: "Unit oversight & review packs", icon: Users },
  { role: "analyst", label: "Crime Analyst", description: "Pattern & network analysis", icon: Search },
  { role: "admin", label: "Admin", description: "Audit & user management", icon: Settings },
];

export default function RoleSelectScreen() {
  const { loginAs } = useAuth();
  const router = useRouter();
  const [pending, setPending] = useState<Role | null>(null);
  const [error, setError] = useState<string | null>(null);

  const enter = async (role: Role) => {
    if (pending) return;
    setError(null);
    setPending(role);
    try {
      // Routing uses the server-verified role in the session, not the tile.
      const authUser = await loginAs(role);
      router.push(authUser.role === "investigating_officer" ? "/ask" : "/dashboard");
    } catch (err) {
      setError(err instanceof AuthApiError ? err.message : "Sign-in failed. Try again.");
      setPending(null);
    }
  };

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-10 bg-muted/30 p-6">
      <KspEmblem />
      <div className="text-center">
        <h1 className="text-lg font-semibold">Choose a role to explore</h1>
        <p className="text-sm text-muted-foreground">
          Karnataka State Police · Intelligent Conversational AI &amp; Crime Analytics Platform
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Demo with synthetic data. No sign-in needed.
        </p>
      </div>
      <div className="grid w-full max-w-2xl grid-cols-1 gap-3 sm:grid-cols-2">
        {ROLE_TILES.map((tile) => (
          <RoleTile
            key={tile.role}
            label={pending === tile.role ? "Signing in…" : tile.label}
            description={tile.description}
            icon={tile.icon}
            disabled={pending !== null}
            onClick={() => enter(tile.role)}
          />
        ))}
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
