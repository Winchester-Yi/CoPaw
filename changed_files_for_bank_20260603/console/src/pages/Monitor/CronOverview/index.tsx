import { useState, useEffect, useRef, useMemo } from "react";
import { useTranslation } from "react-i18next";
import {
  Tabs,
  Card,
  Table,
  Select,
  DatePicker,
  Button,
  Space,
  Tag,
  Drawer,
  Descriptions,
  message,
  Spin,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { DownloadOutlined, EyeOutlined, ReloadOutlined } from "@ant-design/icons";
import { PageHeader } from "@/components/PageHeader";
import {
  monitorApi,
  CronJobItem,
  ExecutionItem,
  FilterOption,
  FilterOptionsResponse,
} from "../../../api/modules/monitor";
import { BBK_ID_MAP, BBK_ID_TO_NAME_MAP } from "../../../constants/bbk";
import styles from "./index.module.less";

const { RangePicker } = DatePicker;


const EXEC_STATUS_COLORS: Record<string, string> = {
  success: "green",
  error: "red",
  cancelled: "orange",
  timeout: "orange",
  skipped: "default",
  running: "blue",
};


const EXEC_STATUS_LABELS: Record<string, string> = {
  success: "成功",
  error: "失败",
  cancelled: "取消",
  timeout: "超时",
  skipped: "跳过",
  running: "运行中",
};

// 时间范围选项
const TIME_RANGE_OPTIONS = [
  { value: "today", label: "今日" },
  { value: "week", label: "本周" },
  { value: "month", label: "本月" },
  { value: "last7days", label: "近7天" },
  { value: "last30days", label: "近30天" },
  { value: "custom", label: "自定义" },
];


const EXEC_STATUS_OPTIONS = [
  { value: "", label: "全部状态" },
  { value: "success", label: "成功" },
  { value: "error", label: "失败" },
  { value: "timeout", label: "超时" },
  { value: "cancelled", label: "取消" },
  { value: "skipped", label: "跳过" },
];

// 分行选项（来自前端常量）
const BBK_OPTIONS = [
  { value: "", label: "全部分行" },
  ...BBK_ID_MAP,
];

// 是否启用选项
const ENABLED_OPTIONS = [
  { value: "", label: "全部" },
  { value: "true", label: "已启用" },
  { value: "false", label: "已禁用" },
];

export default function CronOverviewPage() {
  const { t } = useTranslation();

  // 首次加载标记：控制是否自动触发查询
  const initialLoadDone = useRef(false);

  // Filter options (loaded from API - 仅用户从数据库查询)
  const [filterOptions, setFilterOptions] = useState<FilterOptionsResponse>({
    users: [],
    bbk_ids: [], // 不使用，分行来自前端常量
    channels: [], // 不使用
    source_ids: [],
    job_names: [],
    job_ids: [],
  });
  const [filterOptionsLoading, setFilterOptionsLoading] = useState(false);

  // Jobs state
  const [jobsLoading, setJobsLoading] = useState(false);
  const [jobs, setJobs] = useState<CronJobItem[]>([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobsPage, setJobsPage] = useState(1);
  const [jobsPageSize, setJobsPageSize] = useState(10);
  // Jobs filters (dropdown selects)
  const [jobsUserFilter, setJobsUserFilter] = useState<string>("");
  const [jobsBbkFilter, setJobsBbkFilter] = useState<string>("");
  const [jobsEnabledFilter, setJobsEnabledFilter] = useState<string>("true"); // 默认选中已启用

  // Executions state
  const [execsLoading, setExecsLoading] = useState(false);
  const [executions, setExecutions] = useState<ExecutionItem[]>([]);
  const [execsTotal, setExecsTotal] = useState(0);
  const [execsPage, setExecsPage] = useState(1);
  const [execsPageSize, setExecsPageSize] = useState(10);
  // Executions filters (dropdown selects)
  const [execsJobFilter, setExecsJobFilter] = useState<string>("");
  const [execsUserFilter, setExecsUserFilter] = useState<string>("");
  const [execsStatusFilter, setExecsStatusFilter] = useState<string>("");
  const [execsTimeRangeType, setExecsTimeRangeType] = useState<string>("today");
  const [execsCustomTimeRange, setExecsCustomTimeRange] = useState<[dayjs.Dayjs | null, dayjs.Dayjs | null]>([dayjs().subtract(7, "day"), dayjs()]);

  // Execution detail drawer
  const [detailDrawerOpen, setDetailDrawerOpen] = useState(false);
  const [selectedExecution, setSelectedExecution] = useState<ExecutionItem | null>(null);

  // 获取时间范围
  const getTimeRange = (
    rangeType: string,
    customRange?: [dayjs.Dayjs | null, dayjs.Dayjs | null],
  ): { start_time?: string; end_time?: string } => {
    const today = dayjs().startOf("day");
    switch (rangeType) {
      case "today":
        return {
          start_time: today.format("YYYY-MM-DDTHH:mm:ss"),
          end_time: today.endOf("day").format("YYYY-MM-DDTHH:mm:ss"),
        };
      case "week":
        return {
          start_time: today.subtract(6, "day").format("YYYY-MM-DDTHH:mm:ss"),
          end_time: today.endOf("day").format("YYYY-MM-DDTHH:mm:ss"),
        };
      case "month":
        return {
          start_time: today.subtract(29, "day").format("YYYY-MM-DDTHH:mm:ss"),
          end_time: today.endOf("day").format("YYYY-MM-DDTHH:mm:ss"),
        };
      case "last7days":
        return {
          start_time: today.subtract(6, "day").format("YYYY-MM-DDTHH:mm:ss"),
          end_time: today.endOf("day").format("YYYY-MM-DDTHH:mm:ss"),
        };
      case "last30days":
        return {
          start_time: today.subtract(29, "day").format("YYYY-MM-DDTHH:mm:ss"),
          end_time: today.endOf("day").format("YYYY-MM-DDTHH:mm:ss"),
        };
      case "custom":
        if (customRange && customRange[0] && customRange[1]) {
          return {
            start_time: customRange[0].format("YYYY-MM-DDTHH:mm:ss"),
            end_time: customRange[1].endOf("day").format("YYYY-MM-DDTHH:mm:ss"),
          };
        }
        return {};
      default:
        return {};
    }
  };

  // Fetch filter options
  const fetchFilterOptions = async () => {
    setFilterOptionsLoading(true);
    try {
      const options = await monitorApi.getFilterOptions();
      setFilterOptions(options);
    } catch (error) {
      console.error("Failed to fetch filter options:", error);
    } finally {
      setFilterOptionsLoading(false);
    }
  };

  // Fetch jobs
  const fetchJobs = async () => {
    setJobsLoading(true);
    try {
      const enabledValue = jobsEnabledFilter === "true" ? true : jobsEnabledFilter === "false" ? false : undefined;
      const result = await monitorApi.getJobs(jobsPage, jobsPageSize, {
        tenant_id: jobsUserFilter || undefined,
        bbk_id: jobsBbkFilter || undefined,
        enabled: enabledValue,
      });
      setJobs(result.items);
      setJobsTotal(result.total);
    } catch (error) {
      console.error("Failed to fetch jobs:", error);
      message.error("获取任务列表失败");
    } finally {
      setJobsLoading(false);
    }
  };

  // Fetch executions
  const fetchExecutions = async () => {
    setExecsLoading(true);
    try {
      const timeRange = getTimeRange(execsTimeRangeType, execsCustomTimeRange);
      const result = await monitorApi.getExecutions(execsPage, execsPageSize, {
        job_id: execsJobFilter || undefined,
        tenant_id: execsUserFilter || undefined,
        status: execsStatusFilter || undefined,
        ...timeRange,
      });
      setExecutions(result.items);
      setExecsTotal(result.total);
    } catch (error) {
      console.error("Failed to fetch executions:", error);
      message.error("获取执行历史失败");
    } finally {
      setExecsLoading(false);
    }
  };

  // Load filter options on mount
  useEffect(() => {
    fetchFilterOptions();
  }, []);

  // 首次加载逻辑
  useEffect(() => {
    // 如果已经完成首次加载，不再重复
    if (initialLoadDone.current) {
      return;
    }

    // 使用空筛选进行首次加载
    fetchJobs();
    fetchExecutions();
    initialLoadDone.current = true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Jobs query effect (分页变化时触发查询，首次加载由上面的 effect 控制)
  useEffect(() => {
    if (!initialLoadDone.current) {
      return;
    }
    fetchJobs();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobsPage, jobsPageSize]);

  // Executions query effect (分页变化时触发查询)
  useEffect(() => {
    if (!initialLoadDone.current) {
      return;
    }
    fetchExecutions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [execsPage, execsPageSize]);

  // Handler for jobs search button
  const handleJobsSearch = () => {
    setJobsPage(1);
    fetchJobs();
  };

  // Handler for executions search button
  const handleExecsSearch = () => {
    setExecsPage(1);
    fetchExecutions();
  };

  // Export jobs
  const handleExportJobs = async () => {
    try {
      message.loading("正在导出...");
      const enabledValue = jobsEnabledFilter === "true" ? true : jobsEnabledFilter === "false" ? false : undefined;
      const blob = await monitorApi.exportJobs({
        tenant_id: jobsUserFilter || undefined,
        bbk_id: jobsBbkFilter || undefined,
        enabled: enabledValue,
      });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `cron_jobs_${dayjs().format("YYYY-MM-DD_HHmmss")}.xlsx`;
      a.click();
      window.URL.revokeObjectURL(url);
      message.success("导出成功");
    } catch (error) {
      console.error("Export failed:", error);
      message.error("导出失败");
    }
  };

  // Export executions
  const handleExportExecutions = async () => {
    try {
      message.loading("正在导出...");
      const timeRange = getTimeRange(execsTimeRangeType, execsCustomTimeRange);
      const blob = await monitorApi.exportExecutions({
        job_id: execsJobFilter || undefined,
        tenant_id: execsUserFilter || undefined,
        status: execsStatusFilter || undefined,
        ...timeRange,
      });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `cron_executions_${dayjs().format("YYYY-MM-DD_HHmmss")}.xlsx`;
      a.click();
      window.URL.revokeObjectURL(url);
      message.success("导出成功");
    } catch (error) {
      console.error("Export failed:", error);
      message.error("导出失败");
    }
  };

  // View execution detail
  const handleViewExecution = (exec: ExecutionItem) => {
    setSelectedExecution(exec);
    setDetailDrawerOpen(true);
  };

  // 构建带"全部"选项的筛选项列表（使用 useMemo 缓存）
  const userOptions = useMemo(() => {
    return [{ value: "", label: "全部" }, ...filterOptions.users];
  }, [filterOptions.users]);

  const jobIdOptions = useMemo(() => {
    return [{ value: "", label: "全部" }, ...filterOptions.job_ids];
  }, [filterOptions.job_ids]);

  // Jobs table columns
  const jobsColumns: ColumnsType<CronJobItem> = [
    {
      title: "任务名称",
      dataIndex: "name",
      key: "name",
      width: 160,
      ellipsis: true,
    },
    {
      title: "用户姓名",
      dataIndex: "tenant_name",
      key: "tenant_name",
      width: 120,
      render: (name: string) => name || "-",
    },
    {
      title: "用户ID",
      dataIndex: "tenant_id",
      key: "tenant_id",
      width: 140,
      ellipsis: true,
    },
    {
      title: "分行",
      dataIndex: "bbk_id",
      key: "bbk_id",
      width: 100,
      render: (bbk: string) => BBK_ID_TO_NAME_MAP[bbk] || bbk || "-",
    },
    {
      title: "是否启用",
      dataIndex: "enabled",
      key: "enabled",
      width: 90,
      render: (enabled: boolean) => (
        <Tag color={enabled ? "green" : "orange"}>
          {enabled ? "已启用" : "已禁用"}
        </Tag>
      ),
    },
    {
      title: "今日执行状态",
      dataIndex: "today_status",
      key: "today_status",
      width: 100,
      render: (status: string | null) => {
        if (!status) {
          return <Tag color="default">未执行</Tag>;
        }
        return (
          <Tag color={EXEC_STATUS_COLORS[status] || "default"}>
            {EXEC_STATUS_LABELS[status] || status}
          </Tag>
        );
      },
    },
    {
      title: "执行次数",
      dataIndex: "execution_count",
      key: "execution_count",
      width: 90,
      render: (count: number) => count || 0,
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 160,
      render: (time: string | null) =>
        time ? dayjs(time).format("YYYY-MM-DD HH:mm") : "-",
    },
  ];

  // Executions table columns
  const execsColumns: ColumnsType<ExecutionItem> = [
    {
      title: "任务名称",
      dataIndex: "job_name",
      key: "job_name",
      width: 160,
      ellipsis: true,
    },
    {
      title: "用户姓名",
      dataIndex: "tenant_name",
      key: "tenant_name",
      width: 120,
      render: (name: string) => name || "-",
    },
    {
      title: "用户ID",
      dataIndex: "tenant_id",
      key: "tenant_id",
      width: 140,
      ellipsis: true,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 80,
      render: (status: string, record: ExecutionItem) => {
        const color = EXEC_STATUS_COLORS[status] || "default";
        // 未读的成功任务添加高亮背景
        if (status === "success" && !record.is_read) {
          return (
            <Tag color={color} style={{ fontWeight: "bold" }}>
              {EXEC_STATUS_LABELS[status] || status}
            </Tag>
          );
        }
        return (
          <Tag color={color}>
            {EXEC_STATUS_LABELS[status] || status}
          </Tag>
        );
      },
    },
    {
      title: "已读",
      dataIndex: "is_read",
      key: "is_read",
      width: 70,
      render: (isRead: boolean, record: ExecutionItem) => {
        // 只有成功的任务才显示已读状态
        if (record.status !== "success") {
          return "-";
        }
        return isRead ? (
          <Tag color="green">已读</Tag>
        ) : (
          <Tag color="orange">未读</Tag>
        );
      },
    },
    {
      title: "执行时间",
      dataIndex: "actual_time",
      key: "actual_time",
      width: 160,
      render: (time: string) => dayjs(time).format("YYYY-MM-DD HH:mm"),
    },
    {
      title: "耗时",
      dataIndex: "duration_ms",
      key: "duration_ms",
      width: 80,
      render: (ms: number) => {
        if (!ms) return "-";
        if (ms < 1000) return `${ms}ms`;
        if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
        return `${(ms / 60000).toFixed(1)}m`;
      },
    },
    {
      title: "执行方式",
      dataIndex: "is_manual",
      key: "is_manual",
      width: 80,
      render: (manual: boolean) => manual ? "手动" : "自动",
    },
    {
      title: "操作",
      key: "action",
      width: 80,
      render: (_, record) => (
        <Button
          type="link"
          size="small"
          icon={<EyeOutlined />}
          onClick={() => handleViewExecution(record)}
        >
          详情
        </Button>
      ),
    },
  ];

  return (
    <div className={styles.cronOverviewPage}>
      <PageHeader
        items={[
          { title: t("nav.monitor") || "监控" },
          { title: "定时任务概览" },
        ]}
      />

      <Card className={styles.tableCard}>
        <Tabs
          defaultActiveKey="jobs"
          items={[
            {
              key: "jobs",
              label: "任务列表",
              children: (
                <>
                  <div className={styles.filterBar}>
                    <Space size="middle" wrap>
                      <Select
                        placeholder="用户"
                        value={jobsUserFilter}
                        onChange={(value) => setJobsUserFilter(value || "")}
                        style={{ width: 180 }}
                        showSearch
                        filterOption={(input, option) =>
                          (option?.label ?? "").toLowerCase().includes(input.toLowerCase())
                        }
                        options={userOptions}
                        loading={filterOptionsLoading}
                        virtual={false}
                      />
                      <Select
                        placeholder="分行"
                        value={jobsBbkFilter}
                        onChange={(value) => setJobsBbkFilter(value || "")}
                        style={{ width: 120 }}
                        options={BBK_OPTIONS}
                      />
                      <Select
                        placeholder="是否启用"
                        value={jobsEnabledFilter || undefined}
                        onChange={(value) => setJobsEnabledFilter(value || "")}
                        style={{ width: 120 }}
                        allowClear
                        options={ENABLED_OPTIONS}
                      />
                      <Button
                        type="primary"
                        icon={<ReloadOutlined />}
                        onClick={handleJobsSearch}
                      >
                        查询
                      </Button>
                      <Button
                        icon={<DownloadOutlined />}
                        onClick={handleExportJobs}
                      >
                        导出
                      </Button>
                    </Space>
                  </div>

                  <Table
                    columns={jobsColumns}
                    dataSource={jobs}
                    rowKey="id"
                    loading={jobsLoading}
                    pagination={{
                      current: jobsPage,
                      pageSize: jobsPageSize,
                      total: jobsTotal,
                      showSizeChanger: true,
                      showTotal: (total) => `共 ${total} 条`,
                      onChange: (page, pageSize) => {
                        setJobsPage(page);
                        setJobsPageSize(pageSize);
                      },
                    }}
                    scroll={{ x: 1100 }}
                  />
                </>
              ),
            },
            {
              key: "executions",
              label: "执行记录",
              children: (
                <>
                  <div className={styles.filterBar}>
                    <Space size="middle" wrap>
                      <Select
                        placeholder="任务"
                        value={execsJobFilter}
                        onChange={(value) => setExecsJobFilter(value || "")}
                        style={{ width: 200 }}
                        showSearch
                        filterOption={(input, option) =>
                          (option?.label ?? "").toLowerCase().includes(input.toLowerCase())
                        }
                        options={jobIdOptions}
                        loading={filterOptionsLoading}
                        virtual={false}
                      />
                      <Select
                        placeholder="用户"
                        value={execsUserFilter}
                        onChange={(value) => setExecsUserFilter(value || "")}
                        style={{ width: 180 }}
                        showSearch
                        filterOption={(input, option) =>
                          (option?.label ?? "").toLowerCase().includes(input.toLowerCase())
                        }
                        options={userOptions}
                        loading={filterOptionsLoading}
                        virtual={false}
                      />
                      <Select
                        placeholder="状态"
                        value={execsStatusFilter}
                        onChange={(value) => setExecsStatusFilter(value || "")}
                        style={{ width: 120 }}
                        options={EXEC_STATUS_OPTIONS}
                      />
                      <Select
                        placeholder="时间范围"
                        value={execsTimeRangeType}
                        onChange={(value) => {
                          setExecsTimeRangeType(value);
                          if (value !== "custom") {
                            setExecsCustomTimeRange([null, null]);
                          }
                        }}
                        style={{ width: 120 }}
                        options={TIME_RANGE_OPTIONS}
                      />
                      {execsTimeRangeType === "custom" && (
                        <RangePicker
                          value={execsCustomTimeRange as [dayjs.Dayjs, dayjs.Dayjs]}
                          onChange={(dates) =>
                            setExecsCustomTimeRange(dates as [dayjs.Dayjs | null, dayjs.Dayjs | null])
                          }
                          allowClear
                        />
                      )}
                      <Button
                        type="primary"
                        icon={<ReloadOutlined />}
                        onClick={handleExecsSearch}
                      >
                        查询
                      </Button>
                      <Button
                        icon={<DownloadOutlined />}
                        onClick={handleExportExecutions}
                      >
                        导出
                      </Button>
                    </Space>
                  </div>

                  <Table
                    columns={execsColumns}
                    dataSource={executions}
                    rowKey="id"
                    loading={execsLoading}
                    pagination={{
                      current: execsPage,
                      pageSize: execsPageSize,
                      total: execsTotal,
                      showSizeChanger: true,
                      showTotal: (total) => `共 ${total} 条`,
                      onChange: (page, pageSize) => {
                        setExecsPage(page);
                        setExecsPageSize(pageSize);
                      },
                    }}
                    scroll={{ x: 1020 }}
                  />
                </>
              ),
            },
          ]}
        />
      </Card>

      <Drawer
        title="执行详情"
        placement="right"
        width={500}
        open={detailDrawerOpen}
        onClose={() => setDetailDrawerOpen(false)}
      >
        {selectedExecution && (
          <Descriptions column={1} bordered size="small">
            <Descriptions.Item label="任务名称">
              {selectedExecution.job_name || "-"}
            </Descriptions.Item>
            <Descriptions.Item label="用户姓名">
              {selectedExecution.tenant_name || "-"}
            </Descriptions.Item>
            <Descriptions.Item label="用户ID">
              {selectedExecution.tenant_id || "-"}
            </Descriptions.Item>
            <Descriptions.Item label="执行状态">
              <Tag color={EXEC_STATUS_COLORS[selectedExecution.status]}>
                {EXEC_STATUS_LABELS[selectedExecution.status]}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="执行时间">
              {dayjs(selectedExecution.actual_time).format("YYYY-MM-DD HH:mm:ss")}
            </Descriptions.Item>
            <Descriptions.Item label="耗时">
              {selectedExecution.duration_ms
                ? `${(selectedExecution.duration_ms / 1000).toFixed(1)}秒`
                : "-"}
            </Descriptions.Item>
            <Descriptions.Item label="执行方式">
              {selectedExecution.is_manual ? "手动触发" : "自动执行"}
            </Descriptions.Item>
            <Descriptions.Item label="Trace ID">
              {selectedExecution.trace_id || "-"}
            </Descriptions.Item>
            <Descriptions.Item label="Session ID">
              {selectedExecution.session_id || "-"}
            </Descriptions.Item>
            <Descriptions.Item label="输出预览">
              {selectedExecution.output_preview || "-"}
            </Descriptions.Item>
            {selectedExecution.error_message && (
              <Descriptions.Item label="错误信息">
                <pre style={{ whiteSpace: "pre-wrap", color: "#ff4d4f" }}>
                  {selectedExecution.error_message}
                </pre>
              </Descriptions.Item>
            )}
          </Descriptions>
        )}
      </Drawer>
    </div>
  );
}
