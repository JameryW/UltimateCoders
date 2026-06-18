import { useState, useEffect, useCallback } from "react";

export interface AuthState {
  isAuthenticated: boolean;
  isChecking: boolean;
  connectionError: boolean;
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
 * Auth hook — validates access against the backend auth gate.
 *
 * Flow:
 * 1. On mount, try fetching `/dashboard/api/health` with a stored token.
 *    - 200 → authenticated
 *    - 401 → not authenticated (show login modal)
 * 2. `login(password)` validates against the same health endpoint
 *    using `Authorization: Bearer <password>`.
 * 3. Token is persisted in localStorage so it survives page reload.
 */
export function useAuth(): AuthState {
  const [token, setToken] = useState<string | null>(getStoredToken);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isChecking, setIsChecking] = useState(true);
  const [connectionError, setConnectionError] = useState(false);

  // Validate current token on mount
  useEffect(() => {
    const stored = getStoredToken();
    if (!stored) {
      // No token yet — try an unauthenticated request.
      // If the server has no DASHBOARD_PASSWORD, it will succeed.
      fetch("/dashboard/api/health")
        .then((res) => {
          if (res.ok) {
            setIsAuthenticated(true);
          } else if (res.status === 401) {
            setIsAuthenticated(false);
          } else {
            // Other errors (503, etc.) — assume not auth-related
            setIsAuthenticated(true);
          }
        })
        .catch(() => {
          // Network error — can't reach server, show connection error
          setConnectionError(true);
        })
        .finally(() => setIsChecking(false));
    } else {
      // We have a stored token — validate it
      fetch("/dashboard/api/health", {
        headers: { Authorization: `Bearer ${stored}` },
      })
        .then((res) => {
          if (res.ok) {
            setToken(stored);
            setIsAuthenticated(true);
          } else {
            // Token is invalid — clear it
            setStoredToken(null);
            setToken(null);
            setIsAuthenticated(false);
          }
        })
        .catch(() => {
          // Network error — can't reach server, show connection error
          setConnectionError(true);
        })
        .finally(() => setIsChecking(false));
    }
  }, []);

  const login = useCallback(async (password: string): Promise<boolean> => {
    try {
      const res = await fetch("/dashboard/api/health", {
        headers: { Authorization: `Bearer ${password}` },
      });
      if (res.ok) {
        setStoredToken(password);
        setToken(password);
        setIsAuthenticated(true);
        return true;
      }
      // 401 or other error
      return false;
    } catch {
      return false;
    }
  }, []);

  const logout = useCallback(() => {
    setStoredToken(null);
    setToken(null);
    setIsAuthenticated(false);
  }, []);

  return { isAuthenticated, isChecking, connectionError, token, login, logout };
}
