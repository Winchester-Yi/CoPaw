import { useId, useState } from "react";
import { Tooltip } from "antd";
import type { CustomerField } from "../types";
import styles from "../index.module.less";

const PREVIEW_COUNT = 6;

export function CustomerMetrics({ fields = [] }: { fields?: CustomerField[] }) {
  const [expanded, setExpanded] = useState(false);
  const id = useId();
  const visible = expanded ? fields : fields.slice(0, PREVIEW_COUNT);
  if (!fields.length)
    return <span className={styles.metricEmpty}>暂无指标</span>;

  return (
    <div className={styles.customerMetrics}>
      <dl id={id} className={styles.metricGrid}>
        {visible.map((field) => (
          <Tooltip
            key={field.name}
            trigger={["hover", "focus"]}
            title={`${field.label}：${field.value}`}
          >
            <div className={styles.metricItem} tabIndex={0}>
              <dt>{field.label}：</dt>
              <dd>{field.value}</dd>
            </div>
          </Tooltip>
        ))}
      </dl>
      {fields.length > PREVIEW_COUNT && (
        <button
          type="button"
          className={styles.metricsToggle}
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "收起" : `展开全部（${fields.length}项）`}
        </button>
      )}
    </div>
  );
}
