// ！！！！！！特别注意：具体内容已省略，仅展示部分机构，需要单独合并
export const BBK_ID_MAP = [
  { label: "总行", value: "100" },
  { label: "北京分行", value: "110" },
];

// 机构 ID 到名称的映射（用于快速查找显示）
export const BBK_ID_TO_NAME_MAP: Record<string, string> = BBK_ID_MAP.reduce(
  (acc, item) => {
    acc[item.value] = item.label;
    return acc;
  },
  {} as Record<string, string>,
);

/**
 * 获取机构显示名称
 * @param bbkId 机构 ID
 * @returns 机构名称，如果未找到则返回原 bbkId
 */
export function getBbkDisplayName(bbkId?: string): string {
  if (!bbkId) return "-";
  if (bbkId === "other" || bbkId === "unassigned") return "其他";
  return BBK_ID_TO_NAME_MAP[bbkId] || bbkId;
}
