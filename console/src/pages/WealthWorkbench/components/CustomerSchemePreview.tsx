/**
 * 智能财富工作台 —— 客户经营方案预览
 *
 * 客户名单（/wealth/name-list）的 filename 为经营方案的完整 URL（reportView 页面，
 * 与本项目 console/src/pages/ReportView 同源站点）。
 * 点击表格「查看经营方案」时，在弹窗中以 <iframe> 渲染该链接。
 *
 * 为何采用 iframe 而非直接挂载 ReportView 组件：
 * - filename 是携带 custUid/templateId/resultId/sapId/bbkOrgId 等参数的独立页面 URL，
 *   ReportView 依赖 useSearchParams 读取路由参数来圈定客户与模板，
 *   直接挂载在 /wealth/* 路由下无法提供这些参数，需要大面积改造 ReportView。
 * - iframe 加载部署后的独立 ReportView 实例，由它自行处理路由/参数/事件，
 *   与本项目 iframe 通置信令（USER_DATA）一致，也贴合 RM 组件库 CustPlanCard 的
 *   Modal + iframe 交互范式。
 *
 * 通信：iframe 加载完成后，通过 postMessage 发送 USER_DATA（sapId/bbkId/hideMenu/
 * source/pageSource/platformSource）给目标源，供 ReportView 侧 useIframeStore 接收上下文。
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Spin } from "antd";
import { useIframeStore } from "@/stores/iframeStore";
import styles from "../index.module.less";

/** 从完整 URL 提取 origin（协议 + 主机 + 端口），用于 postMessage 目标源校验 */
function originOf(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}

interface CustomerSchemePreviewProps {
  /** 客户经营方案 URL（/wealth/name-list 返回的 filename） */
  url: string;
  /** 客户名称，用于 iframe 标题与提示 */
  custName?: string;
  /** 营销场景名，用于 iframe 标题 */
  title?: string;
}

/** 经营方案 iframe 弹窗体：渲染给定 URL，加载完成后向子窗口下发用户上下文 */
export function CustomerSchemePreview({
  url,
  custName,
  title,
}: CustomerSchemePreviewProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const retryCountRef = useRef(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  // 当前登录人上下文（父系统经 postMessage 写入 useIframeStore）
  const source = useIframeStore((s) => s.source);
  const userId = useIframeStore((s) => s.userId);
  const bbk = useIframeStore((s) => s.bbk);
  const pageSource = useIframeStore((s) => s.pageSource);
  const platformSource = useIframeStore((s) => s.platformSource);
  const authHeaders = useIframeStore((s) => s.authHeaders);

  // reportView 独立站点源，用于限制 postMessage 目标
  const targetOrigin = useMemo(() => originOf(url), [url]);

  /** 向 iframe 子窗口下发用户数据（与 RM CustPlanCard postUserData 同契约） */
  const postUserData = useCallback(() => {
    const iframe = iframeRef.current;
    if (!iframe || !iframe.contentWindow) return;
    iframe.contentWindow.postMessage(
      {
        type: "USER_DATA",
        data: {
          ...authHeaders,
          sapId: userId ?? "",
          userId: userId ?? "",
          bbkId: bbk ?? "",
          bbkOrgId: bbk ?? "",
          hideMenu: true,
          source: source ?? "RMASSIST",
          pageSource: pageSource ?? "",
          platformSource: platformSource ?? "",
          skipPreviewTracking: true,
        },
      },
      targetOrigin,
    );
  }, [authHeaders, userId, bbk, source, pageSource, platformSource, targetOrigin]);

  /** iframe 加载完成：隐藏 loading 并下发用户上下文 */
  const handleLoad = useCallback(() => {
    retryCountRef.current = 0;
    setLoading(false);
    setLoadError(false);
    postUserData();
  }, [postUserData]);

  /** 全局消息监听：确认 ReportView 方收到上下文（调试辅助） */
  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      if (!targetOrigin || event.origin !== targetOrigin) return;
      if (event.data?.type === "DATA_RECEIVED") {
        console.debug("[Wealth][SchemePreview] ReportView 已确认收到数据");
      }
    };
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [targetOrigin]);

  const handleError = useCallback(() => {
    if (retryCountRef.current < 2) {
      retryCountRef.current += 1;
      const iframe = iframeRef.current;
      if (iframe) {
        const delay = 1000 * retryCountRef.current;
        window.setTimeout(() => {
          iframe.src = iframe.src;
        }, delay);
      }
      return;
    }
    setLoading(false);
    setLoadError(true);
  }, []);

  return (
    <div className={styles.schemePreviewWrap}>
      <Spin spinning={loading}>
        <div className={styles.schemePreviewFrame}>
          {loadError ? (
            <div className={styles.schemePreviewEmpty}>
              经营方案加载失败，请关闭后重试
              {custName ? `（客户：${custName}）` : ""}
            </div>
          ) : (
            <iframe
              ref={iframeRef}
              src={url}
              title={title || `客户经营方案${custName ? `-${custName}` : ""}`}
              onLoad={handleLoad}
              onError={handleError}
            />
          )}
        </div>
      </Spin>
    </div>
  );
}