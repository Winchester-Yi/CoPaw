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
  [key: string]: unknown;
};

export type HookContext = Record<string, unknown>;

export type HookScriptDiagnostic = {
  event: string;
  group_id: string;
  handler_id: string;
  argument: string;
  reason: string;
};

export type HookConfigurationResponse = {
  hooks: HookConfig;
  revision: string;
  diagnostics: HookScriptDiagnostic[];
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

export type HookManualTestResponse = {
  redacted_summary: Record<string, unknown>;
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
};
