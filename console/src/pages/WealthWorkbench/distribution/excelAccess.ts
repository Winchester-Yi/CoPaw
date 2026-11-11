export function canUseExcelDistribution(userId: string | null | undefined) {
  const users = window.__env__?.wealthExcelDistributionUsers;
  return !!userId && Array.isArray(users) && users.includes(userId);
}
