/**
 * Auth placeholder hook — no-op for now.
 * Future: JWT/OAuth integration.
 */

export interface AuthState {
  isAuthenticated: boolean;
  user: null; // future: { id, name, role }
  token: null; // future: string
}

const INITIAL_STATE: AuthState = {
  isAuthenticated: true, // no auth = always "authenticated"
  user: null,
  token: null,
};

export function useAuth(): AuthState {
  // No-op placeholder. When auth is implemented:
  // - Check localStorage/sessionStorage for token
  // - Validate token on mount
  // - Provide login/logout methods
  return INITIAL_STATE;
}
