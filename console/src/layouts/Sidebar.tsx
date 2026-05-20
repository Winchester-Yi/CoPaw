import {
  Layout,
  Menu,
  Button,
  Modal,
  Input,
  Form,
  Tooltip,
  type MenuProps,
} from "antd";
import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "../hooks/useAppMessage";
// ==================== 选择智能体 (Kun He) - 已注释 ====================
// import AgentSelector from "../components/AgentSelector";
// ==================== 选择智能体结束 (Kun He) - 已注释 ====================
import {
  SparkChatTabFill,
  SparkWifiLine,
  SparkUserGroupLine,
  SparkDateLine,
  SparkVoiceChat01Line,
  SparkMagicWandLine,
  SparkLocalFileLine,
  SparkModePlazaLine,
  SparkInternetLine,
  SparkModifyLine,
  SparkBrowseLine,
  SparkMcpMcpLine,
  SparkToolLine,
  SparkDataLine,
  SparkMicLine,
  SparkAgentLine,
  SparkExitFullscreenLine,
  SparkSearchUserLine,
  SparkMenuExpandLine,
  SparkMenuFoldLine,
  SparkOtherLine,
  SparkBarChartLine,
  SparkMessageLine,
  SparkSearchLine,
  SparkFileTxtLine,
  SparkDevicesLine,
  SparkAdvancedMonitoringLine,
  SparkAuditLogLine,
  SparkRefreshLine,
} from "@agentscope-ai/icons";
import { Store, Wrench, Puzzle } from "lucide-react";
import { clearAuthToken } from "../api/config";
import { authApi } from "../api/modules/auth";
import styles from "./index.module.less";
import { useTheme } from "../contexts/ThemeContext";
import { KEY_TO_PATH, DEFAULT_OPEN_KEYS } from "./constants";

// ── Layout ────────────────────────────────────────────────────────────────

const { Sider } = Layout;

// ── Types ─────────────────────────────────────────────────────────────────

interface SidebarProps {
  selectedKey: string;
}

// ── Sidebar ───────────────────────────────────────────────────────────────

export default function Sidebar({ selectedKey }: SidebarProps) {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { isDark } = useTheme();
  const [authEnabled, setAuthEnabled] = useState(false);
  const [accountModalOpen, setAccountModalOpen] = useState(false);
  const [accountLoading, setAccountLoading] = useState(false);
  const [accountForm] = Form.useForm();
  const [collapsed, setCollapsed] = useState(false);

  // ── Effects ──────────────────────────────────────────────────────────────

  useEffect(() => {
    authApi
      .getStatus()
      .then((res) => setAuthEnabled(res.enabled))
      .catch(() => {});
  }, []);

  // ── Handlers ──────────────────────────────────────────────────────────────

  const handleUpdateProfile = async (values: {
    currentPassword: string;
    newUsername?: string;
    newPassword?: string;
  }) => {
    const trimmedUsername = values.newUsername?.trim() || undefined;
    const trimmedPassword = values.newPassword?.trim() || undefined;

    if (values.newPassword && !trimmedPassword) {
      message.error(t("account.passwordEmpty"));
      return;
    }

    if (values.newUsername && !trimmedUsername) {
      message.error(t("account.usernameEmpty"));
      return;
    }

    if (!trimmedUsername && !trimmedPassword) {
      message.warning(t("account.nothingToUpdate"));
      return;
    }

    setAccountLoading(true);
    try {
      await authApi.updateProfile(
        values.currentPassword,
        trimmedUsername,
        trimmedPassword,
      );
      message.success(t("account.updateSuccess"));
      setAccountModalOpen(false);
      accountForm.resetFields();
      clearAuthToken();
      window.location.href = "/login";
    } catch (err: unknown) {
      const raw = err instanceof Error ? err.message : "";
      let msg = t("account.updateFailed");
      if (raw.includes("password is incorrect")) {
        msg = t("account.wrongPassword");
      } else if (raw.includes("Nothing to update")) {
        msg = t("account.nothingToUpdate");
      } else if (raw.includes("cannot be empty")) {
        msg = t("account.nothingToUpdate");
      } else if (raw) {
        msg = raw;
      }
      message.error(msg);
    } finally {
      setAccountLoading(false);
    }
  };

  // ── Collapsed nav items (all leaf pages) ──────────────────────────────

  const collapsedNavItems = [
    {
      key: "chat",
      icon: <SparkChatTabFill size={18} />,
      path: "/chat",
      label: t("nav.chat"),
    },
    {
      key: "channels",
      icon: <SparkWifiLine size={18} />,
      path: "/channels",
      label: t("nav.channels"),
    },
    {
      key: "sessions",
      icon: <SparkUserGroupLine size={18} />,
      path: "/sessions",
      label: t("nav.sessions"),
    },
    {
      key: "cron-jobs",
      icon: <SparkDateLine size={18} />,
      path: "/cron-jobs",
      label: t("nav.cronJobs"),
    },
    {
      key: "heartbeat",
      icon: <SparkVoiceChat01Line size={18} />,
      path: "/heartbeat",
      label: t("nav.heartbeat"),
    },
    // {
    //   key: "greeting-management",
    //   icon: <SparkMessageLine size={18} />,
    //   path: "/greeting-management",
    //   label: t("nav.greetingManagement", "引导文案管理"),
    // },
    {
      key: "featured-cases-management",
      icon: <SparkFileTxtLine size={18} />,
      path: "/featured-cases-management",
      label: t("nav.featuredCasesManagement", "精选案例管理"),
    },
    {
      key: "workspace",
      icon: <SparkLocalFileLine size={18} />,
      path: "/workspace",
      label: t("nav.workspace"),
    },
    {
      key: "skills",
      icon: <SparkMagicWandLine size={18} />,
      path: "/skills",
      label: t("nav.skills"),
    },
    {
      key: "skill-pool",
      icon: <SparkOtherLine size={18} />,
      path: "/skill-pool",
      label: t("nav.skillPool", "Skill Pool"),
    },
    {
      key: "tools",
      icon: <SparkToolLine size={18} />,
      path: "/tools",
      label: t("nav.tools"),
    },
    {
      key: "mcp",
      icon: <SparkMcpMcpLine size={18} />,
      path: "/mcp",
      label: t("nav.mcp"),
    },
    {
      key: "agent-config",
      icon: <SparkModifyLine size={18} />,
      path: "/agent-config",
      label: t("nav.agentConfig"),
    },
    {
      key: "agents",
      icon: <SparkAgentLine size={18} />,
      path: "/agents",
      label: t("nav.agents"),
    },
    {
      key: "models",
      icon: <SparkModePlazaLine size={18} />,
      path: "/models",
      label: t("nav.models"),
    },
    {
      key: "environments",
      icon: <SparkInternetLine size={18} />,
      path: "/environments",
      label: t("nav.environments"),
    },
    {
      key: "security",
      icon: <SparkBrowseLine size={18} />,
      path: "/security",
      label: t("nav.security"),
    },
    // {
    //   key: "token-usage",
    //   icon: <SparkDataLine size={18} />,
    //   path: "/token-usage",
    //   label: t("nav.tokenUsage"),
    // },
    // {
    //   key: "voice-transcription",
    //   icon: <SparkMicLine size={18} />,
    //   path: "/voice-transcription",
    //   label: t("nav.voiceTranscription"),
    // },
    {
      key: "analytics-users",
      icon: <SparkUserGroupLine size={18} />,
      path: "/analytics/users",
      label: t("nav.analyticsUsers", "Users"),
    },
    {
      key: "analytics-sessions",
      icon: <SparkMessageLine size={18} />,
      path: "/analytics/sessions",
      label: t("nav.analyticsSessions", "Sessions"),
    },
    {
      key: "analytics-messages",
      icon: <SparkSearchLine size={18} />,
      path: "/analytics/messages",
      label: t("nav.analyticsMessages", "Messages"),
    },
    {
      key: "analytics-traces",
      icon: <SparkFileTxtLine size={18} />,
      path: "/analytics/traces",
      label: t("nav.analyticsTraces", "Traces"),
    },
    {
      key: "analytics-business-overview",
      icon: <SparkBarChartLine size={18} />,
      path: "/analytics/business-overview",
      label: t("nav.analyticsBusinessOverview", "运营看板"),
    },
    {
      key: "monitor-cron-overview",
      icon: <SparkAdvancedMonitoringLine size={18} />,
      path: "/monitor/cron-overview",
      label: t("nav.monitorCronOverview", "定时任务概览"),
    },
    {
      key: "instance-overview",
      icon: <SparkAdvancedMonitoringLine size={18} />,
      path: "/instance/overview",
      label: t("nav.instanceOverview", "Overview"),
    },
    {
      key: "instance-instances",
      icon: <SparkDevicesLine size={18} />,
      path: "/instance/instances",
      label: t("nav.instanceInstances", "Instances"),
    },
    {
      key: "instance-allocations",
      icon: <SparkOtherLine size={18} />,
      path: "/instance/allocations",
      label: t("nav.instanceAllocations", "Allocations"),
    },
    {
      key: "instance-operation-logs",
      icon: <SparkAuditLogLine size={18} />,
      path: "/instance/operation-logs",
      label: t("nav.instanceOperationLogs", "Operation Logs"),
    },
    {
      key: "continuous-iteration",
      icon: <SparkRefreshLine size={18} />,
      path: "/continuous-iteration",
      label: t("nav.continuousIteration", "持续迭代"),
    },
    {
      key: "market",
      icon: <Store size={18} />,
      path: "/market",
      label: t("nav.market"),
    },
    {
      key: "my-skills",
      icon: <Wrench size={18} />,
      path: "/my-skills",
      label: t("nav.mySkills"),
    },
    {
      key: "my-mcp",
      icon: <Puzzle size={18} />,
      path: "/my-mcp",
      label: t("nav.myMcp"),
    },
  ];

  // ── Menu items ────────────────────────────────────────────────────────────

  const menuItems: MenuProps["items"] = [
    {
      key: "chat",
      label: collapsed ? null : t("nav.chat"),
      icon: <SparkChatTabFill size={16} />,
    },
    {
      key: "control-group",
      label: collapsed ? null : t("nav.control"),
      children: [
        {
          key: "channels",
          label: collapsed ? null : t("nav.channels"),
          icon: <SparkWifiLine size={16} />,
        },
        {
          key: "sessions",
          label: collapsed ? null : t("nav.sessions"),
          icon: <SparkUserGroupLine size={16} />,
        },
        {
          key: "cron-jobs",
          label: collapsed ? null : t("nav.cronJobs"),
          icon: <SparkDateLine size={16} />,
        },
        {
          key: "heartbeat",
          label: collapsed ? null : t("nav.heartbeat"),
          icon: <SparkVoiceChat01Line size={16} />,
        },
        // {
        //   key: "greeting-management",
        //   label: collapsed ? null : t("nav.greetingManagement", "引导文案管理"),
        //   icon: <SparkMessageLine size={16} />,
        // },
        {
          key: "featured-cases-management",
          label: collapsed ? null : t("nav.featuredCasesManagement", "精选案例管理"),
          icon: <SparkFileTxtLine size={16} />,
        },
      ],
    },
    {
      key: "agent-group",
      label: collapsed ? null : t("nav.agent"),
      children: [
        {
          key: "workspace",
          label: collapsed ? null : t("nav.workspace"),
          icon: <SparkLocalFileLine size={16} />,
        },
        {
          key: "skills",
          label: collapsed ? null : t("nav.skills"),
          icon: <SparkMagicWandLine size={16} />,
        },
        {
          key: "tools",
          label: collapsed ? null : t("nav.tools"),
          icon: <SparkToolLine size={16} />,
        },
        {
          key: "mcp",
          label: collapsed ? null : t("nav.mcp"),
          icon: <SparkMcpMcpLine size={16} />,
        },
        {
          key: "agent-config",
          label: collapsed ? null : t("nav.agentConfig"),
          icon: <SparkModifyLine size={16} />,
        },
      ],
    },
    {
      key: "settings-group",
      label: collapsed ? null : t("nav.settings"),
      children: [
        {
          key: "agents",
          label: collapsed ? null : t("nav.agents"),
          icon: <SparkAgentLine size={16} />,
        },
        {
          key: "models",
          label: collapsed ? null : t("nav.models"),
          icon: <SparkModePlazaLine size={16} />,
        },
        {
          key: "skill-pool",
          label: collapsed ? null : t("nav.skillPool", "Skill Pool"),
          icon: <SparkOtherLine size={16} />,
        },
        {
          key: "environments",
          label: collapsed ? null : t("nav.environments"),
          icon: <SparkInternetLine size={16} />,
        },
        {
          key: "system-config",
          label: collapsed ? null : t("nav.systemConfig"),
          icon: <SparkModifyLine size={16} />,
        },
        {
          key: "security",
          label: collapsed ? null : t("nav.security"),
          icon: <SparkBrowseLine size={16} />,
        },
        // {
        //   key: "token-usage",
        //   label: collapsed ? null : t("nav.tokenUsage"),
        //   icon: <SparkDataLine size={16} />,
        // },
        // {
        //   key: "voice-transcription",
        //   label: collapsed ? null : t("nav.voiceTranscription"),
        //   icon: <SparkMicLine size={16} />,
        // },
      ],
    },
    {
      key: "analytics-group",
      label: collapsed ? null : t("nav.analytics", "Analytics"),
      children: [
        {
          key: "analytics-business-overview",
          label: collapsed
            ? null
            : t("nav.analyticsBusinessOverview", "运营看板"),
          icon: <SparkBarChartLine size={16} />,
        },
        {
          key: "analytics-users",
          label: collapsed ? null : t("nav.analyticsUsers", "Users"),
          icon: <SparkUserGroupLine size={16} />,
        },
        {
          key: "analytics-sessions",
          label: collapsed ? null : t("nav.analyticsSessions", "Sessions"),
          icon: <SparkMessageLine size={16} />,
        },
        {
          key: "analytics-messages",
          label: collapsed ? null : t("nav.analyticsMessages", "Messages"),
          icon: <SparkSearchLine size={16} />,
        },
        {
          key: "analytics-traces",
          label: collapsed ? null : t("nav.analyticsTraces", "Traces"),
          icon: <SparkFileTxtLine size={16} />,
        },
        {
          key: "monitor-cron-overview",
          label: collapsed ? null : t("nav.monitorCronOverview", "定时任务概览"),
          icon: <SparkAdvancedMonitoringLine size={16} />,
        },
      ],
    },
    {
      key: "harness-group",
      label: collapsed ? null : t("nav.harness", "Harness"),
      children: [
        {
          key: "continuous-iteration",
          label: collapsed ? null : t("nav.continuousIteration", "持续迭代"),
          icon: <SparkRefreshLine size={16} />,
        },
      ],
    },
    {
      key: "market-group",
      label: collapsed ? null : t("nav.marketGroup"),
      children: [
        {
          key: "market",
          label: collapsed ? null : t("nav.market"),
          icon: <Store size={16} />,
        },
        {
          key: "my-skills",
          label: collapsed ? null : t("nav.mySkills"),
          icon: <Wrench size={16} />,
        },
        {
          key: "my-mcp",
          label: collapsed ? null : t("nav.myMcp"),
          icon: <Puzzle size={16} />,
        },
      ],
    },
    // {
    //   key: "instance-group",
    //   label: collapsed ? null : t("nav.instance", "Instance"),
    //   children: [
    //     {
    //       key: "instance-overview",
    //       label: collapsed ? null : t("nav.instanceOverview", "Overview"),
    //       icon: <SparkAdvancedMonitoringLine size={16} />,
    //     },
    //     {
    //       key: "instance-instances",
    //       label: collapsed ? null : t("nav.instanceInstances", "Instances"),
    //       icon: <SparkDevicesLine size={16} />,
    //     },
    //     {
    //       key: "instance-allocations",
    //       label: collapsed ? null : t("nav.instanceAllocations", "Allocations"),
    //       icon: <SparkOtherLine size={16} />,
    //     },
    //     {
    //       key: "instance-operation-logs",
    //       label: collapsed
    //         ? null
    //         : t("nav.instanceOperationLogs", "Operation Logs"),
    //       icon: <SparkAuditLogLine size={16} />,
    //     },
    //   ],
    // },
  ];

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <Sider
      width={collapsed ? 72 : 240}
      className={`${styles.sider}${
        collapsed ? ` ${styles.siderCollapsed}` : ""
      }${isDark ? ` ${styles.siderDark}` : ""}`}
    >
      {/* ==================== 选择智能体 (Kun He) - 已注释 ==================== */}
      {/* <div className={styles.agentSelectorContainer}>
        <AgentSelector collapsed={collapsed} />
      </div> */}
      {/* ==================== 选择智能体结束 (Kun He) - 已注释 ==================== */}
      {collapsed ? (
        <nav className={styles.collapsedNav}>
          {collapsedNavItems.map((item) => {
            const isActive = selectedKey === item.key;
            return (
              <Tooltip
                key={item.key}
                title={item.label}
                placement="right"
                overlayInnerStyle={{
                  background: "rgba(0,0,0,0.75)",
                  color: "#fff",
                }}
              >
                <button
                  className={`${styles.collapsedNavItem} ${
                    isActive ? styles.collapsedNavItemActive : ""
                  }`}
                  onClick={() => navigate(item.path)}
                >
                  {item.icon}
                </button>
              </Tooltip>
            );
          })}
        </nav>
      ) : (
        <Menu
          mode="inline"
          selectedKeys={[selectedKey]}
          openKeys={DEFAULT_OPEN_KEYS}
          onClick={({ key }) => {
            const path = KEY_TO_PATH[String(key)];
            if (path) navigate(path);
          }}
          items={menuItems}
          theme={isDark ? "dark" : "light"}
          className={styles.sideMenu}
        />
      )}

      {authEnabled && !collapsed && (
        <div className={styles.authActions}>
          <Button
            type="text"
            icon={<SparkSearchUserLine size={16} />}
            onClick={() => {
              accountForm.resetFields();
              setAccountModalOpen(true);
            }}
            block
            className={`${styles.authBtn} ${
              collapsed ? styles.authBtnCollapsed : ""
            }`}
          >
            {!collapsed && t("account.title")}
          </Button>
          <Button
            type="text"
            icon={<SparkExitFullscreenLine size={16} />}
            onClick={() => {
              clearAuthToken();
              window.location.href = "/login";
            }}
            block
            className={`${styles.authBtn} ${
              collapsed ? styles.authBtnCollapsed : ""
            }`}
          >
            {!collapsed && t("login.logout")}
          </Button>
        </div>
      )}

      <div className={styles.collapseToggleContainer}>
        <Button
          type="text"
          icon={
            collapsed ? (
              <SparkMenuExpandLine size={20} />
            ) : (
              <SparkMenuFoldLine size={20} />
            )
          }
          onClick={() => setCollapsed(!collapsed)}
          className={styles.collapseToggle}
        />
      </div>

      <Modal
        open={accountModalOpen}
        onCancel={() => setAccountModalOpen(false)}
        title={t("account.title")}
        footer={null}
        destroyOnHidden
        centered
      >
        <Form
          form={accountForm}
          layout="vertical"
          onFinish={handleUpdateProfile}
        >
          <Form.Item
            name="currentPassword"
            label={t("account.currentPassword")}
            rules={[
              { required: true, message: t("account.currentPasswordRequired") },
            ]}
          >
            <Input.Password />
          </Form.Item>
          <Form.Item name="newUsername" label={t("account.newUsername")}>
            <Input placeholder={t("account.newUsernamePlaceholder")} />
          </Form.Item>
          <Form.Item name="newPassword" label={t("account.newPassword")}>
            <Input.Password placeholder={t("account.newPasswordPlaceholder")} />
          </Form.Item>
          <Form.Item
            name="confirmPassword"
            label={t("account.confirmPassword")}
            dependencies={["newPassword"]}
            rules={[
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value && !getFieldValue("newPassword")) {
                    return Promise.resolve();
                  }
                  if (value === getFieldValue("newPassword")) {
                    return Promise.resolve();
                  }
                  return Promise.reject(
                    new Error(t("account.passwordMismatch")),
                  );
                },
              }),
            ]}
          >
            <Input.Password
              placeholder={t("account.confirmPasswordPlaceholder")}
            />
          </Form.Item>
          <Form.Item>
            <Button
              type="primary"
              htmlType="submit"
              loading={accountLoading}
              block
            >
              {t("account.save")}
            </Button>
          </Form.Item>
        </Form>
      </Modal>
    </Sider>
  );
}
