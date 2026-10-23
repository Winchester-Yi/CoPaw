import type { ProviderInfo, ActiveModelsInfo } from "../../../../../api/types";
import { RemoteProviderCard } from "./RemoteProviderCard";

interface ProviderCardProps {
  provider: ProviderInfo;
  activeModels: ActiveModelsInfo | null;
  onSaved: () => void;
}

export function ProviderCard({
  provider,
  activeModels,
  onSaved,
}: ProviderCardProps) {
  return (
    <RemoteProviderCard
      provider={provider}
      activeModels={activeModels}
      onSaved={onSaved}
    />
  );
}
