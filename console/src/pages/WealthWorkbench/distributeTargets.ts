/**
 * 智能财富工作台 —— 分发目标用户池
 *
 * 行长/中台发布规划前选择分发对象。数据源复用现有接口
 * GET /user-info/tenants/by-source?source_id=...（见 api/modules/userInfo.ts），
 * 入参 source_id 取自 iframeStore.source；为避免改动现有接口，
 * 在前端按当前登录人的分行号（iframeStore.bbk）过滤出本分行用户池。
 *
 * 本地开发无父系统身份（source/bbk 缺失）时回退到 mock 用户池，保证页面可演示。
 */
import { fetchTenantsBySource } from "../../api/modules/userInfo";
import { useIframeStore } from "../../stores/iframeStore";
import { distributeTargets as mockTargets } from "./mock/data";
import type { DistributeTarget } from "./types";

/**
 * 拉取当前登录人所在分行的分发目标用户池。
 * 真实身份可用时走接口并按 bbk 过滤；否则返回 mock 池。
 */
export async function fetchDistributeTargets(): Promise<DistributeTarget[]> {
  const { source, bbk } = useIframeStore.getState();
  if (!source || !bbk) {
    return [...mockTargets];
  }
  const tenants = await fetchTenantsBySource(source);
  return tenants
    .filter((t) => t.bbk_id === bbk)
    .map((t) => ({
      sapId: t.tenant_id,
      name: t.tenant_name ?? t.tenant_id,
      orgName: "",
    }));
}
