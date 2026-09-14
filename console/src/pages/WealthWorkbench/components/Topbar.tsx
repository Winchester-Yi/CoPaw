/**
 * 智能财富工作台 —— 顶部栏：品牌、标题、通知、角色预览
 */
import { useNavigate } from "react-router-dom";
import styles from "../index.module.less";
import { IS_MOCK } from "../api";
import { selectCurrentAccount, useCanAccess, useWealthStore } from "../store";
import { AccountSwitcher } from "./AccountSwitcher";
import { Icon } from "./Icon";

/** 消息通知弹窗（原型 showNotifications） */
function NotificationsDialogBody() {
  const customers = useWealthStore((s) => s.customers);
  const hasTasks = useCanAccess("tasks");
  return (
    <>
      <div className={styles.recommendation}>
        <h3>本月经营规划已更新</h3>
        <p>查看规划看板，了解当前工作重点。</p>
      </div>
      {hasTasks ? (
        <div className={styles.recommendation}>
          <h3>今日客户清单已准备</h3>
          <p>
            目标客户 28 人，尚有 {customers.filter((c) => !c.done).length}{" "}
            人待触达。
          </p>
        </div>
      ) : (
        <div className={styles.recommendation}>
          <h3>工作重点支持日历查看</h3>
          <p>可按周、月查看工作任务安排及执行频率。</p>
        </div>
      )}
      <p className={styles.pageNote}>通知为原型示例。</p>
    </>
  );
}

export function Topbar() {
  const account = useWealthStore(selectCurrentAccount);
  const openDialog = useWealthStore((s) => s.openDialog);
  const closeDialog = useWealthStore((s) => s.closeDialog);
  const hasTasks = useCanAccess("tasks");
  const navigate = useNavigate();

  const showNotifications = () => {
    openDialog({
      title: "消息通知",
      body: <NotificationsDialogBody />,
      buttons: [
        { label: "关闭" },
        {
          label: hasTasks ? "查看今日任务" : "查看规划看板",
          primary: true,
          onClick: () => {
            closeDialog();
            navigate(hasTasks ? "/wealth/today" : "/wealth/board");
          },
        },
      ],
    });
  };

  return (
    <header className={styles.topbar}>
      <div className={styles.brand}>
        <svg
          className={styles.brandmark}
          viewBox="0 0 42 42"
          aria-hidden="true"
        >
          <circle
            cx="21"
            cy="21"
            r="18"
            fill="none"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            d="M31 12C18 2 5 25 17 29c7 2 12-6 14-10L14 24c-2-7 3-13 8-11"
            fill="none"
            stroke="currentColor"
            strokeWidth="4"
            strokeLinecap="round"
          />
        </svg>
        <div>
          <strong>智拓</strong>
        </div>
      </div>
      <div className={styles.brandtitle}>智能财富管理工作台</div>
      <div className={styles.account}>
        <button
          className={`${styles.iconbtn} ${styles.bell}`}
          aria-label="查看通知"
          onClick={showNotifications}
        >
          <Icon name="bell" />
        </button>
        <span className={styles.sep}></span>
        {IS_MOCK && (
          <>
            <AccountSwitcher />
            <span className={styles.sep}></span>
          </>
        )}
        <span className={`${styles.department} ${styles.subtle}`}>
          {account?.department}
        </span>
      </div>
    </header>
  );
}
