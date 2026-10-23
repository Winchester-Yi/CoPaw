export interface StopAppendMetadata {
  message_id: string;
  order?: number;
  turn_id?: string;
}

export function readStopAppendMetadata(
  message: unknown,
): StopAppendMetadata | null {
  if (!message || typeof message !== "object") return null;
  const metadata = (message as { metadata?: unknown }).metadata;
  if (!metadata || typeof metadata !== "object") return null;
  const record = metadata as Record<string, unknown>;
  const nested = record.metadata;
  const marker =
    record.stop_append ??
    (nested && typeof nested === "object"
      ? (nested as Record<string, unknown>).stop_append
      : undefined);
  if (!marker || typeof marker !== "object") return null;
  const messageId = (marker as Record<string, unknown>).message_id;
  return typeof messageId === "string" && messageId.trim()
    ? (marker as StopAppendMetadata)
    : null;
}

export function stopAppendBubbleId(marker: StopAppendMetadata): string {
  return `stop-append-${marker.message_id}`;
}
