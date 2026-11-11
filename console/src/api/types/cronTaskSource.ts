import type { CronJobSpecOutput } from "./cronjob";
import type { ManagedCronContext, ManagedJob } from "./schedulerCron";
import type { MigrationOwnership } from "../modules/cronMigration";

/** 仅用于客户端分流；服务端始终独立按 Header、归属和完整身份鉴权。 */
export interface CronTaskOrigin {
  owner: "legacy" | "scheduler";
  context: ManagedCronContext;
  accessKey: string;
  unavailable?: boolean;
}
export interface SourcedCronTask extends CronJobSpecOutput {
  clientTaskOrigin: CronTaskOrigin;
}
export interface SourcedManagedTask extends ManagedJob {
  clientTaskOrigin: CronTaskOrigin;
}
export interface TaskSourceSnapshot {
  legacy: SourcedCronTask[] | null;
  scheduler: SourcedManagedTask[] | null;
  errors: string[];
  ownership?: MigrationOwnership[] | null;
}
export interface TaskPresence {
  legacy: ReadonlySet<string> | null;
  scheduler: ReadonlySet<string> | null;
}
