// External system token management module
// Handles token fetch, refresh, and storage for external authentication

// Token storage keys (isolated by domain via localStorage)
const EXTERNAL_TOKEN_KEY = "claw_external_token";
const EXTERNAL_TOKEN_EXPIRES_KEY = "claw_external_token_expires";

// Refresh margin: refresh token 60 seconds before expiry
const REFRESH_MARGIN_SECONDS = 600;

// Prevent concurrent refresh
let isRefreshing = false;
let refreshPromise: Promise<string> | null = null;

/**
 * Runtime configuration from window.__env__
 * Different domains inject different values at deployment
 */
interface TokenConfig {
  systemCode: string;
  systemSecret: string;
}

interface WindowEnv {
  baseUrl?: string;
  SYSTEM_CODE?: string;
  SYSTEM_SECRET?: string;
  responseFeedbackUserWhitelist?: string[];
}

function getTokenConfig(): TokenConfig {
  return {
    systemCode: window.__env__.systemCode || "",
    systemSecret: window.__env__.systemSect || "",
  };
}

/**
 * Check if external token feature is enabled
 */
export function isExternalTokenEnabled(): boolean {
  const config = getTokenConfig();
  return !!config.systemCode && !!config.systemSecret;
}

/**
 * Fetch new token from external API
 * POST {tokenApiUrl} with {"systemcode": xxx, "systemSect": xxx}
 * Response: {"accesstoken": xxx, "expirestime": xxx}
 */
export async function fetchNewToken(): Promise<{ token: string; expiresIn: number }> {
  const config = getTokenConfig();
  const response = await fetch("/gateway-admin/api/auth/system/token", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      systemCode: config.systemCode,
      systemSecret: config.systemSecret,
      grantType: "issue"
    }),
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch token: ${response.status}`);
  }

  const data = await response.json();

  const token = data?.data.accessToken;
  const expiresIn = data?.data.expiresIn;

  if (!token) {
    throw new Error("Token not found in response");
  }
  return { token, expiresIn };
}

/**
 * Refresh token using old token
 * POST {tokenApiUrl} with {"token": oldToken}
 * Response: {"accesstoken": xxx, "expirestime": xxx}
 */
export async function refreshToken(oldToken: string): Promise<{ token: string; expiresIn: number }> {
  const config = getTokenConfig();
  const response = await fetch("/gateway-admin/api/auth/system/token", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      oldToken: oldToken,
      systemCode: config.systemCode,
      systemSecret: config.systemSecret,
      grantType: "renew"
    }),
  });

  if (!response.ok) {
    // If refresh fails (e.g., 401), fetch new token instead
    if (response.status === 401) {
      return fetchNewToken();
    }
    throw new Error(`Failed to refresh token: ${response.status}`);
  }

  const data = await response.json();
  const token = data?.data.accessToken;
  const expiresIn = data?.data.expiresIn;

  if (!token) {
    throw new Error("Token not found in response");
  }

  return { token, expiresIn };
}

/**
 * Get external token from localStorage (check expiry)
 */
export function getExternalToken(): string | null {
  const expiresAt = localStorage.getItem(EXTERNAL_TOKEN_EXPIRES_KEY);
  if (expiresAt) {
    const expiresTime = parseInt(expiresAt, 10);
    if (Date.now() >= expiresTime) {
      // Token expired, clear it
      clearExternalToken();
      return null;
    }
  }
  return localStorage.getItem(EXTERNAL_TOKEN_KEY) || null;
}

/**
 * Set external token to localStorage
 * Store with提前 60 秒过期 to trigger early refresh
 */
export function setExternalToken(token: string, expiresIn: number): void {
  const effectiveExpiresIn = Math.max(1, expiresIn - REFRESH_MARGIN_SECONDS);
  const expiresAt = Date.now() + effectiveExpiresIn * 1000;
  localStorage.setItem(EXTERNAL_TOKEN_KEY, token);
  localStorage.setItem(EXTERNAL_TOKEN_EXPIRES_KEY, expiresAt.toString());
  window.dispatchEvent(new Event("swe:auth-ready"));
}

/**
 * Clear external token from localStorage
 */
export function clearExternalToken(): void {
  localStorage.removeItem(EXTERNAL_TOKEN_KEY);
  localStorage.removeItem(EXTERNAL_TOKEN_EXPIRES_KEY);
  window.dispatchEvent(new Event("swe:auth-cleared"));
}

/**
 * Check if current token is expired or about to expire
 */
export function isTokenExpired(): boolean {
  const expiresAt = localStorage.getItem(EXTERNAL_TOKEN_EXPIRES_KEY);
  if (!expiresAt) {
    return true;
  }
  return Date.now() >= parseInt(expiresAt, 10);
}

/**
 * Ensure valid token is available
 * If forceRefresh=true, always fetch new token (for 401 recovery)
 * Handles concurrent refresh by sharing promise
 */
export async function ensureValidToken(forceRefresh: boolean = false): Promise<string> {
  // If already refreshing, wait for existing promise
  if (isRefreshing && refreshPromise) {
    return refreshPromise;
  }

  // Check if we have a valid token
  const currentToken = getExternalToken();
  if (!forceRefresh && currentToken && !isTokenExpired()) {
    return currentToken;
  }

  // Start refresh
  isRefreshing = true;
  refreshPromise = (async () => {
    try {
      let result: { token: string; expiresIn: number };

      if (currentToken && !forceRefresh) {
        // Try refresh with old token
        result = await refreshToken(currentToken);
      } else {
        // Fetch new token
        result = await fetchNewToken();
      }

      setExternalToken(result.token, result.expiresIn);
      return result.token;
    } catch (error) {
      // Clear token on error
      clearExternalToken();
      throw error;
    } finally {
      isRefreshing = false;
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

/**
 * Get token config for external use (e.g., in hooks)
 */
export { getTokenConfig };
