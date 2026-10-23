import { providerIcon } from "../../Settings/Models/components/providerIcon";

const BUILTIN_MODEL_ICON_RULES: Array<[string, string]> = [
  ["deepseek", "/icons/providers/deepseek.svg"],
  ["minimax", "/icons/providers/minimax.svg"],
  ["qwen", "/icons/providers/qwen.svg"],
  ["glm", "/icons/providers/glm.svg"],
];

export function getModelIcon(modelId: string, providerId: string): string {
  const normalizedModelId = modelId.toLowerCase();
  const builtinIcon = BUILTIN_MODEL_ICON_RULES.find(([keyword]) =>
    normalizedModelId.includes(keyword),
  );
  return builtinIcon?.[1] ?? providerIcon(providerId);
}
