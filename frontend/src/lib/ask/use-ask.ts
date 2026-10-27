"use client";

import { useMutation } from "@tanstack/react-query";
import { askService } from "./index";
import type { AskInput } from "./ask-service";
import { AskServiceError, UnauthenticatedError } from "./ask-service";
import { useAuth } from "@/lib/auth/auth-context";

// ASK_ENDPOINT_CONTRACT.md §3.6.
const RETRY_DELAYS_MS = [250, 750];

export function useAsk() {
  const { logout } = useAuth();

  return useMutation({
    mutationFn: (input: AskInput) => askService.ask(input),
    retry: (failureCount, error) => {
      // UnauthenticatedError / InvalidRequestError: no retry (handled by the
      // final `return false` below since neither is an AskServiceError with
      // a retryable status).
      if (error instanceof AskServiceError) {
        if (error.status === 429) return false; // rate_limited: retrying would only extend the wait
        return failureCount < 2; // network / 500 / 503
      }
      return false;
    },
    retryDelay: (failureCount) => RETRY_DELAYS_MS[failureCount - 1] ?? 750,
    onError: (error) => {
      // Role Authority Rule / auth boundary: an expired or invalid token
      // means the session is no longer valid -- the auth context (not this
      // hook's caller) owns clearing it (§3.5).
      if (error instanceof UnauthenticatedError) {
        logout();
      }
    },
  });
}
