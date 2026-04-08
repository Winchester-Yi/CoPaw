/**
 * ============================================================
 * 品牌主题 Context
 * Author: Kun He
 * Date: 2026-04-07
 * ============================================================
 *
 * 提供品牌主题配置的 React Context
 * 根据 iframe 传入的 source 参数自动切换主题
 *
 * 使用方式：
 * ```tsx
 * import { useBrandTheme } from "../../contexts/BrandThemeContext";
 *
 * const { theme, source } = useBrandTheme();
 * console.log(theme.brandName, theme.logo, theme.primaryColor);
 * ```
 *
 * 相关文件：
 * - config/brandThemes.ts: 主题配置定义
 * - stores/iframeStore.ts: iframe 参数存储
 * ============================================================
 */

import { createContext, useContext, useEffect, useState } from "react";
import {
  getBrandTheme,
  type BrandThemeConfig,
} from "../config/brandThemes";
import { useIframeStore } from "../stores/iframeStore";

/**
 * 品牌主题 Context 值类型
 */
interface BrandThemeContextValue {
  /** 当前品牌主题配置 */
  theme: BrandThemeConfig;
  /** 当前来源标识 */
  source: string | null;
}

const BrandThemeContext = createContext<BrandThemeContextValue | null>(null);

/**
 * 品牌主题 Provider
 *
 * 应在 App.tsx 中包裹整个应用
 * 监听 iframeStore.source 变化，自动切换主题
 */
export function BrandThemeProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const source = useIframeStore((state) => state.source);
  const [theme, setTheme] = useState<BrandThemeConfig>(() => getBrandTheme(source));

  // 监听 source 变化，更新主题
  useEffect(() => {
    const newTheme = getBrandTheme(source);
    setTheme(newTheme);

    // ==================== 动态更新页面标题 ====================
    // 更新浏览器标签页标题
    document.title = newTheme.brandName;

    // 更新 favicon
    if (newTheme.favicon) {
      const faviconLink = document.querySelector(
        'link[rel="icon"]'
      ) as HTMLLinkElement;
      if (faviconLink) {
        faviconLink.href = newTheme.favicon;
      }
    }
    // ==================== 动态更新结束 ====================
  }, [source]);

  return (
    <BrandThemeContext.Provider value={{ theme, source }}>
      {children}
    </BrandThemeContext.Provider>
  );
}

/**
 * 获取品牌主题配置的 Hook
 *
 * @returns { theme: BrandThemeConfig, source: string | null }
 * @throws Error 如果不在 BrandThemeProvider 内使用
 */
export function useBrandTheme(): BrandThemeContextValue {
  const context = useContext(BrandThemeContext);
  if (!context) {
    throw new Error("useBrandTheme must be used within BrandThemeProvider");
  }
  return context;
}