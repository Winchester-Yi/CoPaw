import { request } from "../request";
import type {
  EffectiveSourceSystemConfig,
  SourceSystemConfigRecord,
  SourceSystemConfigUpsert,
} from "../types/sourceSystemConfig";

const MANAGER_HEADERS = {
  "X-User-Role": "manager",
};

export const sourceSystemConfigApi = {
  getEffective(): Promise<EffectiveSourceSystemConfig> {
    return request<EffectiveSourceSystemConfig>(
      "/source-system-config/effective",
    );
  },

  getSource(sourceId: string): Promise<SourceSystemConfigRecord> {
    return request<SourceSystemConfigRecord>(
      `/source-system-config/sources/${encodeURIComponent(sourceId)}`,
      {
        headers: MANAGER_HEADERS,
      },
    );
  },

  upsertSource(
    sourceId: string,
    payload: SourceSystemConfigUpsert,
  ): Promise<SourceSystemConfigRecord> {
    return request<SourceSystemConfigRecord>(
      `/source-system-config/sources/${encodeURIComponent(sourceId)}`,
      {
        method: "PUT",
        headers: MANAGER_HEADERS,
        body: JSON.stringify(payload),
      },
    );
  },
};
