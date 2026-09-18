import { CloseOutlined, CommentOutlined } from "@ant-design/icons";
import { useHtmlAnnotations } from "./context";
import styles from "./ComposerSummary.module.less";

export default function HtmlAnnotationComposerSummary() {
  const { pendingBundle, removeBundle } = useHtmlAnnotations();
  if (!pendingBundle) return null;
  return (
    <div className={styles.summary} role="status">
      <CommentOutlined className={styles.icon} />
      <span className={styles.fileName} title={pendingBundle.fileName}>
        {pendingBundle.fileName}
      </span>
      <span className={styles.count}>
        {pendingBundle.annotations.length} 条批注
      </span>
      <button
        type="button"
        className={styles.remove}
        aria-label="移除页面批注"
        onClick={removeBundle}
      >
        <CloseOutlined />
      </button>
    </div>
  );
}
