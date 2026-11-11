import { useReducer, useCallback, useRef, useEffect } from "react";
import Handlebars from "handlebars";
import { useDynamicRender } from "@/components/agentscope-chat/DynamicRenderContext";
import { dynamicRenderApi, RecordDataResponse } from "@/api/modules/dynamicRender";


// ========== PagePhase 状态枚举 ==========
export type PagePhase =
  | "idle"
  | "resolving_id_key"
  | "waiting_template_list"
  | "loading_data"
  | "polling"
  | "rendering"
  | "ready"
  | "error";


// ========== State & Action 类型 ==========
export interface PageState {
  phase: PagePhase;
  error: string | null;
  renderedHtmlContent: string | null;
  rawRecordData: RecordDataResponse;
  resolvedResultId: string | null;
  resolvedTemplateId: string | null;
  resolvedFileName: string | null;
}


type PageAction =
  | { type: "START_RESOLVING_ID_KEY" }
  | { type: "RESOLVED_ID_KEY"; resultId: string; templateId?: string; fileName?: string }
  | { type: "RESOLVE_ID_KEY_FAILED"; error: string }
  | { type: "WAITING_TEMPLATE_LIST" }
  | { type: "START_LOADING_DATA" }
  | { type: "START_POLLING" }
  | { type: "START_RENDERING" }
  | { type: "RENDERED"; htmlContent: string; rawData?: RecordDataResponse }
  | { type: "SET_ERROR"; error: string }
  | { type: "RESET" };


const MAX_POLLING_COUNT = 60;


function pageReducer(state: PageState, action: PageAction): PageState {
  switch (action.type) {
    case "START_RESOLVING_ID_KEY":
      return { ...state, phase: "resolving_id_key", error: null };
    case "RESOLVED_ID_KEY":
      return {
        ...state,
        phase: "idle",
        error: null,
        resolvedResultId: action.resultId,
        resolvedTemplateId: action.templateId ?? null,
        resolvedFileName: action.fileName ?? null,
      };
    case "RESOLVE_ID_KEY_FAILED":
      return { ...state, phase: "error", error: action.error };
    case "WAITING_TEMPLATE_LIST":
      return { ...state, phase: "waiting_template_list", error: null };
    case "START_LOADING_DATA":
      return { ...state, phase: "loading_data", error: null };
    case "START_POLLING":
      return { ...state, phase: "polling", error: null };
    case "START_RENDERING":
      return { ...state, phase: "rendering", error: null };
    case "RENDERED":
      return {
        ...state,
        phase: "ready",
        renderedHtmlContent: action.htmlContent,
        rawRecordData: action.rawData ?? state.rawRecordData,
        error: null,
      };
    case "SET_ERROR":
      return { ...state, phase: "error", error: action.error };
    case "RESET":
      return { phase: "idle", error: null, renderedHtmlContent: null, rawRecordData: null, resolvedResultId: null, resolvedTemplateId: null, resolvedFileName: null };
    default:
      return state;
  }
}


// ========== Hook 参数 ==========
export interface UseReportViewDataParams {
  resultId: string | null;
  templateId: string | null;
  templateName: string | null;
  idKey: string | null;
  userId?: string | null;
  bbk?: string | null;
}


// ========== Hook 返回值 ==========
export interface UseReportViewDataReturn {
  phase: PagePhase;
  error: string | null;
  renderedHtmlContent: string | null;
  rawRecordData: RecordDataResponse | null;
  resolvedResultId: string | null;
  resolvedTemplateId: string | null;
  resolvedFileName: string | null;
  dispatch: React.Dispatch<PageAction>;
  retry: () => void;
}


export function useReportViewData(
  params: UseReportViewDataParams,
): UseReportViewDataReturn {
  const { resultId, templateId, templateName, idKey, userId, bbk } = params;


  const [state, dispatch] = useReducer(pageReducer, {
    phase: "idle",
    error: null,
    renderedHtmlContent: null,
    rawRecordData: null,
    resolvedResultId: null,
    resolvedTemplateId: null,
    resolvedFileName: null,
  });


  const {
    renderTemplate,
    renderStaticTemplate,
    isStaticTemplate,
    getTemplateIdByName,
    renderTemplateByName,
    getTemplateContentByName,
    isTemplateListLoaded,
  } = useDynamicRender();


  // 缓存已获取的动态渲染数据，避免重复请求
  const cachedRenderDataRef = useRef<{
    resultId: string;
    templateId: string;
    data: RecordDataResponse;
  } | null>(null);


  // 标记是否正在通过 idKey 获取 resultId
  const isFetchingResultIdRef = useRef(false);
  // 标记初始化是否完成
  const initCompleteRef = useRef(false);


  // 轮询计数器和定时器
  const pollingCountRef = useRef(0);
  const pollingTimerRef = useRef<NodeJS.Timeout | null>(null);


  // 缓存提前发起的模板内容 Promise（不依赖模板列表）
  const templateContentPromiseRef = useRef<Promise<string | null> | null>(null);
  // 缓存提前发起的模板数据 Promise
  const recordDataPromiseRef = useRef<Promise<any> | null>(null);


  // 跟踪上一次的 resultId/templateId，检测切换时重置内部状态
  const prevParamsRef = useRef<{ resultId: string | null; templateId: string | null } | null>(null);


  useEffect(() => {
    const prev = prevParamsRef.current;
    prevParamsRef.current = { resultId, templateId };


    // Skip on initial mount — initCompleteRef is already false, no need to reset
    if (prev === null) return;


    if (prev.resultId !== resultId || prev.templateId !== templateId) {
      // 切换模板时重置内部状态，使 Effect 3 重新执行
      initCompleteRef.current = false;
      cachedRenderDataRef.current = null;
      recordDataPromiseRef.current = null;
      templateContentPromiseRef.current = null;
      pollingCountRef.current = 0;
      if (pollingTimerRef.current) {
        clearTimeout(pollingTimerRef.current);
        pollingTimerRef.current = null;
      }
      dispatch({ type: "RESET" });
    }
  }, [resultId, templateId, dispatch]);


  const effectiveResultId = resultId || state.resolvedResultId;
  const effectiveTemplateId = templateId || state.resolvedTemplateId;
  const hasValidResultId = !!effectiveResultId;
  const hasValidTemplateParams = !!templateId || !!templateName;


  // 通过 idKey 获取 resultId
  const fetchResultIdByIdKey = useCallback(async (key: string, uid?: string | null, b?: string | null, tname?: string | null) => {
    try {
      const res = await dynamicRenderApi.queryIdKey({ idKey: key, userId: uid, bbkOrgId: b, templateName: tname });
      const data = res.data as { resultId: string, templateId: string, fileName: string };
      if (res.code === "200" && data?.resultId) {
        return { resultId: data.resultId, templateId: data.templateId, fileName: data.fileName };
      }
      return null;
    } catch (err) {
      console.error("通过 idKey 获取 resultId 失败:", err);
      return null;
    }
  }, []);


  // 获取动态渲染数据（带轮询逻辑）
  const fetchDynamicRenderData = useCallback(
    async (
      rid: string,
      tidStr: string | null,
      tnameParam: string | null,
    ) => {
      try {
        let templateIdNum: number | null = tidStr
          ? parseInt(tidStr, 10)
          : null;
        if (!templateIdNum && tnameParam) {
          templateIdNum = getTemplateIdByName(tnameParam);
        }


        if (!templateIdNum) {
          // 当没有 templateId 但有 templateName 时，直接用 templateName 渲染
          if (tnameParam) {
            dispatch({ type: "START_LOADING_DATA" });
            // 优先使用提前发起的 recordData Promise；消费后立即失效，保证每次轮询都重新发起请求
            const res = recordDataPromiseRef.current
              ? await recordDataPromiseRef.current
              : await dynamicRenderApi.getRecordData(rid, "");
            recordDataPromiseRef.current = null;
            if (res.code !== "200") {
              pollingCountRef.current += 1;
              if (pollingCountRef.current >= MAX_POLLING_COUNT) {
                dispatch({
                  type: "SET_ERROR",
                  error: "文件生成超时，请稍后重试",
                });
                return;
              }
              dispatch({ type: "START_POLLING" });
              if (pollingTimerRef.current) {
                clearTimeout(pollingTimerRef.current);
              }
              pollingTimerRef.current = setTimeout(() => {
                fetchDynamicRenderData(rid, tidStr, tnameParam);
              }, 10000);
              return;
            }
            pollingCountRef.current = 0;
            if (pollingTimerRef.current) {
              clearTimeout(pollingTimerRef.current);
              pollingTimerRef.current = null;
            }
            cachedRenderDataRef.current = {
              resultId: rid,
              templateId: "",
              data: res.data,
            };
            dispatch({ type: "START_RENDERING" });
            // 优先使用提前发起的模板内容 Promise
            const contentPromise = templateContentPromiseRef.current
              || getTemplateContentByName(tnameParam);
            const content = await contentPromise;
            if (!content) {
              dispatch({ type: "SET_ERROR", error: "模板内容加载失败" });
              return;
            }
            const compiled = Handlebars.compile(content);
            const renderedHtml = compiled(res.data);
            if (renderedHtml) {
              dispatch({ type: "RENDERED", htmlContent: renderedHtml, rawData: res.data });
            } else {
              dispatch({ type: "SET_ERROR", error: "模板渲染失败" });
            }
            return;
          }
          dispatch({ type: "SET_ERROR", error: "无法获取模板ID" });
          return;
        }


        // 静态模板：无需调用 /api/template/result 获取数据
        if (isStaticTemplate(templateIdNum)) {
          dispatch({ type: "START_RENDERING" });
          const renderedHtml = await renderStaticTemplate(templateIdNum);
          if (renderedHtml) {
            dispatch({ type: "RENDERED", htmlContent: renderedHtml });
          } else {
            dispatch({ type: "SET_ERROR", error: "静态模板渲染失败" });
          }
          return;
        }


        // 检查缓存
        if (
          cachedRenderDataRef.current &&
          cachedRenderDataRef.current.resultId === rid &&
          cachedRenderDataRef.current.templateId === (tidStr || "")
        ) {
          dispatch({ type: "START_RENDERING" });
          const renderedHtml = await renderTemplate(
            templateIdNum,
            cachedRenderDataRef.current.data,
          );
          if (renderedHtml) {
            dispatch({ type: "RENDERED", htmlContent: renderedHtml, rawData: cachedRenderDataRef.current.data });
          } else {
            dispatch({ type: "SET_ERROR", error: "模板渲染失败" });
          }
          return;
        }


        dispatch({ type: "START_LOADING_DATA" });
        // 优先使用提前发起的 recordData Promise；消费后立即失效，保证每次轮询都重新发起请求
        const res = recordDataPromiseRef.current
          ? await recordDataPromiseRef.current
          : await dynamicRenderApi.getRecordData(rid, tidStr || "");
        recordDataPromiseRef.current = null;


        // 如果返回码不是 200，说明文件正在生成中
        if (res.code !== "200") {
          pollingCountRef.current += 1;
          if (pollingCountRef.current >= MAX_POLLING_COUNT) {
            dispatch({
              type: "SET_ERROR",
              error: "文件生成超时，请稍后重试",
            });
            return;
          }
          dispatch({ type: "START_POLLING" });
          if (pollingTimerRef.current) {
            clearTimeout(pollingTimerRef.current);
          }
          pollingTimerRef.current = setTimeout(() => {
            fetchDynamicRenderData(rid, tidStr, tnameParam);
          }, 10000);
          return;
        }


        // 成功获取数据，停止轮询
        pollingCountRef.current = 0;
        if (pollingTimerRef.current) {
          clearTimeout(pollingTimerRef.current);
          pollingTimerRef.current = null;
        }


        // 缓存数据
        cachedRenderDataRef.current = {
          resultId: rid,
          templateId: tidStr || "",
          data: res.data,
        };


        dispatch({ type: "START_RENDERING" });
        const renderedHtml = await renderTemplate(templateIdNum, res.data);
        if (renderedHtml) {
          dispatch({ type: "RENDERED", htmlContent: renderedHtml, rawData: res.data });
        } else {
          dispatch({ type: "SET_ERROR", error: "模板渲染失败" });
        }
      } catch (err) {
        console.error("获取数据失败:", err);
        dispatch({ type: "SET_ERROR", error: "数据加载失败" });
      }
    },
    [renderTemplate, renderStaticTemplate, isStaticTemplate, getTemplateIdByName, renderTemplateByName, getTemplateContentByName],
  );


  // ========== Effect 1: idKey 解析 ==========
  useEffect(() => {
    if (idKey && !resultId && !state.resolvedResultId && !isFetchingResultIdRef.current) {
      isFetchingResultIdRef.current = true;
      dispatch({ type: "START_RESOLVING_ID_KEY" });
      fetchResultIdByIdKey(idKey, userId, bbk, templateName).then((resolved) => {
        isFetchingResultIdRef.current = false;
        if (resolved) {
          initCompleteRef.current = false;
          dispatch({
            type: "RESOLVED_ID_KEY",
            resultId: resolved.resultId,
            templateId: resolved.templateId,
            fileName: resolved.fileName,
          });
        } else {
          dispatch({
            type: "RESOLVE_ID_KEY_FAILED",
            error: "无法通过 idKey 获取 resultId",
          });
        }
      });
    }
  }, [idKey, resultId, state.resolvedResultId, userId, bbk, templateName, fetchResultIdByIdKey]);


  // ========== Effect 2: 等待模板列表 + 提前发起请求 ==========
  useEffect(() => {
    if (initCompleteRef.current) return;
    if (!hasValidResultId) return;
    if (!hasValidTemplateParams) return;


    // 必须等待模板列表加载完成（无论使用 templateId 还是 templateName）
    if (!isTemplateListLoaded) {
      dispatch({ type: "WAITING_TEMPLATE_LIST" });
      return;
    }


    // 模板列表已加载，提前发起模板内容请求（此时 getTemplateContentByName 走分支 B，使用 loadingPromiseRef 去重）
    if (templateName && !templateContentPromiseRef.current) {
      templateContentPromiseRef.current = getTemplateContentByName(templateName);
    }


    // 如果 templateId 已知，提前发起数据请求
    if (effectiveTemplateId && effectiveResultId && !recordDataPromiseRef.current) {
      recordDataPromiseRef.current = dynamicRenderApi.getRecordData(
        effectiveResultId,
        effectiveTemplateId
      );
    }


    // 如果正在通过 idKey 获取 resultId，等待
    if (idKey && isFetchingResultIdRef.current) {
      return;
    }
  }, [
    hasValidResultId,
    hasValidTemplateParams,
    isTemplateListLoaded,
    idKey,
    templateName,
    effectiveTemplateId,
    effectiveResultId,
    getTemplateContentByName,
  ]);


  // ========== Effect 3: 主数据加载流程 ==========
  useEffect(() => {
    if (initCompleteRef.current) return;
    if (!hasValidResultId) return;
    if (!hasValidTemplateParams) return;


    // 等待模板列表加载（无论使用 templateId 还是 templateName）
    if (!isTemplateListLoaded) {
      return;
    }


    // 等待 idKey 解析完成
    if (idKey && isFetchingResultIdRef.current) {
      return;
    }


    initCompleteRef.current = true;
    pollingCountRef.current = 0;
    fetchDynamicRenderData(effectiveResultId, effectiveTemplateId, templateName);
  }, [
    hasValidResultId,
    hasValidTemplateParams,
    effectiveResultId,
    effectiveTemplateId,
    templateName,
    isTemplateListLoaded,
    idKey,
    fetchDynamicRenderData,
  ]);


  // ========== Effect 4: 清理 ==========
  useEffect(() => {
    return () => {
      if (pollingTimerRef.current) {
        clearTimeout(pollingTimerRef.current);
        pollingTimerRef.current = null;
      }
      cachedRenderDataRef.current = null;
      initCompleteRef.current = false;
      isFetchingResultIdRef.current = false;
      pollingCountRef.current = 0;
      templateContentPromiseRef.current = null;
      recordDataPromiseRef.current = null;
    };
  }, []);


  const retry = useCallback(() => {
    initCompleteRef.current = false;
    pollingCountRef.current = 0;
    recordDataPromiseRef.current = null;
    if (pollingTimerRef.current) {
      clearTimeout(pollingTimerRef.current);
      pollingTimerRef.current = null;
    }
    dispatch({ type: "RESET" });
  }, []);


  return {
    phase: state.phase,
    error: state.error,
    renderedHtmlContent: state.renderedHtmlContent,
    rawRecordData: state.rawRecordData,
    resolvedResultId: state.resolvedResultId,
    resolvedTemplateId: state.resolvedTemplateId,
    resolvedFileName: state.resolvedFileName,
    dispatch,
    retry,
  };
}