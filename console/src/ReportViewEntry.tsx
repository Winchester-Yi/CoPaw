import { useEffect } from "react";
import AuthGuard from "./access/AuthGuard";
import {
  DynamicRenderProvider,
  useDynamicRender,
} from "./components/agentscope-chat/DynamicRenderContext";
import ReportView from "./pages/ReportView";

function ReportContent() {
  const { initialize } = useDynamicRender();

  useEffect(() => {
    void initialize({ skipPreload: true });
  }, [initialize]);

  return <ReportView />;
}

export default function ReportViewEntry() {
  return (
    <AuthGuard>
      <DynamicRenderProvider>
        <ReportContent />
      </DynamicRenderProvider>
    </AuthGuard>
  );
}
