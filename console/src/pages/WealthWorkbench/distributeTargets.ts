/**
 * 智能财富工作台 —— 分发目标用户池
 *
 * 行长/中台发布规划前选择分发对象。数据源复用现有接口
 * GET /user-info/tenants/by-source?source_id=...（见 api/modules/userInfo.ts），
 * 入参 source_id 取自 iframeStore.source；为避免改动现有接口，
 * 在前端按当前登录人的分行号（iframeStore.bbk）过滤出本分行用户池。
 *
 * 无父系统身份（source/bbk 缺失）或接口查询失败时返回空列表，
 * 由页面展示无数据态，不做假数据兜底。
 */
import { fetchTenantsBySource } from "../../api/modules/userInfo";
import { useIframeStore } from "../../stores/iframeStore";
import type { DistributeTarget } from "./types";

/**
 * 拉取当前登录人所在分行的分发目标用户池。
 * 查不到身份或接口失败时返回空列表，由页面展示无数据态。
 */
export async function fetchDistributeTargets(): Promise<DistributeTarget[]> {
  const { source, bbk } = useIframeStore.getState();
  if (!source || !bbk) {
    return [];
  }
  try {
    const tenants = await fetchTenantsBySource(source);
    return tenants
      .filter((t) => t.bbk_id === bbk)
      .map((t) => ({
        sapId: t.tenant_id,
        name: t.tenant_name ?? t.tenant_id,
        orgName: "",
      }));
  } catch (error) {
    console.warn("[Wealth] 分发目标用户池查询失败，按无数据处理", error);
    return [];
  }
}
