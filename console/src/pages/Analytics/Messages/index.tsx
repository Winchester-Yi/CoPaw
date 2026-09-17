import { useCallback, useEffect, useMemo, useState, useRef } from "react";
import type { CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import {
  Table,
  Card,
  Input,
  Button,
  DatePicker,
  Tooltip,
  message,
  Select,
  Modal,
  Alert,
  Empty,
  Segmented,
  Spin,
} from "antd";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Clock3,
  Download,
  Eye,
  EyeOff,
  FileText,
  RefreshCw,
  Users,
} from "lucide-react";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import type { Dayjs } from "dayjs";
import { PageHeader } from "@/components/PageHeader";
import { tracingApi, UserMessageItem } from "../../../api/modules/tracing";
import {
  monitorApi,
  type AsyncTaskRecord,
  type HighFrequencyQuestionCriteria,
  type HighFrequencyQuestionResult,
} from "../../../api/modules/monitor";
import { getBbkDisplayName } from "../../../constants/bbk";
import {
  ensureBranchOptions,
  getScopedBranchFilter,
} from "../../../utils/branchScope";
import { useIframeStore } from "../../../stores/iframeStore";
import styles from "./index.module.less";

const { RangePicker } = DatePicker;
const HIGH_FREQUENCY_QUESTION_TASK_TYPE = "monitor.high.freq.question";
const USE_HFQ_MODAL_MOCK_RESULT = true;

function getDefaultAnalysisRange(): [Dayjs, Dayjs] {
  return [dayjs().subtract(6, "day").startOf("day"), dayjs().endOf("day")];
}

function buildMockHighFrequencyQuestionResult(
  range: [Dayjs, Dayjs],
  bbkId?: string,
): HighFrequencyQuestionResult {
  const scopeType = bbkId ? "ORG" : "ALL";

  return {
    state: "AVAILABLE",
    batch_id: "HFQ_MOCK_20260910_001",
    status: "SUCCESS",
    source_id: "RMASSIST",
    stat_start_time: range[0].startOf("day").format("YYYY-MM-DD HH:mm:ss"),
    stat_end_time: range[1].endOf("day").format("YYYY-MM-DD HH:mm:ss"),
    scope_type: scopeType,
    bbk_id: bbkId || "ALL",
    result_updated_at: "2026-09-01 09:27:01",
    message_count: 876,
    user_count: 243,
    total_skill_used_count: 536,
    topic_count: 4,
    skill_gap_topic_count: 2,
    topics: [
      {
        rank_no: 1,
        topic_name: "生成保险营销话术与方案",
        message_count: 76,
        valid_message_count: 876,
        skill_used_count: 63,
        top_skill: "保险营销助手",
        bbk_dis: {
          "100": 60,
          "110": 10,
          "121": 2,
          "130": 1,
          "140": 1,
        },
        sample_questions: [
          "帮我营销张兴客户保险，都会长盈2026年金10w3年缴",
          "我要营销客户陈艺保险，建议客户增加配置年金产品",
          "帮我写需要找哪些目标客户，产品亮点、市场分析、沟通的话术",
        ],
      },
      {
        rank_no: 2,
        topic_name: "生成客户沟通与异议应对话术",
        message_count: 61,
        valid_message_count: 876,
        skill_used_count: 20,
        top_skill: "客户异议助手",
        bbk_dis: {
          "100": 40,
          "110": 14,
          "121": 4,
          "150": 1,
          "160": 1,
        },
        sample_questions: [
          "客户说买的理财都没亏，这个就亏了，不赞同2年周期就是保本",
          "针对一个经常联系不上的客户该怎么办",
          "客户觉得保险5年时间有点久怎么办",
        ],
      },
      {
        rank_no: 3,
        topic_name: "产品收益对比与选择建议",
        message_count: 54,
        valid_message_count: 876,
        skill_used_count: 3,
        top_skill: null,
        bbk_dis: {
          "170": 24,
          "100": 15,
          "180": 7,
          "190": 4,
          "140": 4,
        },
        sample_questions: [
          "这两款保险产品有什么区别，哪个收益更高",
          "年金和增额终身寿险哪个好",
          "帮我对比一下同业的类似产品",
        ],
      },
      {
        rank_no: 4,
        topic_name: "客户画像总结与分析",
        message_count: 50,
        valid_message_count: 876,
        skill_used_count: 31,
        top_skill: "客户分析助手",
        bbk_dis: {
          "100": 26,
          "110": 12,
          "121": 6,
          "160": 3,
          "140": 3,
        },
        sample_questions: [
          "帮我总结这个客户的特点和需求",
          "根据历史对话，分析客户的风险偏好",
          "生成客户画像，并给出后续跟进建议",
        ],
      },
    ],
    message: null,
  };
}

function toHighFrequencyCriteria(
  range: [Dayjs, Dayjs],
  bbkId?: string,
): HighFrequencyQuestionCriteria {
  return {
    start_time: range[0].startOf("day").format("YYYY-MM-DD HH:mm:ss"),
    end_time: range[1].endOf("day").format("YYYY-MM-DD HH:mm:ss"),
    bbk_id: bbkId || null,
  };
}

function getTopicPercent(topic: HighFrequencyQuestionResult["topics"][number]) {
  if (!topic.valid_message_count) {
    return "0.0%";
  }
  return `${((topic.message_count / topic.valid_message_count) * 100).toFixed(
    1,
  )}%`;
}

function getTopicMetricText(
  topic: HighFrequencyQuestionResult["topics"][number],
) {
  return `${topic.message_count.toLocaleString("zh-CN")}条（${getTopicPercent(
    topic,
  )}）`;
}

function formatAnalysisCount(value?: number | null) {
  return (value || 0).toLocaleString("zh-CN");
}

function formatAnalysisPercent(
  numerator?: number | null,
  denominator?: number | null,
) {
  if (!denominator || denominator <= 0) {
    return "0.0%";
  }
  return `${(((numerator || 0) / denominator) * 100).toFixed(1)}%`;
}

function getAnalysisSkillCoverage(result: HighFrequencyQuestionResult) {
  if (!result.message_count || result.message_count <= 0) {
    return 0;
  }
  return Math.min(
    Math.max(result.total_skill_used_count / result.message_count, 0),
    1,
  );
}

function getTopicSkillCoverage(
  topic: HighFrequencyQuestionResult["topics"][number],
) {
  if (!topic.message_count || topic.message_count <= 0) {
    return 0;
  }
  return Math.min(Math.max(topic.skill_used_count / topic.message_count, 0), 1);
}

function getTopicSkillMetricText(
  topic: HighFrequencyQuestionResult["topics"][number],
) {
  return formatAnalysisPercent(topic.skill_used_count, topic.message_count);
}

function getTopicBbkDistribution(
  topic: HighFrequencyQuestionResult["topics"][number],
) {
  const entries = Object.entries(topic.bbk_dis || {})
    .map(([bbkId, value]) => ({
      bbkId,
      value: Number(value),
    }))
    .filter((item) => Number.isFinite(item.value) && item.value > 0)
    .sort((a, b) => b.value - a.value);

  if (entries.length === 0) {
    return [];
  }

  const total = entries.reduce((sum, item) => sum + item.value, 0);

  return entries.slice(0, 5).map((item) => {
    const name = getBbkDisplayName(item.bbkId);
    const width =
      entries.length === 1
        ? 100
        : total > 0
        ? Math.max((item.value / total) * 100, 10)
        : 0;
    return {
      bbkId: item.bbkId,
      name,
      valueText:
        topic.message_count > 0
          ? `${((item.value / topic.message_count) * 100).toFixed(1)}%`
          : "0.0%",
      width,
    };
  });
}

function getCriteriaTaskRequest(criteria: HighFrequencyQuestionCriteria) {
  const bbkId = criteria.bbk_id?.trim();
  return {
    start_date: dayjs(criteria.start_time).format("YYYY-MM-DD"),
    end_date: dayjs(criteria.end_time).format("YYYY-MM-DD"),
    scope_type: bbkId ? "ORG" : "ALL",
    bbk_id: bbkId || "ALL",
  };
}

function isMatchingRunningAnalysisTask(
  task: AsyncTaskRecord,
  criteria: HighFrequencyQuestionCriteria,
) {
  if (
    task.task_type !== HIGH_FREQUENCY_QUESTION_TASK_TYPE ||
    String(task.status || "").toLowerCase() !== "running"
  ) {
    return false;
  }

  const resultJson = task.result_json;
  if (!resultJson || typeof resultJson !== "object") {
    return false;
  }

  const request = (resultJson as { request?: Record<string, unknown> }).request;
  if (!request) {
    return false;
  }

  const expected = getCriteriaTaskRequest(criteria);
  return (
    request.start_date === expected.start_date &&
    request.end_date === expected.end_date &&
    request.scope_type === expected.scope_type &&
    request.bbk_id === expected.bbk_id
  );
}

function formatAnalysisTime(value?: string | null) {
  if (!value) {
    return "-";
  }
  return dayjs(value).format("YYYY-MM-DD HH:mm:ss");
}

function getPercentClassName(rankNo: number) {
  if (rankNo === 1) return styles.analysisPercentFirst;
  if (rankNo === 2) return styles.analysisPercentSecond;
  if (rankNo === 3) return styles.analysisPercentThird;
  return styles.analysisPercentDefault;
}

function getRankClassName(rankNo: number) {
  if (rankNo === 1) return styles.analysisRankGold;
  if (rankNo === 2) return styles.analysisRankSilver;
  if (rankNo === 3) return styles.analysisRankBronze;
  return styles.analysisRank;
}

function getTopicAccentColor(rankNo: number) {
  if (rankNo === 1) return "#f97316";
  if (rankNo === 2) return "#1d4ed8";
  if (rankNo === 3) return "#16a34a";
  return "#2563eb";
}

export default function MessagesPage() {
  const { t } = useTranslation();
  const currentBbkId = useIframeStore((state) => state.bbk);
  const branchScope = useMemo(
    () => getScopedBranchFilter(currentBbkId),
    [currentBbkId],
  );
  const branchOptions = useMemo(
    () => ensureBranchOptions(branchScope.lockedBbkId),
    [branchScope.lockedBbkId],
  );
  const [loading, setLoading] = useState(true);
  const [messages, setMessages] = useState<UserMessageItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [searchQuery, setSearchQuery] = useState("");
  const [userIdFilter, setUserIdFilter] = useState("");
  const [sessionIdFilter, setSessionIdFilter] = useState("");
  const [bbkIdFilter, setBbkIdFilter] = useState<string | undefined>(
    branchScope.lockedBbkId,
  );
  const [dateRange, setDateRange] = useState<[dayjs.Dayjs, dayjs.Dayjs] | null>(
    [dayjs().subtract(7, "day"), dayjs()],
  );
  const [exporting, setExporting] = useState(false);
  const [showIdColumns, setShowIdColumns] = useState(false);
  const [analysisOpen, setAnalysisOpen] = useState(false);
  const [analysisRange, setAnalysisRange] = useState<[Dayjs, Dayjs]>(
    getDefaultAnalysisRange,
  );
  const [analysisQuickRange, setAnalysisQuickRange] = useState("7");
  const [analysisBbkId, setAnalysisBbkId] = useState<string | undefined>(
    branchScope.lockedBbkId,
  );
  const [analysisResult, setAnalysisResult] =
    useState<HighFrequencyQuestionResult | null>(null);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisSubmitting, setAnalysisSubmitting] = useState(false);
  const [analysisTaskId, setAnalysisTaskId] = useState<string | null>(null);
  const [analysisTaskStatus, setAnalysisTaskStatus] = useState<
    "idle" | "running" | "failed"
  >("idle");
  const [analysisQueried, setAnalysisQueried] = useState(false);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const analysisQuerySeqRef = useRef(0);

  useEffect(() => {
    if (branchScope.lockedBbkId) {
      setBbkIdFilter(branchScope.lockedBbkId);
      setAnalysisBbkId(branchScope.lockedBbkId);
    }
  }, [branchScope.lockedBbkId]);

  useEffect(() => {
    if (branchScope.lockedBbkId) {
      setBbkIdFilter(branchScope.lockedBbkId);
    }
  }, [branchScope.lockedBbkId]);

  // 用于追踪筛选条件变化，避免 useEffect 重复触发
  const filtersRef = useRef({
    searchQuery: "",
    userIdFilter: "",
    sessionIdFilter: "",
    bbkIdFilter: undefined as string | undefined,
    dateRange: [dayjs().subtract(7, "day"), dayjs()] as
      | [dayjs.Dayjs, dayjs.Dayjs]
      | null,
  });

  useEffect(() => {
    // 检查筛选条件是否变化
    const filtersChanged =
      filtersRef.current.searchQuery !== searchQuery ||
      filtersRef.current.userIdFilter !== userIdFilter ||
      filtersRef.current.sessionIdFilter !== sessionIdFilter ||
      filtersRef.current.bbkIdFilter !== bbkIdFilter ||
      filtersRef.current.dateRange !== dateRange;

    // 更新 ref
    filtersRef.current = {
      searchQuery,
      userIdFilter,
      sessionIdFilter,
      bbkIdFilter,
      dateRange,
    };

    // 如果筛选条件变化且不是第一页，只重置页码不查询（等待 page 变化触发查询）
    if (filtersChanged && page !== 1) {
      setPage(1);
      return;
    }

    fetchMessages();
  }, [page, pageSize, bbkIdFilter, dateRange]);

  const handleSearch = () => {
    setPage(1);
    fetchMessages();
  };

  const fetchMessages = async () => {
    setLoading(true);
    try {
      const data = await tracingApi.getUserMessages(page, pageSize, {
        user_id: userIdFilter || undefined,
        session_id: sessionIdFilter || undefined,
        bbk_ids: bbkIdFilter,
        start_date: dateRange?.[0]?.format("YYYY-MM-DD"),
        end_date: dateRange?.[1]?.format("YYYY-MM-DD"),
        query: searchQuery || undefined,
        exclude_cron_task_sessions: true,
      });
      setMessages(data.items || []);
      setTotal(data.total || 0);
    } catch (error) {
      console.error("Failed to fetch messages:", error);
    } finally {
      setLoading(false);
    }
  };

  const handleExport = async () => {
    setExporting(true);
    try {
      const blob = await tracingApi.exportUserMessages(
        {
          user_id: userIdFilter || undefined,
          session_id: sessionIdFilter || undefined,
          bbk_ids: bbkIdFilter,
          start_date: dateRange?.[0]?.format("YYYY-MM-DD"),
          end_date: dateRange?.[1]?.format("YYYY-MM-DD"),
          query: searchQuery || undefined,
          exclude_cron_task_sessions: true,
        },
        "xlsx",
      );
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `user_messages_${dayjs().format("YYYYMMDD_HHmmss")}.xlsx`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);
    } catch (error) {
      console.error("Failed to export messages:", error);
      const errorMsg = error instanceof Error ? error.message : "Export failed";
      message.error(errorMsg);
    } finally {
      setExporting(false);
    }
  };

  const resetAnalysisResultState = () => {
    setAnalysisResult(null);
    setAnalysisError(null);
    setAnalysisTaskId(null);
    setAnalysisTaskStatus("idle");
    setAnalysisQueried(false);
  };

  const queryAnalysisResult = useCallback(
    async (range = analysisRange, bbkId = analysisBbkId) => {
      const querySeq = analysisQuerySeqRef.current + 1;
      analysisQuerySeqRef.current = querySeq;
      setAnalysisLoading(true);
      setAnalysisError(null);
      setAnalysisTaskId(null);
      setAnalysisTaskStatus("idle");
      try {
        const criteria = toHighFrequencyCriteria(range, bbkId);
        if (USE_HFQ_MODAL_MOCK_RESULT) {
          const data = buildMockHighFrequencyQuestionResult(range, bbkId);
          if (querySeq !== analysisQuerySeqRef.current) {
            return;
          }
          setAnalysisResult(data);
          setAnalysisQueried(true);
          return;
        }
        const data = await monitorApi.getHighFrequencyQuestionResults(criteria);
        if (querySeq !== analysisQuerySeqRef.current) {
          return;
        }
        if (data.state === "EMPTY") {
          const tasks = await monitorApi.getAsyncTasks({
            task_type: HIGH_FREQUENCY_QUESTION_TASK_TYPE,
            status: "running",
            page: 1,
            page_size: 100,
          });
          if (querySeq !== analysisQuerySeqRef.current) {
            return;
          }
          const runningTask = tasks.items.find((task) =>
            isMatchingRunningAnalysisTask(task, criteria),
          );
          if (runningTask) {
            setAnalysisResult(null);
            setAnalysisTaskId(runningTask.task_id);
            setAnalysisTaskStatus("running");
            setAnalysisQueried(true);
            return;
          }
        }
        setAnalysisResult(data);
        setAnalysisQueried(true);
      } catch (error) {
        if (querySeq !== analysisQuerySeqRef.current) {
          return;
        }
        const errorMsg =
          error instanceof Error ? error.message : "查询高频问题结果失败";
        setAnalysisError(errorMsg);
        message.error(errorMsg);
      } finally {
        if (querySeq === analysisQuerySeqRef.current) {
          setAnalysisLoading(false);
        }
      }
    },
    [analysisRange, analysisBbkId],
  );

  useEffect(() => {
    if (!analysisOpen) {
      return;
    }
    void queryAnalysisResult();
  }, [analysisOpen, queryAnalysisResult]);

  const openAnalysisModal = () => {
    setAnalysisOpen(true);
  };

  const handleAnalysisQuickRangeChange = (value: string | number) => {
    const days = Number(value);
    setAnalysisQuickRange(String(value));
    setAnalysisRange([
      dayjs()
        .subtract(days - 1, "day")
        .startOf("day"),
      dayjs().endOf("day"),
    ]);
    resetAnalysisResultState();
  };

  const handleAnalysisRangeChange = (
    dates: null | [Dayjs | null, Dayjs | null],
  ) => {
    if (!dates?.[0] || !dates?.[1]) {
      return;
    }
    if (dates[1].startOf("day").diff(dates[0].startOf("day"), "day") > 6) {
      message.warning("高频问题分析最多支持 7 天的数据范围");
      return;
    }
    setAnalysisRange([dates[0], dates[1]]);
    setAnalysisQuickRange("custom");
    resetAnalysisResultState();
  };

  const handleAnalysisBbkChange = (value?: string) => {
    if (!branchScope.lockedBbkId) {
      setAnalysisBbkId(value);
      resetAnalysisResultState();
    }
  };

  const submitAnalysisTask = async () => {
    setAnalysisSubmitting(true);
    setAnalysisError(null);
    try {
      const shouldForceRegenerate =
        analysisResult?.state === "AVAILABLE" ||
        analysisResult?.state === "AVAILABLE_STALE";
      const data = await monitorApi.submitHighFrequencyQuestionTask({
        ...toHighFrequencyCriteria(analysisRange, analysisBbkId),
        force: shouldForceRegenerate,
      });
      if (data.state === "AVAILABLE") {
        setAnalysisResult({ ...data, state: "AVAILABLE" });
        setAnalysisTaskId(null);
        setAnalysisTaskStatus("idle");
        setAnalysisQueried(true);
        return;
      }
      setAnalysisResult(null);
      setAnalysisQueried(true);
      setAnalysisTaskId(data.task_id || null);
      setAnalysisTaskStatus("running");
    } catch (error) {
      const errorMsg =
        error instanceof Error ? error.message : "提交高频问题分析任务失败";
      setAnalysisError(errorMsg);
      message.error(errorMsg);
    } finally {
      setAnalysisSubmitting(false);
    }
  };

  useEffect(() => {
    if (!analysisOpen || !analysisTaskId || analysisTaskStatus !== "running") {
      return;
    }

    let cancelled = false;
    const timer = window.setInterval(() => {
      monitorApi
        .getAsyncTaskDetail(analysisTaskId)
        .then((task) => {
          if (cancelled) {
            return;
          }
          const status = String(task.status || "").toLowerCase();
          if (status === "succeeded" || status === "success") {
            window.clearInterval(timer);
            setAnalysisTaskStatus("idle");
            setAnalysisTaskId(null);
            void queryAnalysisResult();
          } else if (status === "failed" || status === "error") {
            window.clearInterval(timer);
            setAnalysisTaskStatus("failed");
            setAnalysisError(
              task.error_message || "高频问题分析生成失败，请稍后重新生成",
            );
          }
        })
        .catch((error) => {
          if (cancelled) {
            return;
          }
          const errorMsg =
            error instanceof Error ? error.message : "查询任务状态失败";
          setAnalysisError(errorMsg);
        });
    }, 5000);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [analysisOpen, analysisTaskId, analysisTaskStatus, queryAnalysisResult]);

  const renderAnalysisContent = () => {
    if (analysisLoading) {
      return (
        <div className={styles.analysisCenterState}>
          <Spin />
          <span>正在查询高频问题结果</span>
        </div>
      );
    }

    if (analysisTaskStatus === "running") {
      return (
        <div className={styles.analysisCenterState}>
          <Spin />
          <strong>高频问题分析生成中</strong>
          <span>结果生成后将自动刷新，你也可以关闭弹窗稍后再看。</span>
        </div>
      );
    }

    if (
      analysisResult?.state === "AVAILABLE" ||
      analysisResult?.state === "AVAILABLE_STALE"
    ) {
      const skillCoverage = getAnalysisSkillCoverage(analysisResult);
      const skillCoveragePercent = formatAnalysisPercent(
        analysisResult.total_skill_used_count,
        analysisResult.message_count,
      );

      return (
        <>
          <div className={styles.analysisStatusBar}>
            <div className={styles.analysisStatusMain}>
              <span className={styles.analysisStatusIcon}>
                <Clock3 size={18} />
              </span>
              <div>
                <strong>当前结果已生成</strong>
                <span>
                  本结果更新于{" "}
                  {formatAnalysisTime(analysisResult.result_updated_at)}
                </span>
              </div>
            </div>
          </div>
          <section className={styles.analysisSummaryGrid}>
            <div className={styles.analysisSummaryCard}>
              <span className={styles.analysisSummaryIcon}>
                <FileText size={24} />
              </span>
              <div>
                <strong>
                  {formatAnalysisCount(analysisResult.message_count)} 条
                </strong>
                <span>参与统计消息</span>
              </div>
            </div>
            <div className={styles.analysisSummaryCard}>
              <span className={styles.analysisSummaryIcon}>
                <Users size={24} />
              </span>
              <div>
                <strong>
                  {formatAnalysisCount(analysisResult.user_count)} 人
                </strong>
                <span>参与用户</span>
              </div>
            </div>
            <div className={styles.analysisSummaryCard}>
              <span className={styles.analysisSummaryIcon}>
                <Activity size={24} />
              </span>
              <div className={styles.analysisSummarySkill}>
                <div className={styles.analysisSummarySkillHeader}>
                  <strong>{skillCoveragePercent}</strong>
                  <span>
                    {formatAnalysisCount(analysisResult.total_skill_used_count)}{" "}
                    / {formatAnalysisCount(analysisResult.message_count)} 条
                  </span>
                </div>
                <div className={styles.analysisSummaryTrack}>
                  <div
                    className={styles.analysisSummaryBar}
                    style={{ width: `${skillCoverage * 100}%` }}
                  />
                </div>
                <span>技能承载率</span>
              </div>
            </div>
            <div className={styles.analysisSummaryCard}>
              <span className={styles.analysisSummaryIcon}>
                <AlertTriangle size={24} />
              </span>
              <div>
                <strong>
                  {formatAnalysisCount(analysisResult.skill_gap_topic_count)} /{" "}
                  {formatAnalysisCount(analysisResult.topic_count)}
                </strong>
                <span>高频问题技能承载缺口</span>
              </div>
            </div>
          </section>
          <section className={styles.analysisResults}>
            <h3>高频问题 TOP10</h3>
            <div className={styles.analysisTopicList}>
              {analysisResult.topics.map((topic) => {
                const bbkDistribution = getTopicBbkDistribution(topic);
                const skillCoverage = getTopicSkillCoverage(topic);

                return (
                  <article
                    className={styles.analysisTopic}
                    key={topic.rank_no}
                    style={
                      {
                        "--analysis-topic-accent": getTopicAccentColor(
                          topic.rank_no,
                        ),
                      } as CSSProperties &
                        Record<"--analysis-topic-accent", string>
                    }
                  >
                    <div className={getRankClassName(topic.rank_no)}>
                      {topic.rank_no}
                    </div>
                    <div className={styles.analysisTopicContent}>
                      <strong>{topic.topic_name}</strong>
                      <div className={styles.analysisQuestions}>
                        {topic.sample_questions.slice(0, 3).map((question) => (
                          <Tooltip key={question} title={`“${question}”`}>
                            <span>{`“${question}”`}</span>
                          </Tooltip>
                        ))}
                      </div>
                    </div>
                    <div className={styles.analysisBbkDistribution}>
                      {bbkDistribution.length > 0 ? (
                        <div className={styles.analysisBbkDistributionList}>
                          {bbkDistribution.map((item) => (
                            <div
                              className={styles.analysisBbkDistributionRow}
                              key={item.bbkId}
                            >
                              <Tooltip title={item.name}>
                                <span
                                  className={styles.analysisBbkDistributionName}
                                >
                                  {item.name}
                                </span>
                              </Tooltip>
                              <div
                                className={styles.analysisBbkDistributionTrack}
                              >
                                <div
                                  className={styles.analysisBbkDistributionBar}
                                  style={{ width: `${item.width}%` }}
                                />
                              </div>
                              <span
                                className={styles.analysisBbkDistributionValue}
                              >
                                {item.valueText}
                              </span>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <span
                          className={styles.analysisBbkDistributionEmpty}
                        ></span>
                      )}
                    </div>
                    <div className={styles.analysisSkillCoverage}>
                      <span className={styles.analysisSkillTitle}>
                        技能承载情况
                      </span>
                      <div className={styles.analysisSkillMetric}>
                        <div className={styles.analysisSkillTrack}>
                          <div
                            className={styles.analysisSkillBar}
                            style={{ width: `${skillCoverage * 100}%` }}
                          />
                        </div>
                        <span>{getTopicSkillMetricText(topic)}</span>
                      </div>
                      <div className={styles.analysisTopSkill}>
                        <span>主要承载技能：</span>
                        {topic.top_skill ? (
                          <Tooltip title={topic.top_skill}>
                            <b>{topic.top_skill}</b>
                          </Tooltip>
                        ) : (
                          <em>暂无</em>
                        )}
                      </div>
                    </div>
                    <span
                      className={`${
                        styles.analysisPercent
                      } ${getPercentClassName(topic.rank_no)}`}
                    >
                      {getTopicMetricText(topic)}
                    </span>
                  </article>
                );
              })}
            </div>
          </section>
        </>
      );
    }

    if (analysisQueried && analysisResult?.state === "EMPTY") {
      return (
        <Empty
          className={styles.analysisEmpty}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="当前筛选条件暂无分析结果"
        />
      );
    }

    return (
      <Empty
        className={styles.analysisEmpty}
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="请先查询当前筛选条件下的分析结果"
      />
    );
  };

  const formatDuration = (ms: number | null) => {
    if (ms === null) return "-";
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    return `${(ms / 60000).toFixed(1)}m`;
  };

  const truncateMessage = (msg: string | null, maxLen: number = 100) => {
    if (!msg) return "-";
    if (msg.length <= maxLen) return msg;
    return msg.slice(0, maxLen) + "...";
  };

  const allColumns: ColumnsType<UserMessageItem> = [
    {
      title: t("analytics.traceId", "对话ID"),
      dataIndex: "trace_id",
      key: "trace_id",
      width: 140,
      ellipsis: true,
      render: (v) => (
        <Tooltip title={v}>
          <span style={{ fontFamily: "monospace", fontSize: 12 }}>{v}</span>
        </Tooltip>
      ),
    },
    {
      title: t("analytics.userId", "User ID"),
      dataIndex: "user_id",
      key: "user_id",
      width: 100,
      ellipsis: true,
    },
    {
      title: t("analytics.userName", "用户姓名"),
      dataIndex: "user_name",
      key: "user_name",
      width: 100,
      render: (v) => v || "-",
    },
    {
      title: t("analytics.bbkId", "所属机构"),
      dataIndex: "bbk_id",
      key: "bbk_id",
      width: 100,
      render: (v) => getBbkDisplayName(v),
    },
    {
      title: t("analytics.sessionId", "会话ID"),
      dataIndex: "session_id",
      key: "session_id",
      width: 120,
      ellipsis: true,
    },
    {
      title: t("analytics.userMessage", "User Message"),
      dataIndex: "user_message",
      key: "user_message",
      width: 320,
      render: (msg) => {
        if (!msg) return <span style={{ color: "#999" }}>-</span>;
        const truncated = truncateMessage(msg, 120);
        if (msg.length <= 120) {
          return <span className={styles.userMessage}>{msg}</span>;
        }
        return (
          <Tooltip
            title={<pre className={styles.messagePopover}>{msg}</pre>}
            overlayStyle={{ maxWidth: 500 }}
          >
            <span className={styles.userMessage}>{truncated}</span>
          </Tooltip>
        );
      },
    },
    {
      title: t("analytics.model", "Model"),
      dataIndex: "model_name",
      key: "model_name",
      width: 150,
      ellipsis: true,
      render: (v) => v || "-",
    },
    {
      title: t("analytics.startTime", "Start Time"),
      dataIndex: "start_time",
      key: "start_time",
      width: 150,
      render: (v) => dayjs(v).format("YYYY-MM-DD HH:mm:ss"),
    },
    {
      title: t("analytics.duration", "Duration"),
      dataIndex: "duration_ms",
      key: "duration_ms",
      width: 80,
      render: (v) => formatDuration(v),
    },
  ];
  const columns = showIdColumns
    ? allColumns
    : allColumns.filter(
        (column) => column.key !== "trace_id" && column.key !== "session_id",
      );

  return (
    <div className={styles.messagesPage}>
      <PageHeader
        items={[
          { title: t("nav.insightCenter", "洞察中心") },
          { title: t("nav.analyticsMessages", "用户消息") },
        ]}
        extra={
          <div className={styles.headerActions}>
            <RangePicker
              value={dateRange}
              onChange={(dates) =>
                setDateRange(dates as [dayjs.Dayjs, dayjs.Dayjs] | null)
              }
              allowClear
            />
            <Button
              icon={showIdColumns ? <EyeOff size={16} /> : <Eye size={16} />}
              onClick={() => setShowIdColumns((visible) => !visible)}
            >
              {showIdColumns ? "隐藏ID列" : "显示ID列"}
            </Button>
            <Button
              type="primary"
              icon={<BarChart3 size={16} />}
              onClick={openAnalysisModal}
            >
              高频问题分析
            </Button>
          </div>
        }
      />

      <div className={styles.content}>
        <div className={styles.toolbar}>
          <div className={styles.searchBox}>
            <Input
              placeholder={t("analytics.searchMessage", "Search messages...")}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onPressEnter={handleSearch}
              allowClear
            />
          </div>
          <div className={styles.filters}>
            <Select
              placeholder={t("analytics.filterBbk")}
              value={bbkIdFilter}
              onChange={(v) => {
                if (!branchScope.lockedBbkId) {
                  setBbkIdFilter(v);
                  setPage(1);
                }
              }}
              allowClear={branchScope.isHeadOffice}
              disabled={!branchScope.isHeadOffice}
              showSearch
              optionFilterProp="label"
              style={{ width: 150 }}
              options={branchOptions}
            />
            <Input
              placeholder={t("analytics.filterUser", "User ID")}
              value={userIdFilter}
              onChange={(e) => setUserIdFilter(e.target.value)}
              onPressEnter={handleSearch}
              style={{ width: 150 }}
              allowClear
            />
            <Input
              placeholder={t("analytics.filterSession", "Session ID")}
              value={sessionIdFilter}
              onChange={(e) => setSessionIdFilter(e.target.value)}
              onPressEnter={handleSearch}
              style={{ width: 200 }}
              allowClear
            />
            <Button type="primary" onClick={handleSearch}>
              {t("common.search", "Search")}
            </Button>
            <Button
              icon={<Download size={16} />}
              onClick={handleExport}
              loading={exporting}
              style={{ minWidth: 120 }}
            >
              {t("analytics.exportExcel", "Export Excel")}
            </Button>
          </div>
        </div>

        <Card>
          <Table
            dataSource={messages}
            columns={columns}
            rowKey="trace_id"
            loading={loading}
            scroll={{ x: 1200 }}
            pagination={{
              current: page,
              pageSize,
              total,
              showSizeChanger: true,
              showQuickJumper: true,
              showTotal: (total) => t("analytics.totalItems", { total }),
              onChange: (p, ps) => {
                setPage(p);
                setPageSize(ps);
              },
            }}
          />
        </Card>
      </div>

      <Modal
        open={analysisOpen}
        title={
          <div className={styles.analysisTitle}>
            <span className={styles.analysisTitleIcon}>
              <BarChart3 size={22} />
            </span>
            <div className={styles.analysisTitleText}>
              <h2>高频问题分析</h2>
              <p>
                基于所选时间范围和机构的数据，分析用户咨询的高频问题及其分布情况
              </p>
            </div>
          </div>
        }
        width="80vw"
        centered
        destroyOnClose={false}
        onCancel={() => setAnalysisOpen(false)}
        className={styles.analysisModal}
        styles={{ body: { padding: 0 } }}
        footer={[
          <Button key="close" onClick={() => setAnalysisOpen(false)}>
            关闭
          </Button>,
          <Button
            key="query"
            icon={<RefreshCw size={16} />}
            onClick={() => void queryAnalysisResult()}
            loading={analysisLoading}
            disabled={analysisTaskStatus === "running"}
          >
            刷新
          </Button>,
          <Button
            key="generate"
            type="primary"
            onClick={submitAnalysisTask}
            loading={analysisSubmitting}
            disabled={analysisLoading || analysisTaskStatus === "running"}
          >
            {analysisTaskStatus === "running"
              ? "生成中..."
              : analysisResult?.state === "AVAILABLE" ||
                analysisResult?.state === "AVAILABLE_STALE"
              ? "重新生成分析"
              : "生成分析"}
          </Button>,
        ]}
      >
        <div className={styles.analysisDialog}>
          <div className={styles.analysisFilters}>
            <label>
              <span>时间范围</span>
              <RangePicker
                value={analysisRange}
                onChange={(dates) =>
                  handleAnalysisRangeChange(
                    dates as null | [Dayjs | null, Dayjs | null],
                  )
                }
                allowClear={false}
              />
            </label>
            <label>
              <span>所属机构</span>
              <Select
                value={analysisBbkId}
                onChange={handleAnalysisBbkChange}
                allowClear={branchScope.isHeadOffice}
                disabled={!branchScope.isHeadOffice}
                showSearch
                optionFilterProp="label"
                placeholder="全部机构（ALL）"
                className={styles.analysisBbkSelect}
                options={branchOptions}
              />
            </label>
            <label>
              <span>快捷选择</span>
              <Segmented
                value={analysisQuickRange}
                onChange={handleAnalysisQuickRangeChange}
                options={[
                  { label: "最近 1 天", value: "1" },
                  { label: "最近 3 天", value: "3" },
                  { label: "最近 7 天", value: "7" },
                ]}
              />
            </label>
          </div>
          {analysisError && (
            <Alert
              className={styles.analysisAlert}
              type="error"
              showIcon
              message={analysisError}
            />
          )}
          <div className={styles.analysisBody}>{renderAnalysisContent()}</div>
        </div>
      </Modal>
    </div>
  );
}
