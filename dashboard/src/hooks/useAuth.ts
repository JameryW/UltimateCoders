import { useState, useEffect, useCallback } from "react";
import { createClient, ConnectError, Code } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import { create } from "@bufbuild/protobuf";
import { EngineService, HealthRequestSchema } from "@/grpc/engine_pb";

// ponytail: auth validation needs a transport that does NOT auto-attach the
// localStorage token. The shared transport in useGrpcWeb.ts has an
// authInterceptor that reads localStorage and overwrites the `authorization`
// header — which would defeat validateToken's per-call candidate token (e.g.
// at login, when a stale token is still in localStorage and the user types a
// new password). This dedicated transport has no interceptors, so the per-call
// `authorization` header set by validateToken is the sole source of truth.
const GRPC_WEB_ADDR = import.meta.env.VITE_GRPC_WEB_ADDR ?? "";
let _authTransport: ReturnType<typeof createGrpcWebTransport> | null = null;
function getAuthTransport() {
  if (!_authTransport) {
    _authTransport = createGrpcWebTransport({ baseUrl: GRPC_WEB_ADDR });
  }
  return _authTransport;
}

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

/** Read the stored token from localStorage (returns null if absent).
 * ponytail: F69 — exported so SSE/trend/alerts fetches can authenticate
 * (the backend gates all /dashboard/api/* for non-localhost clients). */
export function getStoredToken(): string | null {
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
 * Validate a token by calling gRPC Health with Bearer auth via the Connect
 * client (correct gRPC-Web framing + protobuf, unlike bare fetch which sent
 * JSON to a protobuf endpoint and misread 415s as "auth ok").
 *
 * - success (200) → true (server accepted the token, or no auth gate configured)
 * - Unauthenticated / PermissionDenied → false (auth gate rejected the token)
 * - any other error (network, unavailable) → THROWN (F73: callers distinguish
 *   "wrong password" from "server unreachable"; swallowing both made the
 *   connection-error screen unreachable and reported network failures as
 *   "Invalid password")
 */
async function validateToken(token: string): Promise<boolean> {
  try {
    // Dedicated token-free transport: the per-call `authorization` header below
    // is the sole source of truth (the shared transport's authInterceptor would
    // overwrite it with the localStorage token, breaking the login flow when a
    // stale token is still stored).
    const client = createClient(EngineService, getAuthTransport());
    const req = create(HealthRequestSchema, {});
    await client.health(req, {
      headers: { authorization: `Bearer ${token}` },
    });
    return true;
  } catch (err: unknown) {
    if (err instanceof ConnectError) {
      if (err.code === Code.Unauthenticated || err.code === Code.PermissionDenied) {
        return false;
      }
    }
    // Transport-level failure — propagate so callers can surface
    // "connection error" instead of "invalid password".
    throw err;
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

        // No token or token invalid — try unauthenticated Health.
        // Server with no auth gate (UC_DASHBOARD_TOKEN unset) → 200 → authed.
        // Server with auth gate → Unauthenticated → show login.
        const valid = await validateToken("");
        if (valid) {
          setConnectionError(false);
          setIsAuthenticated(true);
        } else {
          setConnectionError(false);
          setIsAuthenticated(false);
        }
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

    // ponytail: F73 — a thrown validateToken is a TRANSPORT failure (server
    // unreachable), not a wrong password: surface the connection-error state
    // instead of "Invalid password". A returned false is a real auth
    // rejection (Unauthenticated/PermissionDenied).
    let valid: boolean;
    try {
      valid = await validateToken(password);
    } catch {
      setConnectionError(true);
      return false;
    }
    if (valid) {
      setStoredToken(password);
      setToken(password);
      setIsAuthenticated(true);
      return true;
    }

    setLoginError("Invalid password");
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
