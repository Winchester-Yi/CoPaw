declare const VITE_API_BASE_URL: string;
declare const TOKEN: string;

const AUTH_TOKEN_KEY = "copaw_auth_token";

// ==================== 运行时配置 (Kun He) ====================
/**
 * 从window读取运行时配置
 * @returns 运行时配置对象
 */
function getRuntimeConfig(): { baseUrl?: string } {
  if (typeof window !== "undefined" && window.__env__?.baseUrl !== undefined) {
    return window.__env__;
  }
  return {};
}
// ==================== 运行时配置结束 ====================
/**
 * Get the full API URL with /api prefix
 * 优先级：运行时配置 > 构建时配置 > 相对路径
 * @param path - API path (e.g., "/models", "/skills")
 * @returns Full API URL (e.g., "http://localhost:8088/api/models" or "/api/models")
 */
export function getApiUrl(path: string): string {
  // ==================== 运行时配置 (Kun He) ====================
  const runtimeConfig = getRuntimeConfig();
  const base = runtimeConfig.baseUrl || VITE_API_BASE_URL || "";
  // ==================== 运行时配置结束 ====================
  const apiPrefix = "/api";
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${apiPrefix}${normalizedPath}`;
}

/**
 * Get the API token - checks localStorage first (auth login),
 * then falls back to the build-time TOKEN constant.
 * @returns API token string or empty string
 */
export function getApiToken(): string {
  const stored = localStorage.getItem(AUTH_TOKEN_KEY);
  if (stored) return stored;
  return typeof TOKEN !== "undefined" ? TOKEN : "";
}

/**
 * Store the auth token in localStorage after login.
 */
export function setAuthToken(token: string): void {
  localStorage.setItem(AUTH_TOKEN_KEY, token);
  window.dispatchEvent(new Event("swe:auth-ready"));
}

/**
 * Remove the auth token from localStorage (logout / 401).
 */
export function clearAuthToken(): void {
  localStorage.removeItem(AUTH_TOKEN_KEY);
  window.dispatchEvent(new Event("swe:auth-cleared"));
}
