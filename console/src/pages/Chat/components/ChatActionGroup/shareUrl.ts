export function buildChatShareUrl(
  sharePath: string,
  location: Pick<Location, "origin" | "pathname"> = window.location,
  source?: string | null,
): string {
  const base = location.pathname.match(/^\/console(?=\/|$)/)?.[0] || "";
  const path = sharePath.startsWith("/") ? sharePath : `/${sharePath}`;
  const publicPath = path.startsWith(`${base}/`) || path === base
    ? path
    : `${base}${path}`;
  const url = new URL(`${location.origin}${publicPath}`);
  if (source) url.searchParams.set("source", source);
  return url.href;
}
