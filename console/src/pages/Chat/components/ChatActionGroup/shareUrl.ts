export function buildChatShareUrl(
  sharePath: string,
  location: Pick<Location, "origin" | "pathname"> = window.location,
): string {
  const base = location.pathname.match(/^\/console(?=\/|$)/)?.[0] || "";
  const path = sharePath.startsWith("/") ? sharePath : `/${sharePath}`;
  const publicPath = path.startsWith(`${base}/`) || path === base
    ? path
    : `${base}${path}`;
  return `${location.origin}${publicPath}`;
}
