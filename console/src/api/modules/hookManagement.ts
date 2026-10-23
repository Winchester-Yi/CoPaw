import { request } from "../request";

export type HookConfig = {
  enabled: boolean;
  events: Record<string, HookMatcherGroup[]>;
};

export type HookMatcherGroup = {
  id: string;
  matcher: { tools: string[] };
  hooks: HookHandler[];
};

export type HookHandler = {
  id: string;
  type: "command" | "http" | "prompt";
  outputAppend?: boolean;
  outputTransform?: boolean;
  once?: boolean;
  [key: string]: unknown;
};

export type HookContext = Record<string, unknown>;

export type HookConfigurationResponse = {
  hooks: HookConfig;
  revision: string;
};

export type HookScript = {
  filename: string;
  size: number;
  sha256: string;
};

export type HookScriptUploadResponse = {
  accepted: string[];
  warned: string[];
  failed: Array<{ filename: string; reason: string }>;
};

export type HookManualTestSummary = {
  handler_id: string;
  decision: string;
  failed: boolean;
  failure_type: string;
  status: string;
  output_transform: boolean;
  replacement_applied: boolean;
  replacement_length: number;
  output_append?: boolean;
  append_applied?: boolean;
  append_bytes?: number;
  effective_fail_policy?: string;
  duration_ms?: number;
};

export type HookManualTestResponse = {
  redacted_summary: HookManualTestSummary;
};

export type HookDistributionRequest = {
  matcherGroupIds: string[];
  targetTenantIds: string[];
};

export type HookDistributionTenantResult = {
  tenant_id: string;
  success: boolean;
  bootstrapped: boolean;
  matcher_group_ids: string[];
  script_names: string[];
  error: string;
};

export type HookDistributionResponse = {
  source_revision: string;
  results: HookDistributionTenantResult[];
};

export const hookManagementApi = {
  getConfiguration: () =>
    request<HookConfigurationResponse>("/hook-management/configuration"),

  saveConfiguration: (hooks: HookConfig, revision: string) =>
    request<HookConfigurationResponse>("/hook-management/configuration", {
      method: "PUT",
      headers: { "If-Match": revision },
      body: JSON.stringify({ hooks }),
    }),

  listScripts: () => request<HookScript[]>("/hook-management/scripts"),

  uploadScripts: (files: File[], overwrite: string[]) => {
    const body = new FormData();
    files.forEach((file) => body.append("files", file));
    body.append("overwrite", JSON.stringify(overwrite));
    return request<HookScriptUploadResponse>("/hook-management/scripts", {
      method: "POST",
      body,
    });
  },

  manualTest: (handler: HookHandler, context: HookContext) =>
    request<HookManualTestResponse>("/hook-management/manual-test", {
      method: "POST",
      body: JSON.stringify({
        confirmRealExecution: true,
        handler,
        context,
      }),
    }),

  distributeToDefaultAgents: (payload: HookDistributionRequest) =>
    request<HookDistributionResponse>(
      "/hook-management/distribute/default-agents",
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    ),
};
