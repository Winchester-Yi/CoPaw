import { reasonLabel, stateLabel } from "./presentation";
import styles from "./index.module.less";
interface EvidenceStateProps {
  state: string | null;
  reason?: string | null;
  missingFields?: string[];
  limited?: boolean;
  expired?: boolean;
}
export function EvidenceState({
  state,
  reason,
  missingFields = [],
  limited,
  expired,
}: EvidenceStateProps) {
  return (
    <div className={styles.evidenceState}>
      <span>{stateLabel(state)}</span>
      {reason && <span>{reasonLabel(reason)}</span>}
      {limited && <span>查询受限</span>}
      {expired && <span>证据已过期</span>}
      {missingFields.length > 0 && (
        <span>缺少字段：{missingFields.join("、")}</span>
      )}
    </div>
  );
}
