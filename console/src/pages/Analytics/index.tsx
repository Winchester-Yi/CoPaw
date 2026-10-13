import { Routes, Route, Navigate, useLocation } from "react-router-dom";
import UsersPage from "./Users";
import SessionsPage from "./Sessions";
import MessagesPage from "./Messages";
import TracesPage from "./Traces";
import BusinessOverviewPage from "./BusinessOverview";
import CronJobOverviewPage from "./CronJobOverview";
import ClawDataOverviewPage from "./ClawDataOverview";
import ContinuousGovernancePage from "./ContinuousGovernance";
import TokenDiagnosticsPage from "./TokenDiagnostics";
import TokenOverviewPage from "./TokenOverview";

export default function AnalyticsPage() {
  const location = useLocation();
  return (
    <Routes>
      <Route path="/" element={<Navigate to="business-overview" replace />} />
      <Route path="users" element={<UsersPage />} />
      <Route path="sessions" element={<SessionsPage />} />
      <Route path="messages" element={<MessagesPage />} />
      <Route path="traces" element={<TracesPage />} />
      <Route path="token-diagnostics" element={<TokenDiagnosticsPage />} />
      <Route path="token-overview" element={<TokenOverviewPage />} />
      <Route
        path="token-executions"
        element={
          <Navigate
            to={{
              pathname: "/analytics/token-overview",
              search: location.search,
              hash: "#task-list",
            }}
            replace
          />
        }
      />
      <Route path="business-overview" element={<BusinessOverviewPage />} />
      <Route path="claw-data-overview" element={<ClawDataOverviewPage />} />
      <Route path="cron-job-overview" element={<CronJobOverviewPage />} />
      <Route
        path="continuous-governance"
        element={<ContinuousGovernancePage />}
      />
    </Routes>
  );
}
