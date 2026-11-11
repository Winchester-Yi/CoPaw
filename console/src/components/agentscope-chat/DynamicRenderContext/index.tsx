import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Handlebars from "handlebars";
import {
  dynamicRenderApi,
  type TemplateInfo,
} from "@/api/modules/dynamicRender";

interface TemplateCache {
  content: string;
  compiled: HandlebarsTemplateDelegate;
}

interface DynamicRenderContextValue {
  /** 模板列表是否已加载 */
  isTemplateListLoaded: boolean;
  /** 加载错误信息 */
  error: string | null;
  /** 初始化模板列表和内容（应用启动时调用） */
  initialize: (options?: { skipPreload?: boolean }) => Promise<void>;
  /** 获取模板内容（从缓存或重新获取） */
  getTemplateContent: (templateId: number) => Promise<string | null>;
  /** 根据模板名称获取模板ID */
  getTemplateIdByName: (templateName: string) => number | null;
  /** 判断模板是否为静态模板（templateFlag === 'no_query'） */
  isStaticTemplate: (templateId: number) => boolean;
  /** 渲染模板数据 */
  renderTemplate: (
    templateId: number,
    data: Record<string, any>
  ) => Promise<string | null>;
  /** 渲染静态模板（无需数据获取，直接渲染模板内容） */
  renderStaticTemplate: (templateId: number) => Promise<string | null>;
  /** 直接用 templateName 获取模板内容（不依赖 templateId） */
  getTemplateContentByName: (templateName: string) => Promise<string | null>;
  /** 直接用 templateName 渲染模板 */
  renderTemplateByName: (
    templateName: string,
    data: Record<string, any>
  ) => Promise<string | null>;
  templateList
}

const DynamicRenderContext = createContext<DynamicRenderContextValue>({
  isTemplateListLoaded: false,
  error: null,
  initialize: async () => { },
  getTemplateContent: async () => null,
  getTemplateIdByName: () => null,
  isStaticTemplate: () => false,
  renderTemplate: async () => null,
  renderStaticTemplate: async () => null,
  getTemplateContentByName: async () => null,
  renderTemplateByName: async () => null,
  templateList: [],
});

interface DynamicRenderProviderProps {
  children: React.ReactNode;
}

export function DynamicRenderProvider(props: DynamicRenderProviderProps) {
  const { children } = props;
  const [isTemplateListLoaded, setIsTemplateListLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 模板列表缓存
  const templateListRef = useRef<TemplateInfo[]>([]);
  // 模板内容缓存: templateId -> TemplateCache
  const templateCacheRef = useRef<Map<number, TemplateCache>>(new Map());
  // 正在加载的 Promise 缓存，避免重复请求
  const loadingPromiseRef = useRef<Map<number, Promise<string>>>(new Map());
  // 按 templateName 去重的 Promise 缓存（模板列表未加载时使用）
  const loadingByNameRef = useRef<Map<string, Promise<string>>>(new Map());
  // 初始化状态标记
  const initializedRef = useRef(false);
  // requestIdleCallback 的 ID，用于清理
  const idleCallbackIdRef = useRef<number | null>(null);
  // 组件卸载标记
  const unmountedRef = useRef(false);

  // 组件卸载时清理
  useEffect(() => {
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
      // 取消 pending 的 requestIdleCallback
      if (idleCallbackIdRef.current !== null) {
        cancelIdleCallback(idleCallbackIdRef.current);
        idleCallbackIdRef.current = null;
      }
      loadingByNameRef.current.clear();
    };
  }, []);

  /**
   * 预加载模板内容（使用 Promise 缓存避免重复请求）
   */
  const preloadTemplates = useCallback(async (templates: TemplateInfo[]) => {
    for (const template of templates) {
      // 检查组件是否已卸载
      if (unmountedRef.current) return;

      // 跳过已缓存的模板
      if (templateCacheRef.current.has(template.templateId)) {
        continue;
      }

      // 检查是否有正在进行的加载请求
      if (loadingPromiseRef.current.has(template.templateId)) {
        continue;
      }

      // 创建加载 Promise 并缓存
      const loadPromise = (async () => {
        try {
          const response = await dynamicRenderApi.getTemplateContent(
            template.templateName
          );
          // 检查组件是否已卸载
          if (unmountedRef.current) return "";
          const compiled = Handlebars.compile(response.content);
          templateCacheRef.current.set(template.templateId, {
            content: response.content,
            compiled,
          });
          loadingPromiseRef.current.delete(template.templateId);
          return response.content;
        } catch (err) {
          loadingPromiseRef.current.delete(template.templateId);
          console.error(`预加载模板 ${template.templateName} 失败:`, err);
          throw err;
        }
      })();

      loadingPromiseRef.current.set(template.templateId, loadPromise);
    }
  }, []);

  /**
   * 初始化模板列表和内容
   * 使用 requestIdleCallback 在页面空闲时加载，避免阻塞渲染
   * @param options.skipPreload - 是否跳过预加载所有模板（用于按需加载场景）
   */
  const initialize = useCallback(async (options?: { skipPreload?: boolean }) => {
    if (initializedRef.current) return;
    initializedRef.current = true;

    try {
      // 1. 获取模板列表
      const templateList = await dynamicRenderApi.getTemplateList();
      templateListRef.current = templateList.data || [];

      // 2. 如果 skipPreload 为 true，则不预加载模板内容（按需加载模式）
      if (options?.skipPreload) {
        setIsTemplateListLoaded(true);
        return;
      }

      // 3. 在页面空闲时预加载模板内容
      const loadTemplatesWhenIdle = () => {
        if (unmountedRef.current) return;

        if ("requestIdleCallback" in window) {
          idleCallbackIdRef.current = requestIdleCallback(
            () => {
              idleCallbackIdRef.current = null;
              preloadTemplates(templateList.data || []);
            },
            { timeout: 5000 }
          );
        } else {
          // 降级方案：使用 setTimeout
          setTimeout(() => {
            preloadTemplates(templateList.data || []);
          }, 100);
          // 保存 timeout ID 以便清理（如果需要的话）
        }
      };

      loadTemplatesWhenIdle();
      setIsTemplateListLoaded(true);
    } catch (err) {
      console.error("初始化模板列表失败:", err);
      setError(err instanceof Error ? err.message : "初始化失败");
      setIsTemplateListLoaded(true); // 即使失败也标记为已尝试
    }
  }, [preloadTemplates]);

  /**
   * 根据模板名称获取模板ID
   */
  const getTemplateIdByName = useCallback((templateName: string): number | null => {
    const template = templateListRef.current.find(
      (t) => t.templateName === templateName
    );
    return template ? template.templateId : null;
  }, []);

  /**
   * 获取模板内容（按需加载）
   */
  const getTemplateContent = useCallback(
    async (templateId: number): Promise<string | null> => {
      // 检查组件是否已卸载
      if (unmountedRef.current) return null;

      // 1. 先从缓存获取
      const cached = templateCacheRef.current.get(templateId);
      if (cached) {
        return cached.content;
      }

      // 2. 检查是否有正在进行的加载请求
      const existingPromise = loadingPromiseRef.current.get(templateId);
      if (existingPromise) {
        return existingPromise;
      }

      // 2b. 防御：如果通过 templateId 找不到，尝试通过 templateName 在 loadingByNameRef 中查找
      const template = templateListRef.current.find(
        (t) => t.templateId === templateId
      );
      if (template) {
        const existingByName = loadingByNameRef.current.get(template.templateName);
        if (existingByName) return existingByName;
      }

      // 3. 按需加载模板
      if (!template) {
        // 如果模板列表尚未加载完成，等待加载完成后再查找
        if (!isTemplateListLoaded) {
          console.warn(`模板列表尚未加载，等待中...`);
          return new Promise<string | null>((resolve) => {
            const check = () => {
              if (unmountedRef.current) {
                resolve(null);
                return;
              }
              const t = templateListRef.current.find(
                (t) => t.templateId === templateId
              );
              if (t) {
                getTemplateContent(templateId).then(resolve);
              } else if (!isTemplateListLoaded) {
                setTimeout(check, 100);
              } else {
                console.warn(`未找到模板 ID: ${templateId}`);
                resolve(null);
              }
            };
            check();
          });
        }
        console.warn(`未找到模板 ID: ${templateId}`);
        return null;
      }

      // 4. 创建加载 Promise 并缓存
      const loadPromise = (async () => {
        try {
          // 再次检查卸载状态
          if (unmountedRef.current) return "";
          const response = await dynamicRenderApi.getTemplateContent(
            template.templateName
          );
          // 检查组件是否已卸载
          if (unmountedRef.current) return "";
          const compiled = Handlebars.compile(response.content);
          templateCacheRef.current.set(templateId, {
            content: response.content,
            compiled,
          });
          loadingPromiseRef.current.delete(templateId);
          return response.content;
        } catch (err) {
          loadingPromiseRef.current.delete(templateId);
          console.error(`加载模板 ${template.templateName} 失败:`, err);
          throw err;
        }
      })();

      loadingPromiseRef.current.set(templateId, loadPromise);
      return loadPromise;
    },
    []
  );

  /**
   * 渲染模板数据
   */
  const renderTemplate = useCallback(
    async (
      templateId: number,
      data: Record<string, any>
    ): Promise<string | null> => {
      try {
        // 1. 确保模板已加载
        let cache = templateCacheRef.current.get(templateId);
        if (!cache) {
          const content = await getTemplateContent(templateId);
          if (!content) {
            throw new Error(`模板 ${templateId} 加载失败`);
          }
          cache = templateCacheRef.current.get(templateId)!;
        }
        // 2. 使用缓存的编译函数渲染
        const rendered = cache.compiled(data);
        return rendered;
      } catch (err) {
        console.error(`渲染模板 ${templateId} 失败:`, err);
        return null;
      }
    },
    [getTemplateContent]
  );

  /**
   * 判断模板是否为静态模板（templateFlag === 'no_query'）
   * 静态模板无需调用 /api/template/result 获取数据，直接渲染模板内容即可
   */
  const isStaticTemplate = useCallback((templateId: number): boolean => {
    const template = templateListRef.current.find(
      (t) => t.templateId === templateId
    );
    return template?.templateFlag === "no_query";
  }, []);

  /**
   * 渲染静态模板（无需数据获取，直接渲染模板内容）
   */
  const renderStaticTemplate = useCallback(
    async (templateId: number): Promise<string | null> => {
      try {
        let cache = templateCacheRef.current.get(templateId);
        if (!cache) {
          const content = await getTemplateContent(templateId);
          if (!content) {
            throw new Error(`静态模板 ${templateId} 加载失败`);
          }
          cache = templateCacheRef.current.get(templateId)!;
        }
        // 静态模板直接使用空对象渲染
        const rendered = cache.compiled({});
        return rendered;
      } catch (err) {
        console.error(`渲染静态模板 ${templateId} 失败:`, err);
        return null;
      }
    },
    [getTemplateContent]
  );

  /**
   * 直接用 templateName 获取模板内容（不依赖 templateId）
   */
  const getTemplateContentByName = useCallback(
    async (templateName: string): Promise<string | null> => {
      if (unmountedRef.current) return null;

      // 如果模板列表为空，跳过缓存查找，直接请求
      if (templateListRef.current.length === 0) {
        // 去重：检查是否有正在进行的同名请求
        const existingByName = loadingByNameRef.current.get(templateName);
        if (existingByName) return existingByName;

        const loadPromise = (async () => {
          try {
            const response = await dynamicRenderApi.getTemplateContent(templateName);
            if (unmountedRef.current) return "";
            loadingByNameRef.current.delete(templateName);

            // 请求完成后，尝试回填 templateCacheRef
            // 此时模板列表可能已加载完成
            const template = templateListRef.current.find(
              (t) => t.templateName === templateName
            );
            if (template) {
              const compiled = Handlebars.compile(response.content);
              templateCacheRef.current.set(template.templateId, {
                content: response.content,
                compiled,
              });
            }

            return response.content;
          } catch (err) {
            loadingByNameRef.current.delete(templateName);
            console.error(`加载模板 ${templateName} 失败:`, err);
            throw err;
          }
        })();

        loadingByNameRef.current.set(templateName, loadPromise);
        return loadPromise;
      }

      const template = templateListRef.current.find(
        (t) => t.templateName === templateName
      );
      const cacheKey = template?.templateId;

      if (cacheKey !== undefined) {
        const cached = templateCacheRef.current.get(cacheKey);
        if (cached) return cached.content;

        const existingPromise = loadingPromiseRef.current.get(cacheKey);
        if (existingPromise) return existingPromise;
      }

      const loadPromise = (async () => {
        try {
          const response = await dynamicRenderApi.getTemplateContent(templateName);
          if (unmountedRef.current) return "";
          const compiled = Handlebars.compile(response.content);
          if (cacheKey !== undefined) {
            templateCacheRef.current.set(cacheKey, { content: response.content, compiled });
            loadingPromiseRef.current.delete(cacheKey);
          }
          return response.content;
        } catch (err) {
          if (cacheKey !== undefined) loadingPromiseRef.current.delete(cacheKey);
          console.error(`加载模板 ${templateName} 失败:`, err);
          throw err;
        }
      })();

      if (cacheKey !== undefined) {
        loadingPromiseRef.current.set(cacheKey, loadPromise);
      }
      return loadPromise;
    },
    []
  );

  /**
   * 直接用 templateName 渲染模板
   */
  const renderTemplateByName = useCallback(
    async (templateName: string, data: Record<string, any>): Promise<string | null> => {
      try {
        const content = await getTemplateContentByName(templateName);
        if (!content) {
          throw new Error(`模板 ${templateName} 加载失败`);
        }
        const compiled = Handlebars.compile(content);
        return compiled(data);
      } catch (err) {
        console.error(`渲染模板 ${templateName} 失败:`, err);
        return null;
      }
    },
    [getTemplateContentByName]
  );

  // 使用 useMemo 优化 Context value，避免不必要的重渲染
  const value = useMemo(
    () => ({
      isTemplateListLoaded,
      error,
      initialize,
      getTemplateContent,
      getTemplateIdByName,
      isStaticTemplate,
      renderTemplate,
      renderStaticTemplate,
      getTemplateContentByName,
      renderTemplateByName,
      templateList: templateListRef,
    }),
    [
      isTemplateListLoaded,
      error,
      initialize,
      getTemplateContent,
      getTemplateIdByName,
      isStaticTemplate,
      renderTemplate,
      renderStaticTemplate,
      getTemplateContentByName,
      renderTemplateByName,
    ]
  );

  return (
    <DynamicRenderContext.Provider value={value}>
      {children}
    </DynamicRenderContext.Provider>
  );
}

export function useDynamicRender() {
  return useContext(DynamicRenderContext);
}
