import { createGlobalStyle } from "antd-style";
import { ConfigProvider, bailianTheme } from "@agentscope-ai/design";
import { App as AntdApp } from "antd";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { useEffect, useState } from "react";
import zhCN from "antd/locale/zh_CN";
import { theme as antdTheme } from "antd";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import "dayjs/locale/zh-cn";
dayjs.extend(relativeTime);
dayjs.locale("zh-cn");
import MainLayout from "./layouts/MainLayout";
import ChatSharePage from "./pages/ChatShare";
import { ThemeProvider, useTheme } from "./contexts/ThemeContext";
// ==================== 品牌主题 (Kun He) ====================
import {
  BrandThemeProvider,
  useBrandTheme,
} from "./contexts/BrandThemeContext";
// ==================== 品牌主题结束 ====================
import LoginPage from "./pages/Login";
import { authApi } from "./api/modules/auth";
import { getApiUrl, getApiToken, clearAuthToken } from "./api/config";
import { buildAuthHeaders } from "./api/authHeaders";
import "./styles/layout.css";
import "./styles/form-override.css";
import "./styles/console-theme.css";
import { DynamicRenderProvider } from "./components/agentscope-chat/DynamicRenderContext";

const GlobalStyle = createGlobalStyle`
* {
  margin: 0;
  box-sizing: border-box;
}
`;

function AuthGuard({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<"loading" | "auth-required" | "ok">(
    "loading",
  );

  useEffect(() => {
    // Token is already initialized at App level, no need to wait here
    let cancelled = false;
    (async () => {
      try {
        const res = await authApi.getStatus();
        if (cancelled) return;
        if (!res.enabled) {
          setStatus("ok");
          return;
        }
        const token = getApiToken();
        if (!token) {
          setStatus("auth-required");
          return;
        }
        try {
          const r = await fetch(getApiUrl("/auth/verify"), {
            headers: {
              Authorization: `Bearer ${token}`,
              ...buildAuthHeaders(),
            },
          });
          if (cancelled) return;
          if (r.ok) {
            setStatus("ok");
          } else {
            clearAuthToken();
            setStatus("auth-required");
          }
        } catch {
          if (!cancelled) {
            clearAuthToken();
            setStatus("auth-required");
          }
        }
      } catch {
        if (!cancelled) setStatus("ok");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (status === "loading") return null;
  if (status === "auth-required")
    return (
      <Navigate
        to={`/login?redirect=${encodeURIComponent(window.location.pathname)}`}
        replace
      />
    );
  return <>{children}</>;
}

function getRouterBasename(pathname: string): string | undefined {
  return /^\/console(?:\/|$)/.test(pathname) ? "/console" : undefined;
}

function AppInner() {
  const basename = getRouterBasename(window.location.pathname);
  const { isDark } = useTheme();

  // ==================== 品牌主题 (Kun He) ====================
  // 获取动态品牌配置，用于设置主题色
  const { theme: brandTheme } = useBrandTheme();
  // ==================== 品牌主题结束 ====================

  return (
    <BrowserRouter basename={basename}>
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
            <Route path="/login" element={<LoginPage />} />
            <Route path="/chat-share/:token" element={<ChatSharePage />} />
            <Route
              path="/*"
              element={
                <AuthGuard>
                  <MainLayout />
                </AuthGuard>
              }
            />
          </Routes>
        </AntdApp>
      </ConfigProvider>
    </BrowserRouter>
  );
}

function App() {
  // ==================== 外部系统 Token 针权 ====================
  // Token 已在 main.tsx 中获取完成，此处无需等待
  // ==================== 外部系统 Token 针权结束 ====================

  return (
    <ThemeProvider>
      <DynamicRenderProvider>
      {/* ==================== 品牌主题 (Kun He) ==================== */}
      {/* 包裹 BrandThemeProvider，根据 source 动态切换品牌配置 */}
      <BrandThemeProvider>
        <AppInner />
      </BrandThemeProvider>
      </DynamicRenderProvider>
      {/* ==================== 品牌主题结束 ==================== */}
    </ThemeProvider>
  );
}

export default App;
