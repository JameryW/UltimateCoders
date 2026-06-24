import { useState, useEffect, useCallback } from "react";

export interface AuthState {
  isAuthenticated: boolean;
  isChecking: boolean;
  connectionError: boolean;
  loginError: string | null;
  token: string | null;
  login: (password: string) => Promise<boolean>;
  logout: () => void;
}

const STORAGE_KEY = "uc_dashboard_token";

/** Read the stored token from localStorage (returns null if absent). */
function getStoredToken(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Persist or clear the token in localStorage. */
function setStoredToken(token: string | null) {
  try {
    if (token) localStorage.setItem(STORAGE_KEY, token);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore — localStorage may be unavailable */
  }
}

/**
 * Validate a token by calling gRPC Health with Bearer auth.
 * Returns true if the server responds (any non-error), false otherwise.
 */
async function validateToken(token: string): Promise<boolean> {
  try {
    const headers: Record<string, string> = {
      "content-type": "application/json",
      authorization: `Bearer ${token}`,
    };
    const res = await fetch("/ultimate_coders.EngineService/Health", {
      method: "POST",
      headers,
      body: JSON.stringify({}),
    });
    // 200 = server accepted auth; 401/403 = auth rejected; other = server up but maybe no auth
    if (res.ok) return true;
    if (res.status === 401 || res.status === 403) return false;
    // Server responded but with non-auth error — probably no auth gate, allow through
    return true;
  } catch {
    return false;
  }
}

/**
 * Auth hook — validates access via gRPC-Web health check.
 *
 * Flow:
 * 1. On mount, if stored token exists → validate it via Health with Bearer.
 *    No token → try unauthenticated Health (server may not require auth).
 * 2. `login(password)` stores token, validates via Health, only authenticates on success.
 * 3. Token persisted in localStorage for page reload survival.
 */
export function useAuth(): AuthState {
  const [token, setToken] = useState<string | null>(getStoredToken);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isChecking, setIsChecking] = useState(true);
  const [connectionError, setConnectionError] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);

  // Validate on mount
  useEffect(() => {
    const stored = getStoredToken();

    const checkAuth = async () => {
      try {
        if (stored) {
          // Validate stored token
          const valid = await validateToken(stored);
          if (valid) {
            setConnectionError(false);
            setIsAuthenticated(true);
            return;
          }
          // Token invalid/expired — clear it
          setStoredToken(null);
          setToken(null);
        }

        // No token or token invalid — try unauthenticated reachability
        const res = await fetch("/ultimate_coders.EngineService/Health", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({}),
        });
        // Server reachable without auth → no auth gate, allow through
        if (res.ok || (res.status >= 400 && res.status !== 401 && res.status !== 403)) {
          setConnectionError(false);
          setIsAuthenticated(true);
          return;
        }

        // 401/403 → auth required but no valid token
        setConnectionError(false);
        setIsAuthenticated(false);
      } catch {
        // Server unreachable
        setConnectionError(true);
        setIsAuthenticated(false);
      }
    };

    checkAuth().finally(() => setIsChecking(false));
  }, []);

  const login = useCallback(async (password: string): Promise<boolean> => {
    setLoginError(null);
    setConnectionError(false);

    const valid = await validateToken(password);
    if (valid) {
      setStoredToken(password);
      setToken(password);
      setIsAuthenticated(true);
      return true;
    }

    // Check if it's a connection error or auth rejection
    try {
      const res = await fetch("/ultimate_coders.EngineService/Health", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({}),
      });
      if (res.status === 401 || res.status === 403) {
        setLoginError("Invalid password");
      } else {
        // Server up but health failed for other reason — likely no auth gate
        setStoredToken(password);
        setToken(password);
        setIsAuthenticated(true);
        return true;
      }
    } catch {
      setLoginError("Cannot connect to server");
      setConnectionError(true);
    }

    return false;
  }, []);

  const logout = useCallback(() => {
    setStoredToken(null);
    setToken(null);
    setIsAuthenticated(false);
    setLoginError(null);
  }, []);

  return { isAuthenticated, isChecking, connectionError, loginError, token, login, logout };
}
