import { createGlobalStyle } from "antd-style";
import { ConfigProvider, bailianTheme } from "@agentscope-ai/design";
import { App as AntdApp } from "antd";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { lazy, Suspense } from "react";
import {
  FileText,
  LayoutDashboard,
  PanelTop,
  type LucideIcon,
} from "lucide-react";
import zhCN from "antd/locale/zh_CN";
import { theme as antdTheme } from "antd";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import "dayjs/locale/zh-cn";
dayjs.extend(relativeTime);
dayjs.locale("zh-cn");
import { ThemeProvider, useTheme } from "./contexts/ThemeContext";
// ==================== 品牌主题 (Kun He) ====================
import {
  BrandThemeProvider,
  useBrandTheme,
} from "./contexts/BrandThemeContext";
// ==================== 品牌主题结束 ====================
import "./styles/layout.css";
import "./styles/form-override.css";
import "./styles/console-theme.css";
import styles from "./App.module.css";

const WealthWorkbench = lazy(() => import("./pages/WealthWorkbench"));
const ReportViewEntry = lazy(() => import("./ReportViewEntry"));
const ConsoleRoutes = lazy(() => import("./ConsoleRoutes"));

const GlobalStyle = createGlobalStyle`
* {
  margin: 0;
  box-sizing: border-box;
}
`;

function getRouterBasename(pathname: string): string | undefined {
  return /^\/console(?:\/|$)/.test(pathname) ? "/console" : undefined;
}

function RouteLoadingStatus({
  label,
  icon: Icon,
}: {
  label: string;
  icon: LucideIcon;
}) {
  return (
    <div className={styles.routeOpening}>
      <div className={styles.routeStatus} role="status">
        <div className={styles.routeIcon} aria-hidden="true">
          <Icon size={24} strokeWidth={1.6} focusable="false" />
        </div>
        {label}
        <div className={styles.routeProgress} aria-hidden="true">
          <span className={styles.routeProgressSegment} />
        </div>
      </div>
    </div>
  );
}

function AppInner() {
  const { isDark } = useTheme();

  // ==================== 品牌主题 (Kun He) ====================
  // 获取动态品牌配置，用于设置主题色
  const { theme: brandTheme } = useBrandTheme();
  // ==================== 品牌主题结束 ====================

  return (
    <>
      <GlobalStyle />
      <ConfigProvider
        {...bailianTheme}
        prefix="swe"
        prefixCls="swe"
        locale={zhCN}
        theme={{
          ...(bailianTheme as any)?.theme,
          algorithm: isDark
            ? antdTheme.darkAlgorithm
            : antdTheme.defaultAlgorithm,
          token: {
            // ==================== 品牌主题 (Kun He) ====================
            // 使用动态品牌主题色
            colorPrimary: brandTheme.primaryColor,
            // 确保浅色主题下 primary button 字体为白色
            colorTextOnPrimary: "#ffffff",
            // ==================== 品牌主题结束 ====================
          },
        }}
      >
        <AntdApp>
          <Routes>
            <Route
              path="/wealth/*"
              element={
                <Suspense
                  fallback={
                    <RouteLoadingStatus
                      label="正在打开工作台"
                      icon={LayoutDashboard}
                    />
                  }
                >
                  <WealthWorkbench />
                </Suspense>
              }
            />
            <Route
              path="/reportView"
              element={
                <Suspense
                  fallback={
                    <RouteLoadingStatus label="正在打开报告" icon={FileText} />
                  }
                >
                  <ReportViewEntry />
                </Suspense>
              }
            />
            <Route
              path="/*"
              element={
                <Suspense
                  fallback={
                    <RouteLoadingStatus label="正在打开页面" icon={PanelTop} />
                  }
                >
                  <ConsoleRoutes />
                </Suspense>
              }
            />
          </Routes>
        </AntdApp>
      </ConfigProvider>
    </>
  );
}

function App() {
  const basename = getRouterBasename(window.location.pathname);
  // ==================== 外部系统 Token 针权 ====================
  // Token 已在 main.tsx 中获取完成，此处无需等待
  // ==================== 外部系统 Token 针权结束 ====================

  return (
    <ThemeProvider>
      {/* ==================== 品牌主题 (Kun He) ==================== */}
      {/* 包裹 BrandThemeProvider，根据 source 动态切换品牌配置 */}
      <BrowserRouter basename={basename}>
        <BrandThemeProvider>
          <AppInner />
        </BrandThemeProvider>
      </BrowserRouter>
      {/* ==================== 品牌主题结束 ==================== */}
    </ThemeProvider>
  );
}

export default App;
