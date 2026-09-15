import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";
import { request } from "../request";
import {
  buildTaskReportDemo,
  buildTaskReportOptionsDemo,
  isTaskReportDemo,
} from "./taskTypeReportDemo";

export type ReportGroup = "branch" | "org" | "manager";
export type TaskType = "push_plan" | "ask_plan" | "push_other";
export interface ReportParams {
  start_date: string;
  end_date: string;
  group_by: ReportGroup;
  task_type?: TaskType;
  skill_detail?: boolean;
  first_bbk_id?: string;
  org_id?: string;
  user_id?: string;
  keyword?: string;
  page?: number;
  page_size?: number;
}
export interface ReportRow {
  first_bbk_id: string | null;
  first_bbk_name: string | null;
  org_id: string | null;
  org_name: string | null;
  user_id: string | null;
  user_name: string | null;
  sapid: string | null;
  pst_lvl: string | null;
  skill_id: string | null;
  cn_name: string | null;
  task_type: TaskType;
  task_type_name: string;
  skill_count: number;
  permission_manager_count: number;
  active_manager_count: number | null;
  suc_execute_job: number;
  read_tasks: number;
  read_rate: number | null;
  recommended_customers: number | null;
  read_customer_count: number | null;
  plan_read_rate: number | null;
  insight_customer_count: number | null;
  click_to_insight_rate: number | null;
  insight_count: number | null;
  phone_customer_count: number | null;
  click_to_phone_rate: number | null;
  phone_count: number | null;
}
export interface ReportResponse {
  sync_date: string | null;
  warnings: string[];
  items: ReportRow[];
  page: number | null;
  page_size: number | null;
  total: number;
  has_more: boolean;
}
export interface ReportOptionParams {
  end_date: string;
  kind: "branches" | "orgs";
  first_bbk_id?: string;
}
export interface ReportOptions {
  sync_date: string | null;
  items: { value: string; label: string }[];
}
export const REPORT_PAGE_SIZE = 20;
const REPORT_PATH = "/monitor/cron/task-type-report";
const XLSX_TYPE =
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

export function getTaskReportBbk() {
  return (
    buildAuthHeaders()["X-Bbk-Id"]?.trim() || (isTaskReportDemo() ? "100" : "")
  );
}

function scopedParams<T extends { first_bbk_id?: string }>(params: T): T {
  const bbk = getTaskReportBbk();
  if (!bbk) throw new Error("缺少分行身份，请从业务系统重新进入看板");
  if (bbk === "100") return params;
  if (params.first_bbk_id && params.first_bbk_id !== bbk)
    throw new Error("只能查询当前分行的数据");
  return { ...params, first_bbk_id: bbk };
}
function queryString(params: ReportParams | ReportOptionParams) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") query.set(key, String(value));
  });
  return query.toString();
}
function validateDates(params: ReportParams) {
  if (
    !/^\d{4}-\d{2}-\d{2}$/.test(params.start_date) ||
    !/^\d{4}-\d{2}-\d{2}$/.test(params.end_date) ||
    params.start_date.slice(0, 7) !== params.end_date.slice(0, 7) ||
    params.start_date > params.end_date
  ) {
    throw new Error("日期范围必须位于同一个月且开始日期不晚于结束日期");
  }
}
export function taskReportError(error: unknown): string {
  const detail = (error as { data?: { detail?: { message?: string } } })?.data
    ?.detail;
  return (
    detail?.message ||
    (error instanceof Error ? error.message : "报表请求失败，请重试")
  );
}
export async function getTaskTypeReport(
  params: ReportParams,
  signal?: AbortSignal,
): Promise<ReportResponse> {
  validateDates(params);
  const filters = scopedParams(params);
  if (isTaskReportDemo()) return buildTaskReportDemo(filters);
  return request<ReportResponse>(`${REPORT_PATH}?${queryString(filters)}`, {
    signal,
    headers: { "X-Bbk-Id": getTaskReportBbk() },
  });
}
export async function getTaskReportOptions(
  params: ReportOptionParams,
  signal?: AbortSignal,
): Promise<ReportOptions> {
  const filters = scopedParams(params);
  if (isTaskReportDemo()) return buildTaskReportOptionsDemo(filters);
  return request<ReportOptions>(
    `${REPORT_PATH}/options?${queryString(filters)}`,
    { signal, headers: { "X-Bbk-Id": getTaskReportBbk() } },
  );
}
export async function exportTaskReport(
  params: ReportParams,
  signal?: AbortSignal,
): Promise<Blob> {
  if (isTaskReportDemo())
    throw new Error("模拟数据仅供预览，Excel 导出需要连接真实后端");
  validateDates(params);
  const filters = {
    ...scopedParams(params),
    page: undefined,
    page_size: undefined,
  };
  const response = await fetch(
    getApiUrl(`${REPORT_PATH}/export?${queryString(filters)}`),
    { headers: buildAuthHeaders(), signal },
  );
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(
      data?.detail?.message ||
        (typeof data?.detail === "string"
          ? data.detail
          : `导出失败（HTTP ${response.status}），请重试`),
    );
  }
  if (!response.headers.get("content-type")?.includes(XLSX_TYPE))
    throw new Error("导出接口未返回 Excel 文件，请重试");
  return response.blob();
}
