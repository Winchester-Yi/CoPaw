/**
 * ============================================================
 * 品牌主题配置
 * Author: Kun He
 * Date: 2026-04-07
 * ============================================================
 *
 * 根据 iframe 传入的 source 参数加载不同的品牌主题配置
 * 包括 logo、品牌名、主题色、favicon 等
 *
 * 使用方式：
 * 1. 在 BRAND_THEMES 中添加新的品牌配置
 * 2. 将 logo 文件放置在 public/brands/{source}/ 目录下
 * 3. iframe 传入 source 参数即可自动切换
 *
 * 相关文件：
 * - contexts/BrandThemeContext.tsx: 主题 Context
 * - stores/iframeStore.ts: iframe 参数存储
 * ============================================================
 */

/**
 * 品牌主题配置接口
 */
export interface BrandThemeConfig {
  /** 来源标识（与 iframe 传入的 source 对应） */
  source: string;
  /** 品牌名称（用于页面标题、登录页等） */
  brandName: string;
  /** Logo URL - 明亮模式（相对于 public 目录） */
  logo: string;
  /** Logo URL - 暗黑模式（相对于 public 目录） */
  darkLogo: string;
  /** 主题色（Ant Design primary color） */
  primaryColor: string;
  /** Favicon URL（相对于 public 目录） */
  favicon?: string;
  /** Welcome 页面头像（聊天欢迎页的 avatar） */
  avatar?: string;
  /** 登录页背景图（可选） */
  loginBackground?: string;
  /** 其他自定义配置 */
  custom?: Record<string, unknown>;
}

/**
 * 默认主题配置 (CoPaw)
 */
export const DEFAULT_THEME: BrandThemeConfig = {
  source: "default",
  brandName: "金葵花RM小助Claw版",
  logo: "/logo.png",
  darkLogo: "/logo.png",
  primaryColor: "#FF7F16",
  favicon: "/swe-symbol.png",
  avatar: "/logo-center.png",
};

/**
 * 品牌主题配置映射
 *
 * 添加新品牌：
 * 1. 在 public/brands/{source}/ 目录下放置 logo.png, dark-logo.png, favicon.ico
 * 2. 在下方添加配置项
 */
export const BRAND_THEMES: Record<string, BrandThemeConfig> = {
  default: DEFAULT_THEME,

  ruice: {
    source: "ruice",
    brandName: "睿策小助Claw版",
    // TODO: 合并代码后修改为: /logo-ruice.png
    logo: "/logo.png",
    darkLogo: "/logo.png",
    primaryColor: "#FF7F16", //睿策
    favicon: "/swe-symbol.png",
    avatar: "/logo-center.png",
  },

  CMSJY: {
    source: "CMSJY",
    brandName: "远程RM小助Claw版",
    // TODO: 合并代码后修改为: /logo-cms.png
    logo: "/logo.png",
    darkLogo: "/logo.png",
    primaryColor: "#FF7F16", // 远程RM
    favicon: "/swe-symbol.png",
    avatar: "/logo-center.png",
  },

  UPPCLAW: {
    source: "UPPCLAW",
    brandName: "智像小助Claw",
    // TODO: 合并代码后修改为: /logo-zx.png
    logo: "/logo.png",
    darkLogo: "/logo.png",
    primaryColor: "#FF7F16", // 智像
    favicon: "/swe-symbol.png",
    avatar: "/logo-center.png",
  },

  copilotClaw: {
    source: "copilotClaw",
    brandName: "数据赋能小助CLAW",
    // TODO: 合并代码后修改为: /logo-copilot.png
    logo: "/logo.png",
    darkLogo: "/logo.png",
    primaryColor: "#FF7F16", // 数据赋能
    favicon: "/swe-symbol.png",
    avatar: "/logo-center.png",
  },
};

/**
 * 根据 source 获取主题配置
 *
 * @param source - 来源标识（来自 iframe）
 * @returns 品牌主题配置
 */
export function getBrandTheme(source?: string | null): BrandThemeConfig {
  if (!source) return DEFAULT_THEME;
  return BRAND_THEMES[source] || DEFAULT_THEME;
}

/**
 * 获取所有可用的 source 列表
 */
export function getAvailableSources(): string[] {
  return Object.keys(BRAND_THEMES);
}