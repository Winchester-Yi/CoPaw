import { Routes, Route } from "react-router-dom";
import AuthGuard from "./access/AuthGuard";
import MainLayout from "./layouts/MainLayout";
import ChatSharePage from "./pages/ChatShare";
import LoginPage from "./pages/Login";
import { DynamicRenderProvider } from "./components/agentscope-chat/DynamicRenderContext";

// Keep Console pages and the chat template engine behind the non-Wealth route.
export default function ConsoleRoutes() {
  return (
    <DynamicRenderProvider>
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
    </DynamicRenderProvider>
  );
}
