/**
 * 智能财富工作台 —— 任务页（今日任务 / 待触达客户 / 已完成）
 * 对应原型 tasksHTML：经营/客户双视角、任务树、重点标签表头筛选、
 * 经营方案/触达记录两类弹窗，执行列外链跳转电访与客户洞察。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import cx from "classnames";
import styles from "../index.module.less";
import { Icon } from "../components/Icon";
import { useCanAccess, useWealthStore } from "../store";
import type { Customer } from "../types";
import { buildTaskTree, dateKey } from "../utils";

/** 重点标签匹配：同一客户可能命中多个场景（标签以 "、" 分隔） */
function matchLabel(label: string, selected: string) {
  return label.split("、").includes(selected);
}

/** 标签筛选选项：从当前名单数据派生（客户视角为命中场景名） */
function labelOptionsOf(customers: Customer[]): string[] {
  const set = new Set<string>();
  for (const c of customers) {
    for (const part of c.label.split("、")) {
      if (part) set.add(part);
    }
  }
  return [...set];
}

export type TaskPageKind = "today" | "pending" | "done";

/** 任务树大类图标（对应 Icon 组件的命名） */
const CATEGORY_ICONS: Record<string, string> = {
  保险: "shield",
  贷款: "bank",
  存款: "safe",
  理财: "money",
  基金: "pie",
  代发: "user",
};

/** 树节点来源标签的配色：复用重点标签的既有色板 */
function sourceTagClass(source: string) {
  return source === "分行关注"
    ? styles.purple
    : source === "行长关注"
    ? styles.orange
    : "";
}

/** 经营机会单元格：多条时以列表展示（原型 opportunitiesHTML） */
function Opportunities({
  customer,
  multiple,
}: {
  customer: Customer;
  multiple?: boolean;
}) {
  const items = customer.opportunities ?? [customer.reason];
  if (multiple && items.length > 1) {
    return (
      <ul className={styles.opportunityList}>
        {items.map((t, i) => (
          <li key={i}>{t}</li>
        ))}
      </ul>
    );
  }
  return <>{customer.reason}</>;
}

/** 电访 / 客户洞察外链占位：地址待外部系统提供，当前新窗口打开占位页 */
function openOutboundLink(kind: "dial" | "insight", c: Customer) {
  window.open(`https://example.com/${kind}?custUid=${c.custUid}`, "_blank");
}

function labelTagClass(label: string) {
  return label === "总行重点"
    ? styles.red
    : label === "行长指派"
    ? styles.orange
    : "";
}

export default function Tasks({ page }: { page: TaskPageKind }) {
  const canViewTasks = useCanAccess("tasks");
  const customers = useWealthStore((s) => s.customers);
  const customersLoading = useWealthStore((s) => s.customersLoading);
  const pendingCustomers = useWealthStore((s) => s.pendingCustomers);
  const pendingLoading = useWealthStore((s) => s.pendingLoading);
  const doneCustomers = useWealthStore((s) => s.doneCustomers);
  const doneLoading = useWealthStore((s) => s.doneLoading);
  const plans = useWealthStore((s) => s.plans);
  const loadTodayCustomers = useWealthStore((s) => s.loadTodayCustomers);
  const loadPendingCustomers = useWealthStore((s) => s.loadPendingCustomers);
  const loadDoneCustomers = useWealthStore((s) => s.loadDoneCustomers);
  const openDialog = useWealthStore((s) => s.openDialog);
  const navigate = useNavigate();

  const [view, setView] = useState<"business" | "customer">("business");
  const [taskLabel, setTaskLabel] = useState("全部");
  const [search, setSearch] = useState("");
  const [selectedTask, setSelectedTask] = useState("");
  const [selectedCategory, setSelectedCategory] = useState("");
  const [tagFilterOpen, setTagFilterOpen] = useState(false);
  const [tagFilterPos, setTagFilterPos] = useState({ left: 10, top: 10 });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);

  const isBiz = view === "business" && page === "today";
  const doneView = page === "done";

  /** 工作任务树：从「已发布且今日有排程」的规划场景实时推导 */
  const today = dateKey(new Date());
  const taskTree = useMemo(() => buildTaskTree(plans, today), [plans, today]);

  // 规划变更/移除后选中节点可能失效：回退到树的第一个节点
  useEffect(() => {
    if (!isBiz) return;
    const stillValid = taskTree.some(
      (g) =>
        g.category === selectedCategory &&
        g.nodes.some((n) => n.sceneName === selectedTask),
    );
    if (!stillValid) {
      const first = taskTree[0];
      setSelectedTask(first?.nodes[0]?.sceneName ?? "");
      setSelectedCategory(first?.category ?? "");
    }
  }, [isBiz, taskTree, selectedTask, selectedCategory]);

  /** 当前页的名单数据源：今日=customers；待触达/已完成=各自接口名单 */
  const pool =
    page === "done"
      ? doneCustomers
      : page === "pending"
      ? pendingCustomers
      : customers;

  /** 原型 customerList：ignoreLabel 用于标签浮层计数 */
  const buildList = (ignoreLabel: boolean) => {
    let list = page === "pending" ? pool.filter((c) => !c.done) : pool;
    if (isBiz)
      list = list.filter(
        (c) => c.task === selectedTask && c.category === selectedCategory,
      );
    if (search.trim())
      list = list.filter((c) =>
        (
          c.name +
          c.reason +
          (c.opportunities ?? []).join(" ") +
          c.label +
          c.task
        ).includes(search.trim()),
      );
    if (!ignoreLabel && taskLabel !== "全部")
      list = list.filter((c) => matchLabel(c.label, taskLabel));
    return list;
  };

  const list = useMemo(
    () => buildList(false),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [page, pool, isBiz, selectedTask, selectedCategory, search, taskLabel],
  );

  // 进入任务页加载名单：今日任务按当前视角查询；待触达/已完成按 touched 口径各查一次
  useEffect(() => {
    if (page === "today") void loadTodayCustomers(view);
    if (page === "pending") void loadPendingCustomers();
    if (page === "done") void loadDoneCustomers();
  }, [page, view, loadTodayCustomers, loadPendingCustomers, loadDoneCustomers]);

  const doneToday = customers.filter(
    (c) => c.done && (taskLabel === "全部" || matchLabel(c.label, taskLabel)),
  ).length;

  // 标签筛选浮层：外部点击 / Escape / 滚动 / 缩放时关闭
  useEffect(() => {
    if (!tagFilterOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (
        !popoverRef.current?.contains(e.target as Node) &&
        !triggerRef.current?.contains(e.target as Node)
      )
        setTagFilterOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setTagFilterOpen(false);
        triggerRef.current?.focus();
      }
    };
    const onScrollOrResize = () => setTagFilterOpen(false);
    document.addEventListener("click", onDocClick);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onScrollOrResize);
    window.addEventListener("scroll", onScrollOrResize, true);
    return () => {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onScrollOrResize);
      window.removeEventListener("scroll", onScrollOrResize, true);
    };
  }, [tagFilterOpen]);

  // 角色门禁：无任务页权限的角色回到看板（权限矩阵见 permissions.ts）
  if (!canViewTasks) {
    return <Navigate to="/wealth/board" replace />;
  }

  const toggleTagFilter = () => {
    if (tagFilterOpen) {
      setTagFilterOpen(false);
      return;
    }
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) {
      const width = 230;
      const height = 234;
      const left = Math.max(
        10,
        Math.min(rect.left, window.innerWidth - width - 10),
      );
      const top =
        rect.bottom + 8 + height > window.innerHeight - 10
          ? Math.max(10, rect.top - height - 8)
          : rect.bottom + 8;
      setTagFilterPos({ left, top });
    }
    setTagFilterOpen(true);
  };

  const applyTagFilter = (label: string) => {
    setTaskLabel(label);
    setTagFilterOpen(false);
    triggerRef.current?.focus();
  };

  const onPopoverKeyDown = (e: React.KeyboardEvent) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const choices = Array.from(
      popoverRef.current?.querySelectorAll<HTMLButtonElement>(
        `.${styles.tagFilterChoice}`,
      ) ?? [],
    );
    const index = choices.indexOf(document.activeElement as HTMLButtonElement);
    const next =
      e.key === "Home"
        ? 0
        : e.key === "End"
        ? choices.length - 1
        : e.key === "ArrowDown"
        ? (index + 1) % choices.length
        : (index + choices.length - 1) % choices.length;
    e.preventDefault();
    choices[next]?.focus();
  };

  const getCustomer = (id: string) => pool.find((c) => c.id === id);

  /** 客户经营方案弹窗：内容待外部经营方案接口接入，当前为空白占位 */
  const showScheme = (id: string) => {
    const c = getCustomer(id);
    if (!c) return;
    openDialog({
      title: "客户经营方案",
      body: <div className={styles.empty}>暂无内容</div>,
      buttons: [{ label: "关闭" }],
    });
  };

  /** 触达记录弹窗（原型 showResult） */
  const showResult = (id: string) => {
    const c = getCustomer(id);
    if (!c) return;
    openDialog({
      title: "客户触达记录",
      body: (
        <>
          <div className={styles.detailGrid}>
            <div>
              <small>客户姓名</small>
              <strong>{c.name}</strong>
            </div>
            <div>
              <small>触达方式</small>
              {c.channel}
            </div>
            <div>
              <small>完成时间</small>
              {c.time}
            </div>
            <div>
              <small>经营场景</small>
              {c.task}
            </div>
          </div>
          <h3>沟通记录</h3>
          <p style={{ marginTop: 10, whiteSpace: "pre-wrap" }}>{c.note}</p>
        </>
      ),
      buttons: [{ label: "关闭" }],
    });
  };

  const title =
    page === "pending"
      ? "待触达客户清单"
      : doneView
      ? "已完成任务"
      : isBiz
      ? "客户经营清单"
      : "今日客户经营清单";

  const filterActive = taskLabel !== "全部";

  return (
    <div className={cx(styles.tasksLayout, isBiz && styles.business)}>
      {isBiz && (
        <aside className={`${styles.panel} ${styles.taskTree}`}>
          <h2>工作任务</h2>
          {taskTree.length ? (
            taskTree.map((g) => (
              <details className={styles.treeGroup} key={g.category} open>
                <summary>
                  <Icon name={CATEGORY_ICONS[g.category] ?? "layer"} />
                  {g.category}
                  <Icon name="up" className={styles.arrow} />
                </summary>
                {g.nodes.map((n) => (
                  <button
                    key={n.sceneId}
                    className={cx(
                      styles.treeItem,
                      selectedTask === n.sceneName &&
                        selectedCategory === g.category &&
                        styles.active,
                    )}
                    title={`来源规划：${n.planName}`}
                    onClick={() => {
                      setSelectedTask(n.sceneName);
                      setSelectedCategory(g.category);
                    }}
                  >
                    <span className={cx(styles.tag, sourceTagClass(n.source))}>
                      {n.source}
                    </span>
                    {n.sceneName}
                  </button>
                ))}
              </details>
            ))
          ) : (
            <p className={styles.pageNote}>
              今日暂无排程中的经营场景，可在「创建计划」中新建并发布规划。
            </p>
          )}
        </aside>
      )}

      <section className={`${styles.panel} ${styles.taskPanel}`}>
        <div className={styles.sectionHead}>
          <h2>{title}</h2>
          {page === "today" ? (
            <div className={styles.switch} aria-label="任务视角">
              <button
                className={view === "business" ? styles.active : ""}
                onClick={() => {
                  setView("business");
                  setTaskLabel("全部");
                }}
              >
                经营视角
              </button>
              <button
                className={view === "customer" ? styles.active : ""}
                onClick={() => {
                  setView("customer");
                  setTaskLabel("全部");
                }}
              >
                客户视角
              </button>
            </div>
          ) : (
            <button
              className={`${styles.btn} ${styles.soft} ${styles.sm}`}
              onClick={() => navigate("/wealth/today")}
            >
              返回今日任务
            </button>
          )}
        </div>

        <div className={styles.taskSummary}>
          <div className={styles.bubble}>
            <Icon name={doneView ? "check" : "list"} />
          </div>
          <div>
            <h3>
              {doneView
                ? "客户经营任务已完成"
                : page === "pending"
                ? "待处理：客户触达"
                : isBiz
                ? selectedTask
                  ? "当前任务：" + selectedTask
                  : "今日暂无排程中的任务"
                : "今日任务：客户经营"}
            </h3>
            <div className={styles.summaryMetrics}>
              {doneView ? (
                <>
                  <span>
                    累计已完成 <b>{list.length}</b> 人
                  </span>
                  <span>
                    今日已完成 <b>{doneToday}</b> 人
                  </span>
                </>
              ) : (
                <>
                  <span>
                    目标客户 <b>{list.length}</b> 人
                  </span>
                  <span>
                    今日已触达 <b>{list.filter((c) => c.done).length}</b> 人
                  </span>
                  <span>
                    待处理{" "}
                    <b className={styles.red}>
                      {list.filter((c) => !c.done).length}
                    </b>{" "}
                    人
                  </span>
                </>
              )}
            </div>
            {isBiz && (
              <p className={styles.pageNote}>
                客户清单为演示数据，待任务实例接口接入后由定时任务执行结果生成
              </p>
            )}
          </div>
        </div>

        {page !== "today" && (
          <div className={styles.tableToolbar}>
            <span className={styles.subtle}>
              {doneView ? "触达记录与经营结果" : "优先处理尚未触达的客户"}
            </span>
            <input
              className={styles.input}
              aria-label="搜索客户或经营机会"
              placeholder="搜索客户、标签或经营机会"
              defaultValue={search}
              onChange={(e) => {
                setSearch(e.target.value);
              }}
            />
          </div>
        )}

        <div className={styles.tableWrap}>
          <table
            className={cx(
              styles.customerTable,
              doneView ? styles.completedTable : styles.opportunityTable,
            )}
          >
            <thead>
              <tr>
                <th>客户姓名</th>
                {!isBiz && (
                  <th scope="col" className={styles.tagFilterHeading}>
                    <button
                      type="button"
                      ref={triggerRef}
                      className={cx(
                        styles.tagFilterTrigger,
                        filterActive && styles.filtered,
                      )}
                      aria-label={
                        "筛选重点标签" +
                        (filterActive ? `，当前：${taskLabel}` : "")
                      }
                      aria-haspopup="dialog"
                      aria-expanded={tagFilterOpen}
                      aria-controls="wealthTagFilterPopover"
                      onClick={toggleTagFilter}
                    >
                      <span>重点标签</span>
                      <span className={styles.tagFilterIcon}>
                        <svg viewBox="0 0 20 20" aria-hidden="true">
                          <path d="M3 4h14l-5.5 6v5l-3 1v-6Z" />
                        </svg>
                        {filterActive && <i></i>}
                      </span>
                    </button>
                    {filterActive && (
                      <div className={styles.tagFilterApplied}>
                        <span>{taskLabel}</span>
                        <button
                          type="button"
                          aria-label="清除重点标签筛选"
                          title="清除筛选"
                          onClick={() => applyTagFilter("全部")}
                        >
                          ×
                        </button>
                      </div>
                    )}
                  </th>
                )}
                <th className={styles.opportunityCol}>
                  {doneView ? "经营任务" : "经营机会"}
                </th>
                <th>{doneView ? "触达方式" : "经营方案"}</th>
                <th className={styles.executionCol}>
                  {doneView ? "完成时间" : "执行"}
                </th>
                {doneView && <th>经营结果</th>}
              </tr>
            </thead>
            <tbody>
              {list.length ? (
                list.map((c) => (
                  <tr key={c.id}>
                    <td className={styles.name}>{c.name}</td>
                    {!isBiz && (
                      <td>
                        <span
                          className={cx(styles.tag, labelTagClass(c.label))}
                        >
                          {c.label}
                        </span>
                      </td>
                    )}
                    <td className={styles.reason}>
                      {doneView ? (
                        c.task
                      ) : (
                        <Opportunities customer={c} multiple={!isBiz} />
                      )}
                    </td>
                    <td>
                      {doneView ? (
                        <>
                          <Icon
                            name={
                              c.channel === "电话"
                                ? "phone"
                                : c.channel === "企微"
                                ? "chat"
                                : "user"
                            }
                          />
                          {"　"}
                          {c.channel}
                        </>
                      ) : (
                        <button
                          className={styles.link}
                          onClick={() => showScheme(c.id)}
                        >
                          查看经营方案
                        </button>
                      )}
                    </td>
                    <td className={styles.executionCol}>
                      {doneView ? (
                        c.time
                      ) : (
                        <div className={styles.contactActions}>
                          <button
                            className={`${styles.btn} ${styles.primary}`}
                            onClick={() => openOutboundLink("dial", c)}
                          >
                            <Icon name="phone" />
                            电访
                          </button>
                          <button
                            className={styles.btn}
                            onClick={() => openOutboundLink("insight", c)}
                          >
                            <Icon name="user" />
                            客户洞察
                          </button>
                        </div>
                      )}
                    </td>
                    {doneView && (
                      <td>
                        <button
                          className={styles.link}
                          onClick={() => showResult(c.id)}
                        >
                          查看记录
                        </button>
                      </td>
                    )}
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={doneView ? 6 : isBiz ? 4 : 5}>
                    <div className={styles.empty}>
                      {(
                        page === "pending"
                          ? pendingLoading
                          : page === "done"
                          ? doneLoading
                          : customersLoading
                      )
                        ? "客户清单加载中…"
                        : isBiz && selectedTask && !search.trim()
                        ? `「${selectedTask}」暂无客户名单，待定时任务执行后生成`
                        : "暂无符合条件的客户"}
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className={styles.pagination}>
          <span className={styles.total}>共 {list.length} 条记录</span>
        </div>
      </section>

      {tagFilterOpen && !isBiz && (
        <div
          className={styles.tagFilterPopover}
          id="wealthTagFilterPopover"
          role="dialog"
          aria-label="筛选重点标签"
          ref={popoverRef}
          style={{ left: tagFilterPos.left, top: tagFilterPos.top }}
          onKeyDown={onPopoverKeyDown}
        >
          <div className={styles.tagFilterPopoverTitle}>
            筛选重点标签<span>单选</span>
          </div>
          <div>
            {["全部", ...labelOptionsOf(customers)].map((label) => {
              const base = buildList(true);
              const count =
                label === "全部"
                  ? base.length
                  : base.filter((c) => matchLabel(c.label, label)).length;
              return (
                <button
                  type="button"
                  key={label}
                  className={cx(
                    styles.tagFilterChoice,
                    taskLabel === label && styles.selected,
                  )}
                  aria-pressed={taskLabel === label}
                  onClick={() => applyTagFilter(label)}
                >
                  <span
                    className={
                      label === "全部"
                        ? styles.allTagText
                        : cx(styles.tag, labelTagClass(label))
                    }
                  >
                    {label === "全部" ? "全部标签" : label}
                  </span>
                  <span className={styles.tagFilterChoiceCount}>{count}</span>
                  <span
                    className={styles.tagFilterChoiceCheck}
                    aria-hidden="true"
                  >
                    {taskLabel === label ? "✓" : ""}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
