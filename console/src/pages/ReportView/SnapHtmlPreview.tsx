import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useSearchParams } from "react-router-dom";
import { Spin, Card, Tabs } from "antd";
import { SparkDownloadLine } from "@agentscope-ai/icons";
import { IconButton } from "@agentscope-ai/design";
import { htmlPreviewEventsApi } from "@/api/modules/htmlPreviewEvents";
import type { HtmlPreviewTemplateType } from "@/api/types/htmlPreviewEvents";
import { useHtmlPreviewTracking } from "@/components/agentscope-chat/HtmlPreviewTrackingContext";
import { useDynamicRender } from "@/components/agentscope-chat/DynamicRenderContext";
import { useIframeHtmlPreviewTracking } from "@/components/agentscope-chat/FilePreviewModal/useHtmlPreviewTracking";
import type { NestedHtmlPreviewRequest } from "@/components/agentscope-chat/FilePreviewModal/htmlPreviewClickTracking";
import FilePreviewModal from "@/components/agentscope-chat/FilePreviewModal";
import styles from "./index.module.less";
import SnapHtmlPreview from "./SnapHtmlPreview";


import { useIframeStore } from "@/stores/iframeStore";
import {
  sendReportMessageToParent,
  registerReportRequestHandler,
  unregisterReportRequestHandler,
} from "@/utils/iframeMessage";
import { useReportViewData } from "./hooks/useReportViewData";
import { cronJobApi } from "@/api/modules/cronjob";
import {
  dynamicRenderApi,
  type ClawFilePlanItem,
  type RecordDataResponse,
} from "@/api/modules/dynamicRender";
import { FilePreviewPresentationProvider } from "@/components/agentscope-chat/FilePreviewPresentationContext";


// 仅声明现有调用参数；此组件尚未使用这些参数或转发 ref。
interface SnapHtmlPreviewProps {
  ref?: React.Ref<HTMLIFrameElement>;
  docHtml: string;
  title: string;
  onLoad: () => void;
  rawRecordData: RecordDataResponse | null;
  resultId: string | null;
}

function ReportViewPage(_props: SnapHtmlPreviewProps) {
  const [searchParams] = useSearchParams();


  const urlFileName = searchParams.get("fileName") || "";
  const templateId = searchParams.get("templateId");
  const resultId = searchParams.get("resultId");
  // 客户UID：若存在该参数，表明当前页面展示客户的所有个人报告模板，需调用接口查询报告列表，并默认展示与templateId对应的模板，若templateId不存在则默认展示第一个模板
  const custUid = searchParams.get("custUid") || "";
  const custName = searchParams.get("custName") || "";
  const idKey = searchParams.get("idKey");
  const sapId = searchParams.get("sapId");
  const bbkOrgId = searchParams.get("bbkOrgId");
  const actvId = searchParams.get("actvId") || "";
  const actvTyp = searchParams.get("actvTyp") || "";
  const pageSourceUrl = searchParams.get("pageSource");
  const platformSourceUrl = searchParams.get("platformSource");
  const headBbk = searchParams.get("headBbk");


  const iframeState = useIframeStore((state) => state);
  const {
    source,
    userId,
    bbk,
    pageSource = pageSourceUrl,
    platformSource = platformSourceUrl,
  } = iframeState;


  const [nestedPreview, setNestedPreview] =
    useState<NestedHtmlPreviewRequest | null>(null);


  // 客户多模板状态
  const [clawFilePlanList, setClawFilePlanList] = useState<ClawFilePlanItem[]>(
    []
  );
  const [activeTemplate, setActiveTemplate] = useState<ClawFilePlanItem | null>(
    null
  );
  const [clawPlanLoading, setClawPlanLoading] = useState(false);
  const [clawPlanFailed, setClawPlanFailed] = useState(false);


  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const personEventsFlagRef = useRef<boolean>(false);
  const trackingContext = useHtmlPreviewTracking();
  const { templateList, isTemplateListLoaded } = useDynamicRender();


  // 计算有效的 templateId 和 resultId（当 custUid 存在时使用 activeTemplate 的值）
  // 若 clawPlanFailed 为 true（接口失败或返回空），则回退到原始 searchParams 中的 templateId/resultId
  const effectiveTemplateId = custUid
    ? activeTemplate?.templateId ?? (clawPlanFailed ? templateId : null)
    : templateId;
  const effectiveResultId = custUid
    ? activeTemplate?.resultId ?? (clawPlanFailed ? resultId : null)
    : resultId;


  // 是否展示空状态：custUid 存在且接口失败/返回空，且 searchParams 中没有 templateId 和 resultId
  const showClawPlanEmpty =
    custUid && clawPlanFailed && !templateId && !resultId;


  const {
    phase,
    error,
    renderedHtmlContent,
    rawRecordData,
    resolvedFileName,
    retry,
  } = useReportViewData({
    resultId: effectiveResultId,
    templateId: String(effectiveTemplateId),
    templateName: "",
    idKey,
    userId,
    bbk,
  });


  // 当 custUid 存在时，获取客户的所有报告模板列表
  const fetchClawFilePlan = useCallback(async () => {
    if (!custUid) {
      setClawFilePlanList([]);
      setActiveTemplate(null);
      setClawPlanFailed(false);
      setClawPlanLoading(false);
      return;
    }
    setClawPlanLoading(true);
    try {
      const res = await dynamicRenderApi.getAllClawFilePlan({
        sapId: sapId || userId || "",
        bbkOrgId: bbkOrgId || bbk || "",
        custUid,
        pageSource: pageSource || "default",
        platformSource: platformSource || "default",
        actvId: actvId || "",
        actvTyp: actvTyp || "",
        headBbk: !!headBbk,
      });
      if (res.data?.length) {
        const sorted = [...res.data].sort((a, b) => a.sortOrder - b.sortOrder);
        const _curTemplateId = parseInt(templateId, 10);
        sorted.forEach((item, index) => {
          item.key = item.templateId + item.resultId + index;
        });
        setClawFilePlanList(sorted);
        const initial =
          sorted.find((item) => item.templateId === _curTemplateId) ||
          sorted[0];
        setActiveTemplate(initial);
        setClawPlanFailed(false);
      } else {
        setClawFilePlanList([]);
        setActiveTemplate(null);
        setClawPlanFailed(true);
      }
    } catch (err) {
      console.error("获取客户报告模板列表失败:", err);
      setClawFilePlanList([]);
      setActiveTemplate(null);
      setClawPlanFailed(true);
    } finally {
      setClawPlanLoading(false);
    }
  }, [custUid, templateId, userId, bbk, sapId, bbkOrgId, pageSource, platformSource, actvId, actvTyp, headBbk]);


  useEffect(() => {
    fetchClawFilePlan();
  }, [fetchClawFilePlan]);


  // 获取当前模板配置
  const templateInfo = useMemo(() => {
    if (
      !isTemplateListLoaded ||
      !effectiveTemplateId ||
      !templateList.current?.length
    ) {
      return null;
    }
    const templateIdNum = Number(effectiveTemplateId);
    return templateList.current.find(
      (item) => item.templateId === templateIdNum
    );
  }, [effectiveTemplateId, isTemplateListLoaded]);


  const fileName = useMemo(() => {
    const effectiveFileName =
      urlFileName || templateInfo?.templateName || resolvedFileName;
    if (effectiveFileName && !effectiveFileName.includes(".")) {
      return effectiveFileName + ".html";
    }
    return effectiveFileName;
  }, [templateInfo]);


  const templateType = useMemo<HtmlPreviewTemplateType>(() => {
    if (templateInfo) {
      return templateInfo?.templateFlag === "main" ? "main" : "sub";
    } else {
      return custUid ? "sub" : "main";
    }
  }, [templateInfo, custUid]);


  useEffect(() => {
    if (
      templateInfo?.templateFlag === "person-event" &&
      rawRecordData?.custUid &&
      rawRecordData?.custName &&
      !personEventsFlagRef.current
    ) {
      personEventsFlagRef.current = true;
      const payload = {
        file_url: fileName || "",
        file_name: fileName || "",
        button_id: "plan",
        button_name: "查看方案",
        button_text: "📋 查看方案",
        button_type: "plan",
        customer_id: rawRecordData?.custUid ?? null,
        customer_name: rawRecordData?.custName ?? null,
        customer_info: {
          customer_id: rawRecordData?.custUid || "",
          name: rawRecordData?.custName || "",
        },
        clicked_at: new Date().toISOString(),
        source_id: source,
        event_type: "button_click" as const,
        template_id: templateInfo?.templateId ?? null,
        result_id: effectiveResultId,
        template_type: "sub" as const,
        page_source: pageSource || "default",
        platform_source: platformSource || "default",
      };
      htmlPreviewEventsApi.recordClick(payload);
    }
  }, [
    rawRecordData,
    templateInfo,
    htmlPreviewEventsApi,
    effectiveResultId,
    pageSource,
    platformSource,
  ]);


  const isLoading =
    phase === "resolving_id_key" ||
    phase === "waiting_template_list" ||
    phase === "loading_data" ||
    phase === "polling" ||
    phase === "rendering";


  const isPolling = phase === "polling";


  // 「随手改」可视化编辑模式下，下载时必须拿到编辑器内最新编辑后的内容（而非页面加载时的
  // renderedHtmlContent 旧内容）。仅在 RM + SKILL_CONFIG 编辑模式且外层 snap 编辑器 iframe
  // 就绪时，向其拉取一次最新 HTML；否则（非编辑模式 / 拉取异常）直接回落 renderedHtmlContent。
  const pullLatestEditorHtml = useCallback(async (): Promise<string | null> => {
    if (pageSource !== "RM" || platformSource !== "SKILL_CONFIG") return null;
    const frameWin = iframeRef.current?.contentWindow;
    if (!frameWin) return null;
    return new Promise<string | null>((resolve) => {
      const requestId = `${Date.now()}_${Math.random().toString(36).slice(2)}`;
      const onResult = (e: MessageEvent) => {
        const d = e.data || {};
        if (
          d.type === "SNAP_EDITOR_GET_HTML_RESULT" &&
          d.requestId === requestId
        ) {
          clearTimeout(timer);
          window.removeEventListener("message", onResult);
          resolve(typeof d.html === "string" && d.html ? d.html : null);
        }
      };
      const timer = setTimeout(() => {
        window.removeEventListener("message", onResult);
        resolve(null);
      }, 5000);
      window.addEventListener("message", onResult);
      try {
        frameWin.postMessage(
          { type: "SNAP_EDITOR_GET_HTML_REQUEST", requestId },
          window.location.origin,
        );
      } catch {
        clearTimeout(timer);
        window.removeEventListener("message", onResult);
        resolve(null);
      }
    }).catch(() => null);
  }, [pageSource, platformSource]);


  const handleDownload = useCallback(async (): Promise<void> => {
    const html = (await pullLatestEditorHtml()) ?? renderedHtmlContent;
    if (!html) return;
    try {
      const blob = new Blob([html], { type: "text/html" });
      const blobUrlStr = URL.createObjectURL(blob);


      const link = document.createElement("a");
      link.href = blobUrlStr;
      link.download = fileName.endsWith(".html")
        ? fileName
        : `${fileName}.html`;
      link.target = "_blank";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);


      setTimeout(() => URL.revokeObjectURL(blobUrlStr), 100);
    } catch (err) {
      console.error("动态渲染下载失败:", err);
    }
  }, [fileName, renderedHtmlContent, pullLatestEditorHtml]);


  // ============================================================
  // 报告内容向父页面透传/下载（三套兼容方案）
  //   方案1（needReportHtml=Y）：父页面需要整份渲染后的原始 HTML。该标识通过 URL 查询
  //      参数下发，renderedHtmlContent 生成后由 ReportView 主动通过 postMessage
  //      透传 REPORT_HTML 消息给父页面。
  //   方案2（REPORT_URL_REQUEST）：父页面仅触发下载动作，通过 postMessage 发送
  //      REPORT_URL_REQUEST，ReportView 生成短时效 blob URL 并以 REPORT_URL 回传父页面。
  //   方案3（REPORT_DOWNLOAD_REQUEST）：父页面仅触发下载动作，通过 postMessage 发送
  //      REPORT_DOWNLOAD_REQUEST，由 ReportView 侧复用 handleDownload 自行下载，
  //      不向父页面外发任何报告内容。
  //   加载完成通知（REPORT_READY）：renderedHtmlContent 通过 srcDoc 注入的 iframe
  //      onLoad 触发后，ReportView 主动向父页面发送 REPORT_READY，告知报告内容已
  //      render 完成。父页面可据此感知就绪态后再触发下载/隐藏加载态。
  // 安全提示：报告内容属敏感数据，透传时机/对象由父页面配合管控（见
  //      docs/report-parent-integration-guide.md）；ReportView 侧仅负责执行透传/下载，
  //      不额外做来源白名单限制。
  // ============================================================
  // ---------- 方案1：URL 标识 needReportHtml=Y 触发的原始 HTML 主动透传 ----------
  const needReportHtml = searchParams.get("needReportHtml");
  // 透传去重标记：renderedHtmlContent 稳定后仅执行一次，避免重复 postMessage
  const reportHandledRef = useRef<boolean>(false);


  useEffect(() => {
    // 明确关闭该能力时直接跳过
    if (needReportHtml !== "Y") return;
    // 仅在报告内容生成成功才尝试透传
    if (!renderedHtmlContent || reportHandledRef.current) return;


    const fileNameWithName = fileName.endsWith(".html")
      ? fileName
      : `${fileName}.html`;


    const ok = sendReportMessageToParent({
      type: "REPORT_HTML",
      data: {
        html: renderedHtmlContent,
        fileName: fileNameWithName,
        resultId: effectiveResultId,
        templateId: effectiveTemplateId ? String(effectiveTemplateId) : null,
        timestamp: Date.now(),
      },
    });
    if (ok) {
      reportHandledRef.current = true;
    }
  }, [
    renderedHtmlContent,
    fileName,
    needReportHtml,
    effectiveResultId,
    effectiveTemplateId,
  ]);


  // 用于保存方案2/方案3处理器的最新闭包值，避免注册的 handler 捕获过期状态。
  // onUrlRequest / onDownloadRequest 由父页面 REQUEST 消息触发，需读取渲染后最新的
  // renderedHtmlContent / fileName / effectiveResultId。
  const reportRequestCbRef = useRef<{
    sendUrl: () => Promise<void>;
    download: () => Promise<void>;
  }>({
    sendUrl: async () => { },
    download: async () => { },
  });
  reportRequestCbRef.current = {
    sendUrl: async () => {
      const html = (await pullLatestEditorHtml()) ?? renderedHtmlContent;
      if (!html) return;
      try {
        const blob = new Blob([html], { type: "text/html" });
        const blobUrlStr = URL.createObjectURL(blob);
        const fileNameWithName = fileName.endsWith(".html")
          ? fileName
          : `${fileName}.html`;
        const ok = sendReportMessageToParent({
          type: "REPORT_URL",
          data: {
            url: blobUrlStr,
            fileName: fileNameWithName,
            resultId: effectiveResultId,
            timestamp: Date.now(),
          },
        });
        if (!ok) {
          // 未授权则回收临时 URL，避免泄漏
          setTimeout(() => URL.revokeObjectURL(blobUrlStr), 0);
        }
      } catch (err) {
        console.error("透传短时效下载 URL 失败:", err);
      }
    },
    download: async () => {
      await handleDownload();
    },
  };


  // 挂载时注册父页面下载请求处理器，卸载时注销。
  // onUrlRequest/onDownloadRequest 仅注册一次，内部经 reportRequestCbRef 读取最新闭包值，
  // 因此不受本例 effect 空依赖的影响。
  useEffect(() => {
    registerReportRequestHandler({
      onUrlRequest: () => {
        void reportRequestCbRef.current.sendUrl();
      },
      onDownloadRequest: () => {
        void reportRequestCbRef.current.download();
      },
    });
    return () => unregisterReportRequestHandler();
  }, []);


  const handleIframeLoad = useCallback(() => {
    reattachTrackersRef.current?.();


    if (rawRecordData?.CRON_JOB_ID) {
      cronJobApi.markTaskRead(rawRecordData.CRON_JOB_ID, true).catch(() => { });
    }


    // 通知父页面：renderedHtmlContent 已在 iframe 中加载完成。
    // 仅当报告内容就绪且处于 iframe 子环境时发送，规避独立标签页访问。
    if (renderedHtmlContent) {
      const ok = sendReportMessageToParent({
        type: "REPORT_READY",
        data: {
          resultId: effectiveResultId,
          templateId: effectiveTemplateId ? String(effectiveTemplateId) : null,
          timestamp: Date.now(),
        },
      });
      if (!ok) {
        console.warn(
          "[ReportView] 报告加载完成通知发送失败（可能非 iframe 环境）"
        );
      }
    }
  }, [
    rawRecordData,
    renderedHtmlContent,
    effectiveResultId,
    effectiveTemplateId,
  ]);


  const metaData = useMemo(
    () => ({
      cronTaskId:
        trackingContext.cronTaskId || (rawRecordData?.CRON_JOB_ID as string),
      cronTaskName: trackingContext.cronTaskName,
      fileUrl: fileName || "",
      fileName: fileName || null,
      listKey: null,
      listName: templateInfo?.templateName ?? null,
      defaultCustomerInfo: custUid
        ? { customer_id: custUid, customer_name: custName }
        : null,
      traceId: rawRecordData?.TRACE_ID,
      templateId: effectiveTemplateId ? String(effectiveTemplateId) : null,
      resultId: effectiveResultId,
      templateType,
      pageSource,
      platformSource,
    }),
    [
      trackingContext,
      rawRecordData,
      fileName,
      effectiveTemplateId,
      custUid,
      custName,
      pageSource,
      platformSource,
      templateType,
    ]
  );


  const { cleanup: cleanupTrackers, reattach: reattachTrackers } =
    useIframeHtmlPreviewTracking(
      iframeRef,
      {
        metaData,
        load: htmlPreviewEventsApi.recordClick,
        click: {
          reporter: htmlPreviewEventsApi.recordClick,
          onOpenNestedPreview: setNestedPreview,
          getTemplateName: (templateIdNum: number) => {
            return templateList.current.find(
              (t) => t.templateId === templateIdNum
            )?.templateName;
          },
        },
        exposure: { reporter: htmlPreviewEventsApi.recordClick },
      },
      []
    );
  const reattachTrackersRef = useRef(reattachTrackers);
  reattachTrackersRef.current = reattachTrackers;


  const renderPreviewContent = useMemo(() => {
    if (isLoading) {
      const tip = isPolling
        ? "文件正在生成中，内容准备完成后，页面会自动展示最新预览"
        : phase === "rendering"
          ? "正在渲染报告..."
          : "加载中...";
      return (
        <div className={styles.loadingWrapper}>
          <Spin tip={tip} size="large" />
        </div>
      );
    }
    if (error) {
      return (
        <div className={styles.errorWrapper}>
          <div className={styles.errorText}>{error}</div>
          <IconButton icon={<SparkDownloadLine />} onClick={handleDownload}>
            下载文件查看
          </IconButton>
        </div>
      );
    }
    if (renderedHtmlContent) {
      // 「随手改」可视化编辑模式：用内置 HTML 编辑器 iframe 替代只读预览
      if (pageSource === "RM" && platformSource === "SKILL_CONFIG") {
        return (
          <SnapHtmlPreview
            ref={iframeRef}
            docHtml={renderedHtmlContent}
            title={fileName || "报告编辑"}
            onLoad={handleIframeLoad}
            rawRecordData={rawRecordData}
            resultId={effectiveResultId}
          />
        );
      }
      return (
        <div className={styles.htmlPreviewWrapper}>
          <iframe
            ref={iframeRef}
            srcDoc={renderedHtmlContent}
            className={styles.previewIframe}
            title="File Preview"
            onLoad={handleIframeLoad}
          />
        </div>
      );
    }
    return null;
  }, [
    isLoading,
    isPolling,
    phase,
    error,
    renderedHtmlContent,
    fileName,
    handleDownload,
    handleIframeLoad,
    retry,
    pageSource,
    platformSource,
  ]);


  useEffect(() => {
    cleanupTrackers();
  }, []);


  return (
    <FilePreviewPresentationProvider value="modal">
      <div className={styles.reportContainer}>
        <Card
          styles={{
            body: { padding: 0, height: "100%", overflow: "hidden" },
          }}
          style={{ height: "100%" }}
        >
          {custUid && clawPlanLoading && (
            <div className={styles.tabsLoadingWrapper}>
              <Spin size="small" />
            </div>
          )}
          {custUid && !clawPlanLoading && clawFilePlanList.length > 0 && (
            <div className={styles.tabsWrapper}>
              <Tabs
                activeKey={activeTemplate?.key}
                onChange={(key) => {
                  const next = clawFilePlanList.find(
                    (item) => item.key === key
                  );
                  if (next) setActiveTemplate(next);
                }}
                items={clawFilePlanList.map((item) => ({
                  key: item.key,
                  label: item.skillName,
                }))}
                size="small"
              />
            </div>
          )}
          <div
            style={
              custUid && clawFilePlanList.length > 0
                ? { height: "calc(100% - 48px)" }
                : { height: "100%" }
            }
          >
            {showClawPlanEmpty ? (
              <div className={styles.emptyWrapper}>
                <div className={styles.emptyIcon}>
                  <svg
                    className={styles.emptyIconSvg}
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                    <polyline points="7 10 12 15 17 10" />
                    <line x1="12" y1="15" x2="12" y2="3" />
                  </svg>
                </div>
                <div className={styles.emptyTitle}>未查询到个人报告数据</div>
                <div className={styles.emptyDesc}>
                  当前客户暂无可用报告模板，请确认客户信息后重试
                </div>
              </div>
            ) : (
              renderPreviewContent
            )}
          </div>
        </Card>


        {nestedPreview && (
          <FilePreviewModal
            open
            onClose={() => setNestedPreview(null)}
            fileUrl={nestedPreview.fileUrl}
            fileName={nestedPreview.fileName}
            enableClickTracking
            enableListSnapshotTracking={false}
            trackingListKey={nestedPreview.listKey}
            trackingListName={nestedPreview.listName}
            defaultCustomerInfo={nestedPreview.customerInfo}
            custUid={nestedPreview.custUid}
            rootResultId={effectiveResultId}
            rootTemplateId={
              templateInfo?.templateId
                ? String(templateInfo.templateId)
                : undefined
            }
            urlParams={{ sapId, bbkOrgId }}
          />
        )}
      </div>
    </FilePreviewPresentationProvider>
  );
}


export default ReportViewPage;
