import { request } from "../request";
import type { SystemSyncSubmission } from "../types/systemCron";
import type {
  CurrentSourceSystemConfigResponse,
  CurrentSourceSystemConfigUpdateRequest,
  EffectiveSourceSystemConfig,
} from "../types/sourceSystemConfig";

export const sourceSystemConfigApi = {
  getEffective(): Promise<EffectiveSourceSystemConfig> {
    return request<EffectiveSourceSystemConfig>(
      "/source-system-config/effective",
    );
  },

  getCurrent(): Promise<CurrentSourceSystemConfigResponse> {
    return request<CurrentSourceSystemConfigResponse>(
      "/source-system-config/current",
    );
  },

  updateCurrent(
    payload: CurrentSourceSystemConfigUpdateRequest,
  ): Promise<CurrentSourceSystemConfigResponse> {
    return request<CurrentSourceSystemConfigResponse>(
      "/source-system-config/current",
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    );
  },

  deleteCurrent(): Promise<{
    deleted: boolean;
    scheduler_sync?: SystemSyncSubmission;
  }> {
    return request<{ deleted: boolean; scheduler_sync?: SystemSyncSubmission }>(
      "/source-system-config/current",
      {
        method: "DELETE",
      },
    );
  },
};
