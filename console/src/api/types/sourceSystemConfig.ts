export type SourceSystemConfig = Record<string, unknown>;

export interface EffectiveSourceSystemConfig {
  source_id: string;
  config: SourceSystemConfig;
  version: number;
  is_default: boolean;
  stale: boolean;
  last_error?: string | null;
  updated_by?: string | null;
  updated_at?: string | null;
}

export interface SourceSystemConfigRecord {
  source_id: string;
  config: SourceSystemConfig;
  version: number;
  updated_by?: string | null;
  updated_at?: string | null;
}

export interface SourceSystemConfigUpsert {
  config: SourceSystemConfig;
}
