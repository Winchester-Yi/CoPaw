import { request } from "@/api/request";
import { getWorkbenchContext } from "../../operations";
import { getUserId } from "@/utils/identity";
import { DEFAULT_BBK_ID } from "@/constants/identity";
import type { Customer } from "../../types";

export const INTENTION_LABELS = {
  0: "待了解",
  1: "低意向",
  2: "中意向",
  3: "高意向",
} as const;
export type Intention = keyof typeof INTENTION_LABELS;
export type EditKind = "insurancePlan" | "contactPlan" | "followUp";

export interface ContactEntry {
  date: string;
  method: FollowUpMethod;
  content: string;
}

export const FOLLOW_UP_RESULTS = {
  0: "待跟进",
  1: "已完成",
} as const;
export const FOLLOW_UP_METHODS = {
  0: "企微",
  1: "招乎",
  2: "电访",
  3: "面访",
} as const;
export type FollowUpMethod = keyof typeof FOLLOW_UP_METHODS;
export function followUpMethodLabel(method?: FollowUpMethod): string {
  return method == null ? "—" : FOLLOW_UP_METHODS[method] ?? "—";
}
export type FollowUpResult = keyof typeof FOLLOW_UP_RESULTS;
export interface FollowUpEntry extends ContactEntry {
  result: FollowUpResult;
  skillId?: string;
  sceneName?: string;
}
export type ContactRecord = Omit<ContactEntry, "method"> & {
  id: string;
  method?: FollowUpMethod;
};
export type FollowUpRecord = Omit<FollowUpEntry, "result" | "method"> & {
  id: string;
  method?: FollowUpMethod;
  result?: FollowUpResult;
};

/** 页面视图模型，接口 DTO 在本模块内适配。 */
export interface InsuranceCustomer {
  id: string;
  custUid: string;
  skillId?: string;
  sapId?: string;
  name: string;
  branchName?: string;
  sceneName?: string;
  bbkOrgId?: string;
  /** 经营方案完整地址，对齐原名单的 filename 字段。 */
  link?: string;
  assets?: string | number;
  assetsDescription?: string;
  insurancePlan?: string;
  contactPlans?: ContactRecord[];
  followUps?: FollowUpRecord[];
  intention: Intention;
  touched?: FollowUpResult;
}

export interface InsuranceList {
  customers: InsuranceCustomer[];
  counts: { customers: number; highIntention: number; tracking: number };
}

export interface JoinTrackingValues {
  insurancePlan: string;
  contactPlans: ContactEntry[];
  intention: Intention;
}

export type InsuranceChange =
  | { kind: "insurancePlan"; value: string }
  | {
      kind: "contactPlan";
      value: ContactEntry;
      recordId?: string;
      requestId: string;
    }
  | {
      kind: "followUp";
      value: FollowUpEntry;
      recordId?: string;
      requestId: string;
    }
  | { kind: "intention"; value: Intention };

export type InsuranceEditorChange = Exclude<
  InsuranceChange,
  { kind: "intention" }
>;

const API_PREFIX = "/agent/workspace";
const ENDPOINTS = {
  list: "/cust-trace/query",
  join: "/cust-trace/batch-create",
  remove: "/cust-trace/batch-delete",
  update: "/cust-trace/update",
  addRecord: "/cust-trace/ext/add",
  updateRecord: "/cust-trace/ext/update",
} as const;

type WireId = string | number;
interface TraceExt {
  id: WireId;
  touchPlan?: string | null;
  touchType?: FollowUpMethod | null;
  touchDate?: string | null;
  tracePlan?: string | null;
  traceType?: FollowUpMethod | null;
  traceTime?: string | null;
  traceSkillId?: string;
  traceSkillName?: string;
  touched?: FollowUpResult;
}
interface CustTrace {
  id: WireId;
  sapId: string;
  custUid: string;
  custBbkOrgId?: string;
  custName?: string | null;
  aum?: string | null;
  skillId: string;
  skillName?: string | null;
  bxPlan?: string | null;
  custIntention?: Intention | null;
  touched?: FollowUpResult;
  touchList?: TraceExt[] | null;
  traceList?: TraceExt[] | null;
  filename?: string | null;
}
interface TraceGroup {
  sapId: string;
  custTraceList: CustTrace[];
}

function traceKey(custUid?: string) {
  return {
    sapId: getUserId(),
    // bbkId 始终使用请求侧二级分行，不使用查询返回的一级分行。
    bbkId: getWorkbenchContext().bbk || DEFAULT_BBK_ID,
    ...(custUid ? { custUid } : {}),
  };
}

async function insuranceRequest<T>(
  operation: keyof typeof ENDPOINTS,
  payload: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  signal?.addEventListener("abort", abort, { once: true });
  const timeout = setTimeout(abort, 60000);
  try {
    const response = await request<{
      code: string | number;
      message?: string;
      data: T;
    }>(`${API_PREFIX}${ENDPOINTS[operation]}`, {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify(payload),
    });
    if (String(response?.code) !== "200")
      throw new Error(response?.message || "保险跟踪请求失败，请重试");
    return response.data;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

function mapCustomer(row: CustTrace): InsuranceCustomer {
  return {
    id: String(row.id),
    custUid: row.custUid,
    sapId: row.sapId,
    skillId: row.skillId,
    name: row.custName ?? "—",
    bbkOrgId: row.custBbkOrgId,
    assets: row.aum ?? undefined,
    sceneName: row.skillName ?? undefined,
    insurancePlan: row.bxPlan ?? undefined,
    intention: row.custIntention ?? 0,
    touched: row.touched,
    link: row.filename ?? undefined,
    contactPlans: (row.touchList ?? []).map((entry) => ({
      id: String(entry.id),
      date: entry.touchDate ?? "",
      method: entry.touchType ?? undefined,
      content: entry.touchPlan ?? "",
    })),
    followUps: (row.traceList ?? []).map((entry) => ({
      id: String(entry.id),
      date: (entry.traceTime ?? "").replace(" ", "T"),
      method: entry.traceType ?? undefined,
      content: entry.tracePlan ?? "",
      result: entry.touched ?? row.touched,
      skillId: entry.traceSkillId,
      sceneName: entry.traceSkillName,
    })),
  };
}

export async function fetchInsuranceTracking(
  signal?: AbortSignal,
): Promise<InsuranceList> {
  const data = await insuranceRequest<TraceGroup[]>(
    "list",
    { items: [traceKey()] },
    signal,
  );
  if (
    !Array.isArray(data) ||
    data.some((group) => !Array.isArray(group.custTraceList))
  )
    throw new Error("保险跟踪数据格式异常，请联系管理员");
  const customers = data.flatMap((group) =>
    group.custTraceList.map(mapCustomer),
  );
  return {
    customers,
    counts: {
      customers: customers.length,
      tracking: customers.length,
      highIntention: customers.filter((row) => row.intention === 3).length,
    },
  };
}

export function customerAssets(customer: Pick<Customer, "dynamicFields">) {
  return customer.dynamicFields?.find(
    (field) =>
      /^(aum|assets|totalAssets)$/i.test(field.name) ||
      /^(资产|总资产|资产总额|时点AUM)$/i.test(field.label.replace(/\s/g, "")),
  )?.value;
}

export async function joinInsuranceTracking(
  customer: Customer,
  values: JoinTrackingValues,
  signal?: AbortSignal,
): Promise<{ created: boolean }> {
  const data = await insuranceRequest<{
    successCount: number;
    skipCount: number;
  }>(
    "join",
    {
      items: [
        {
          ...traceKey(customer.custUid),
          custBbkOrgId: customer.bbkOrgId,
          custName: customer.name,
          aum: customerAssets(customer),
          skillId: customer.skillId,
          skillName: customer.task,
          filename: customer.link,
          bxPlan: values.insurancePlan,
          touched: customer.touched === 1 ? 1 : 0,
          custIntention: values.intention,
          extList: values.contactPlans.map((plan) => ({
            type: 0,
            touchPlan: plan.content,
            touchType: plan.method,
            touchDate: plan.date,
          })),
        },
      ],
    },
    signal,
  );
  if (data?.successCount === 1) return { created: true };
  if (data?.skipCount === 1) return { created: false };
  throw new Error("加入跟踪响应缺少处理结果，请刷新后确认");
}

export async function removeInsuranceTracking(
  customer: InsuranceCustomer,
  signal?: AbortSignal,
): Promise<void> {
  await insuranceRequest(
    "remove",
    { items: [traceKey(customer.custUid)] },
    signal,
  );
}

/** 记录保存成功后回查失败不等于保存失败。 */
export class InsuranceSyncError extends Error {
  constructor() {
    super("保存已成功，但列表刷新失败，请刷新列表后继续编辑");
  }
}
export interface SavedRecords {
  touched?: FollowUpResult;
  records: (ContactRecord | FollowUpRecord)[];
}

export async function saveInsuranceChange(
  customer: InsuranceCustomer,
  change: InsuranceChange,
): Promise<ContactRecord | FollowUpRecord | SavedRecords | void> {
  if (change.kind === "insurancePlan" || change.kind === "intention") {
    await insuranceRequest("update", {
      id: customer.id,
      [change.kind === "insurancePlan" ? "bxPlan" : "custIntention"]:
        change.value,
    });
    return;
  }
  const previous =
    change.kind === "followUp"
      ? customer.followUps?.find((record) => record.id === change.recordId)
      : undefined;
  const fields =
    change.kind === "contactPlan"
      ? {
          type: 0,
          touchPlan: change.value.content,
          touchType: change.value.method,
          touchDate: change.value.date,
        }
      : {
          type: 1,
          tracePlan: change.value.content,
          traceType: change.value.method,
          traceTime: change.value.date
            .replace("T", " ")
            .replace(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})$/, "$1:00"),
          traceSkillId:
            change.value.skillId ?? previous?.skillId ?? customer.skillId,
          traceSkillName:
            change.value.sceneName ?? previous?.sceneName ?? customer.sceneName,
          touched: change.value.result,
        };
  await insuranceRequest(change.recordId ? "updateRecord" : "addRecord", {
    ...(change.recordId ? { id: change.recordId } : { traceId: customer.id }),
    ...fields,
  });
  // 跟进编辑会回写主表 touched；重新查询，更新所有从主表回填的触达结果。
  // 接触计划编辑不影响主表状态，可直接保留本次编辑值。
  if (change.recordId && change.kind === "contactPlan")
    return { ...previous, ...change.value, id: change.recordId };
  try {
    const list = await fetchInsuranceTracking();
    const saved = list.customers.find((row) => row.id === customer.id);
    if (!saved) throw new InsuranceSyncError();
    return {
      touched: saved.touched,
      records:
        (change.kind === "contactPlan"
          ? saved.contactPlans
          : saved.followUps) ?? [],
    };
  } catch {
    throw new InsuranceSyncError();
  }
}

export const EDIT_LABELS: Record<EditKind, string> = {
  insurancePlan: "保险计划",
  contactPlan: "接触计划",
  followUp: "跟进记录",
};
