import cx from "classnames";
import styles from "../index.module.less";
import skeleton from "./CustomerListSkeleton.module.less";

function SkeletonCell({
  className,
  shape,
}: {
  className?: string;
  shape?: "long" | "metrics" | "actions";
}) {
  return (
    <td className={className}>
      <span className={cx(skeleton.placeholder, shape && skeleton[shape])} />
    </td>
  );
}

export function CustomerListSkeleton({
  business,
  completed,
  showContactStatus,
}: {
  business: boolean;
  completed: boolean;
  showContactStatus: boolean;
}) {
  return (
    <>
      {[1, 2, 3, 4, 5].map((row) => (
        <tr key={row} aria-hidden="true" className={skeleton.row}>
          <SkeletonCell />
          {!business && <SkeletonCell />}
          {!business && <SkeletonCell className={styles.sceneCol} />}
          {!completed && (
            <SkeletonCell className={styles.opportunityCol} shape="long" />
          )}
          <SkeletonCell className={styles.metricsCol} shape="metrics" />
          {completed && <SkeletonCell className={styles.channelCol} />}
          {showContactStatus && (
            <SkeletonCell
              className={cx(styles.contactStatusCol, skeleton.contactStatus)}
            />
          )}
          <SkeletonCell className={styles.recentContactCol} />
          {!completed && <SkeletonCell className={styles.schemeCol} />}
          <SkeletonCell
            className={styles.executionCol}
            shape={completed ? undefined : "actions"}
          />
          {completed && <SkeletonCell />}
        </tr>
      ))}
    </>
  );
}
