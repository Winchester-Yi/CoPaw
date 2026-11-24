import { useEffect, useRef, useState } from "react";
import { Button, Spin } from "antd";
import { request } from "../../api/request";
import styles from "./evidence.module.less";

type Evidence = {
  evidence_ref: string;
  content_type?: string;
  status: string;
  message?: unknown;
  message_chunk?: string;
  chunk_offset?: number;
  next_cursor?: string | null;
  has_more?: boolean;
};
type Entry = { item: Evidence; bytes: Uint8Array };
type Result = { identity: string; entries: Entry[] };

const labels: Record<string, string> = {
  available: "可用",
  pending: "待写入",
  not_saved: "未留存",
  compacted: "已压缩",
  unavailable: "不可用",
  unresolved: "无法解析",
  truncated: "正文不完整",
};

function chunkBytes(chunk?: string): Uint8Array {
  if (!chunk) return new Uint8Array();
  const base64 = chunk.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, "="));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function content(entry: Entry): string | null {
  if (entry.bytes.length) {
    return new TextDecoder().decode(entry.bytes, {
      stream: !!entry.item.has_more,
    });
  }
  return entry.item.message === undefined
    ? null
    : JSON.stringify(entry.item.message, null, 2);
}

/** Mount or activate only when the evidence tab is selected. sourceKey tracks the current authorization context. */
export default function SpanEvidence({
  runId,
  spanId,
  sourceKey,
  active,
}: {
  runId: string;
  spanId: string;
  sourceKey: string;
  active: boolean;
}) {
  const identity = JSON.stringify([runId, spanId, sourceKey]);
  const [result, setResult] = useState<Result | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const generation = useRef(0);
  const base = `/agent-observability/runs/${encodeURIComponent(
    runId,
  )}/spans/${encodeURIComponent(spanId)}/evidence`;

  useEffect(() => {
    const currentGeneration = generation;
    const version = ++currentGeneration.current;
    setResult(null);
    setError(null);
    setLoading(null);
    if (!active) return;
    setLoading("initial");
    request<{ items: Evidence[] }>(base)
      .then(({ items }) => {
        if (generation.current === version)
          setResult({
            identity,
            entries: items.map((item) => ({
              item,
              bytes: chunkBytes(item.message_chunk),
            })),
          });
      })
      .catch(() => {
        if (generation.current === version)
          setError("正文证据加载失败，请重试");
      })
      .finally(() => {
        if (generation.current === version) setLoading(null);
      });
    return () => {
      if (currentGeneration.current === version) currentGeneration.current++;
    };
  }, [active, base, identity, retry]);

  const more = async (entry: Entry) => {
    const cursor = entry.item.next_cursor;
    if (!cursor || loading) return;
    const version = generation.current;
    setLoading(entry.item.evidence_ref);
    setError(null);
    try {
      const page = await request<{ items: Evidence[] }>(
        `${base}?cursor=${encodeURIComponent(cursor)}`,
      );
      const item = page.items.find(
        (value) => value.evidence_ref === entry.item.evidence_ref,
      );
      if (!item) throw new Error("Invalid evidence continuation");
      if (item.message_chunk && item.chunk_offset !== entry.bytes.length)
        throw new Error("Invalid evidence continuation");
      const bytes = chunkBytes(item.message_chunk);
      if (generation.current === version)
        setResult((old) =>
          old && old.identity === identity
            ? {
                ...old,
                entries: old.entries.map((value) =>
                  value.item.evidence_ref === item.evidence_ref
                    ? {
                        item,
                        bytes: item.message_chunk
                          ? Uint8Array.from([...value.bytes, ...bytes])
                          : new Uint8Array(),
                      }
                    : value,
                ),
              }
            : old,
        );
    } catch (err) {
      if (generation.current === version) {
        if ((err as { status?: number })?.status === 409) {
          setResult(null);
          setError("正文已发生变化，请重新读取");
        } else setError("正文续读失败，请重试");
      }
    } finally {
      if (generation.current === version) setLoading(null);
    }
  };

  if (!active) return null;
  const entries = result?.identity === identity ? result.entries : null;
  return (
    <section aria-label="正文证据" className={styles.evidence}>
      {loading === "initial" && (
        <p>
          <Spin size="small" /> 正在加载正文证据
        </p>
      )}
      {error && (
        <p role="alert">
          {error}{" "}
          {entries === null && (
            <Button size="small" onClick={() => setRetry((value) => value + 1)}>
              {error === "正文已发生变化，请重新读取" ? "重新读取" : "重试"}
            </Button>
          )}
        </p>
      )}
      {entries && entries.length === 0 && <p>暂无正文证据</p>}
      {entries?.map((entry) => (
        <div className={styles.entry} key={entry.item.evidence_ref}>
          <p>
            {entry.item.content_type || "正文"} ·{" "}
            {labels[entry.item.status] || "状态未知"}
          </p>
          {content(entry) !== null && <pre>{content(entry)}</pre>}
          {entry.item.next_cursor && (
            <Button
              size="small"
              disabled={!!loading}
              onClick={() => void more(entry)}
            >
              {loading === entry.item.evidence_ref ? "加载中" : "继续加载"}
            </Button>
          )}
        </div>
      ))}
    </section>
  );
}
