import type { ActiveModelsInfo } from "../../../api/types";

export function getRemoteProviders<T extends { is_local?: boolean }>(
  providers: T[],
): T[] {
  return providers.filter((provider) => !provider.is_local);
}

export function isActiveModel(
  activeModel: ActiveModelsInfo["active_llm"] | undefined,
  providerId: string,
  modelId: string,
): boolean {
  return (
    activeModel?.provider_id === providerId && activeModel?.model === modelId
  );
}
