/**
 * snapAi.ts
 *
 * 「随手改」编辑器 AI 调试接口封装。
 *
 * 复用系统已有的 skill AI 优化流式通道（/skills/ai/optimize/stream），
 * 携带父级注入的认证上下文头（buildAuthHeaders），让 AI 按用户自然语言描述
 * 对选中的报告代码块进行改写，并流式返回修改后的块 HTML。
 */


declare const VITE_API_BASE_URL: string;


import { buildAuthHeaders } from "@/api/authHeaders";


/** AI 流式调试回调 */
export interface SnapAiChunkHandlers {
  /** 流式片段（AI 每产出一段文本即回调一次，可做打字机效果） */
  onChunk?: (text: string) => void;
  /** 流结束回调整行 */
  onDone?: (fullText: string) => void;
  /** 出错回调 */
  onError?: (err: Error) => void;
}


/** 从单行 SSE 数据中抽取 text 字段（尽力而为，失败返回空串） */
function extractText(line: string): string {
  const trimmed = line.trim();
  if (!trimmed.startsWith("data: ")) return "";
  try {
    const parsed = JSON.parse(trimmed.slice(6));
    return typeof parsed.text === "string" ? parsed.text : "";
  } catch {
    return "";
  }
}


/** 解析单行 SSE 数据，触发相应回调。返回该行是否有有效 data。 */
function consumeChunk(
  line: string,
  onChunk: (text: string) => void,
  onError: (err: Error) => void,
): boolean {
  const trimmed = line.trim();
  if (!trimmed.startsWith("data: ")) return false;
  try {
    const parsed = JSON.parse(trimmed.slice(6));
    if (typeof parsed.text === "string") {
      onChunk(parsed.text);
      return true;
    }
    if (parsed.error) {
      onError(new Error(String(parsed.error)));
      return true;
    }
    return true; // 其余字段（如 done）视为有效 data，但无需额外操作
  } catch {
    return true;
  }
}


/**
 * 调用后端 AI 对指定代码块进行改写（流式）。
 *
 * @param blockHtml - 选中的报告代码块 HTML
 * @param prompt - 用户自然语言描述（要改成什么样）
 * @param handlers - 流式回调
 */
export async function streamOptimizeBlockHtml(
  blockHtml: string,
  prompt: string,
  handlers: SnapAiChunkHandlers,
): Promise<void> {
  const { onChunk = () => { }, onDone = () => { }, onError = () => { } } =
    handlers;


  // 拼装给 AI 的内容：让 AI 只输出修改后的块 HTML。
  const content = [
    "请根据下面的修改要求，返回修改后的 HTML 代码块。",
    "",
    "【修改要求】",
    prompt,
    "",
    "【原始 HTML 代码块】",
    blockHtml,
    "",
    "【约束】",
    "1. 只返回修改后的代码块 HTML，不要添加 markdown 代码块标记、不要输出任何解释。",
    "2. 保持与原块相同的标签结构和语义，仅在满足要求的前提下调整内容、样式与结构。",
    "3. 必须是合法、可直接内嵌的 HTML 片段。",
  ].join("\n");


  const apiUrl = "/api/skills/ai/optimize/stream";
  const base = typeof VITE_API_BASE_URL === "string" ? VITE_API_BASE_URL : "";
  const url = `${base}${apiUrl}`;


  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...buildAuthHeaders(),
      },
      body: JSON.stringify({ content, language: "zh" }),
    });


    if (!response.ok) {
      throw new Error(`AI 调试请求失败（HTTP ${response.status}）`);
    }


    const reader = response.body?.getReader();
    if (!reader) {
      throw new Error("无法读取 AI 响应流");
    }


    const decoder = new TextDecoder();
    let buffer = "";
    let full = "";


    while (true) {
      const { done, value } = await reader.read();
      if (done) break;


      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");


      // 逐行消费（保留最后一行作为缓冲，等待后续分片）
      for (let i = 0; i < lines.length - 1; i++) {
        const line = lines[i];
        if (line.trim().startsWith("data: ")) {
          full += extractText(line);
          consumeChunk(line, onChunk, onError);
        }
      }
      buffer = lines[lines.length - 1];
    }


    // 处理尾部剩余缓冲
    if (buffer.trim().startsWith("data: ")) {
      full += extractText(buffer);
      consumeChunk(buffer, onChunk, onError);
    }


    onDone(full);
  } catch (err) {
    onError(err instanceof Error ? err : new Error(String(err)));
  }
}


/** 从 AI 返回内容中提取可内嵌的 HTML 片段（去除 markdown 围栏等噪声） */
export function normalizeAiHtml(raw: string): string {
  let out = raw || "";
  // 去掉可能的 ```html 围栏
  out = out.replace(/^\s*```(?:html)?\s*/i, "");
  out = out.replace(/```\s*$/, "");
  return out.trim();
}