import { request } from "../request";
import type { HeartbeatConfig } from "../types/heartbeat";
import type { SystemSyncSubmission } from "../types/systemCron";

export const heartbeatApi = {
  getHeartbeatConfig: () => request<HeartbeatConfig>("/config/heartbeat"),

  updateHeartbeatConfig: (body: HeartbeatConfig) =>
    request<HeartbeatConfig & { scheduler_sync?: SystemSyncSubmission }>(
      "/config/heartbeat",
      {
        method: "PUT",
        body: JSON.stringify(body),
      },
    ),
};
