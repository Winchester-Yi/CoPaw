/**
 * ============================================================
 * 身份相关常量配置
 * Author: Kun He
 * Date: 2026-04-07
 * ============================================================
 *
 * 统一的身份标识默认值配置
 * 修改默认值只需改动此文件一处
 *
 * 相关文件：
 * - utils/identity.ts: getUserId/getChannel helper
 * - api/authHeaders.ts: headers 构建
 * - pages/Chat/sessionApi/index.ts: session 管理
 * ============================================================
 */

/** 默认用户 ID（非 iframe 模式时使用） */
export const DEFAULT_USER_ID = "default";

/** 默认租户 ID（与 X-Tenant-Id header 对应） */
export const DEFAULT_TENANT_ID = "default";

/** 默认渠道名称 */
export const DEFAULT_CHANNEL = "console";