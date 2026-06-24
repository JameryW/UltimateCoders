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
 * Auth hook — validates access via gRPC-Web health check.
 *
 * Flow:
 * 1. On mount, try gRPC-Web EngineService.Health.
 *    - reachable → authenticated (no auth gate when only gRPC is running)
 * 2. `login(password)` stores token for future use (e.g. Bearer header).
 * 3. Token is persisted in localStorage so it survives page reload.
 *
 * TODO(security): When the Python backend enforces DASHBOARD_PASSWORD,
 * login() should validate the password by calling Health with the
 * Bearer token and only set isAuthenticated on success.
 */
export function useAuth(): AuthState {
  const [token, setToken] = useState<string | null>(getStoredToken);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isChecking, setIsChecking] = useState(true);
  const [connectionError, setConnectionError] = useState(false);

  // Validate gRPC-Web reachability on mount
  useEffect(() => {
    const checkAuth = async () => {
      try {
        // ponytail: raw gRPC-Web health check — any response means server is up
        const res = await fetch("/ultimate_coders.EngineService/Health", {
          method: "POST",
          headers: { "content-type": "application/grpc-web+proto" },
        });
        // Any response (even gRPC error) means the gRPC server is reachable
        if (res.status !== 0) {
          setConnectionError(false);
          setIsAuthenticated(true);
          return;
        }
      } catch {
        // gRPC-Web unreachable
      }

      setConnectionError(true);
      setIsAuthenticated(false);
    };

    checkAuth().finally(() => setIsChecking(false));
  }, []);

  const login = useCallback(async (password: string): Promise<boolean> => {
    // ponytail: store token for Bearer header; clear connection error on attempt
    setConnectionError(false);
    setStoredToken(password);
    setToken(password);
    setIsAuthenticated(true);
    return true;
  }, []);

  const logout = useCallback(() => {
    setStoredToken(null);
    setToken(null);
    setIsAuthenticated(false);
  }, []);

  return { isAuthenticated, isChecking, connectionError, token, login, logout };
}
