/**
 * 智能财富工作台 —— 规划看板页
 * 对应原型 boardHTML：周期筛选、本期规划概览、当前工作重点（日历/列表双模式）。
 */
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import cx from "classnames";
import styles from "../index.module.less";
import { Icon } from "../components/Icon";
import { PLATFORM_CAPABILITY_COUNT } from "../mock/data";
import {
  addCalendarDays,
  calendarDate,
  calendarRange,
  cycleRange,
  dateKey,
  planFrequency,
  planScheduledOn,
  scheduleLabel,
  todayKey,
} from "../utils";
import { selectCurrentAccount, useWealthStore } from "../store";
import { Calendar } from "./Calendar";

const BOARD_TABS = ["全部", "分行关注", "行长关注", "我的关注"];
const PERIODS = ["本季", "本月", "本周", "今日", "T+1日"];

/** 发布中轮询间隔（毫秒） */
const PUBLISH_POLL_INTERVAL = 3000;

/** 状态着色：失败→红；发布中/分发中→蓝；已下发等稳定态→默认 */
function statusClass(status: string) {
  if (status.includes("失败")) return styles.red;
  if (status.includes("发布中") || status.includes("分发中")) {
    return styles.blue;
  }
  return "";
}

/** 列表模式 / 详情弹窗共用的规划操作按钮语义由父级注入 */

export default function Board() {
  const account = useWealthStore(selectCurrentAccount);
  const plans = useWealthStore((s) => s.plans);
  const openDialog = useWealthStore((s) => s.openDialog);
  const closeDialog = useWealthStore((s) => s.closeDialog);
  const editPlan = useWealthStore((s) => s.editPlan);
  const removePlan = useWealthStore((s) => s.removePlan);
  const refreshPlans = useWealthStore((s) => s.refreshPlans);
  const navigate = useNavigate();

  const [boardTab, setBoardTab] = useState("全部");
  const [period, setPeriod] = useState("本月");
  const [boardMode, setBoardMode] = useState<"calendar" | "list">("calendar");
  const [dimension, setDimension] = useState<"week" | "month">("month");
  const [anchor, setAnchor] = useState(todayKey);

  const range: [string, string] =
    boardMode === "calendar"
      ? calendarRange(anchor, dimension)
      : cycleRange(period) ?? [todayKey(), todayKey()];
  const [rangeStart, rangeEnd] = range;

  const all = useMemo(
    () => plans.filter((p) => p.start <= rangeEnd && p.end >= rangeStart),
    [plans, rangeStart, rangeEnd],
  );
  const visible = useMemo(
    () => all.filter((p) => boardTab === "全部" || p.source === boardTab),
    [all, boardTab],
  );

  // 已生成任务数 / 任务执行率的统计依赖任务实例接口，待接入后恢复计算（见下方统计卡注释）
  const sceneCount = new Set(
    all.flatMap((p) => p.items?.map((x) => x.id) ?? []),
  ).size;

  // 发布中/分发中的规划都需要轮询，直至发布与分发状态收敛
  const hasActive = plans.some(
    (p) =>
      p.publishStatus === "publishing" ||
      p.status.includes("发布中") ||
      p.status.includes("分发中"),
  );
  useEffect(() => {
    if (!hasActive) return;
    const timer = setInterval(() => void refreshPlans(), PUBLISH_POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [hasActive, refreshPlans]);

  const onEditPlan = (id: string) => {
    editPlan(id);
    navigate("/wealth/create");
  };

  const onRemovePlan = (id: string) => {
    const p = plans.find((x) => x.id === id);
    if (!p) return;
    openDialog({
      title: "移除规划",
      body: (
        <>
          <p>确定移除「{p.name}」？</p>
          <p>移除后，该规划将不再显示在当前工作重点中。</p>
        </>
      ),
      buttons: [
        { label: "取消" },
        {
          label: "确认移除",
          danger: true,
          onClick: () => void removePlan(id),
        },
      ],
    });
  };

  /** 工作任务详情弹窗（原型 showCalendarDetails） */
  const showCalendarDetails = (day: string, id?: string) => {
    const entries = all.filter(
      (p) =>
        (boardTab === "全部" || p.source === boardTab) &&
        planScheduledOn(p, day) &&
        (id === undefined || p.id === id),
    );
    openDialog({
      title: "工作任务详情",
      wide: true,
      body: (
        <>
          <div className={styles.calendarDetailCaption}>
            <span>
              {day.replace(/-/g, " / ")}
              {"　·　"}
              {id === undefined
                ? "当日工作任务"
                : entries[0]?.name ?? "工作任务"}
            </span>
            <span>共 {entries.length} 项</span>
          </div>
          <div className={styles.tableWrap}>
            <table className={styles.calendarDetailTable}>
              <thead>
                <tr>
                  <th>工作任务</th>
                  <th>关注来源</th>
                  <th>经营方向</th>
                  <th>执行时间</th>
                  <th>执行频率</th>
                  <th>目标客户</th>
                  <th>已生成任务</th>
                  <th>任务执行率</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {entries.length ? (
                  entries.map((p) => (
                    <tr key={p.id}>
                      <td className={styles.detailName}>{p.name}</td>
                      <td>
                        <span
                          className={cx(
                            styles.tag,
                            p.source === "分行关注"
                              ? styles.red
                              : styles.purple,
                          )}
                        >
                          {p.source}
                        </span>
                      </td>
                      <td className={styles.detailDirection}>
                        {p.desc}
                        {p.items?.length ? (
                          <div className={styles.detailTaskSchedules}>
                            {p.items.map((x, i) => (
                              <span key={x.id}>
                                {i > 0 && <br />}
                                {x.sceneName}：{x.start} 至 {x.end} ·{" "}
                                {scheduleLabel(x.schedule)}
                              </span>
                            ))}
                          </div>
                        ) : null}
                      </td>
                      <td className={styles.detailTime}>
                        {p.start}
                        <br />至 {p.end}
                      </td>
                      <td>
                        <span className={styles.tag}>{planFrequency(p)}</span>
                      </td>
                      <td>{p.customers} 人</td>
                      <td>{p.tasks} 个</td>
                      <td>
                        <div className={styles.progressLine}>
                          <div className={styles.progress}>
                            <span style={{ width: `${p.rate}%` }}></span>
                          </div>
                          <b>{p.rate}%</b>
                        </div>
                      </td>
                      <td>
                        <span
                          className={cx(styles.status, statusClass(p.status))}
                        >
                          <i className={styles.dot}></i>
                          {p.status}
                        </span>
                      </td>
                      <td>
                        {p.editable !== false ? (
                          <div className={styles.actions}>
                            <button
                              className={`${styles.btn} ${styles.sm} ${styles.soft}`}
                              onClick={() => {
                                closeDialog();
                                onEditPlan(p.id);
                              }}
                            >
                              修改规划
                            </button>
                            <button
                              className={`${styles.btn} ${styles.sm}`}
                              onClick={() => onRemovePlan(p.id)}
                            >
                              移除规划
                            </button>
                          </div>
                        ) : (
                          <span className={styles.readonlyHint}>只读</span>
                        )}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={10}>
                      <div className={styles.empty}>
                        当日暂无符合筛选条件的工作任务
                      </div>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <p className={styles.pageNote}>
            详情与列表模式使用同一份规划信息，数据为整个规划执行周期的累计值。
          </p>
        </>
      ),
      buttons: [{ label: "关闭" }],
    });
  };

  const stats = [
    {
      icon: "list",
      n: String(all.length),
      title: "当前规划",
      detail: "本期经营方向",
      val: `${all.length} 项`,
      up: false,
    },
    {
      icon: "layer",
      n: String(sceneCount),
      title: "覆盖经营场景",
      detail: "可用能力",
      val: String(PLATFORM_CAPABILITY_COUNT),
      up: false,
    },
    {
      icon: "check",
      // n: total.toLocaleString(),
      n: "--",
      title: "已生成任务",
      detail: "较上月",
      val: "+18% ↗",
      up: true,
    },
    {
      icon: "users",
      // n: rate + "%",
      n: "--",
      title: "任务执行率",
      detail: "较上月",
      val: "+5% ↗",
      up: true,
    },
  ];

  return (
    <>
      <div className={styles.filters}>
        {boardMode === "list" ? (
          <>
            <span className={styles.label}>规划周期</span>
            <div className={styles.pills}>
              {PERIODS.map((p) => (
                <button
                  key={p}
                  className={cx(styles.pill, period === p && styles.active)}
                  aria-pressed={period === p}
                  onClick={() => setPeriod(p)}
                >
                  {p}
                </button>
              ))}
            </div>
          </>
        ) : (
          <span className={styles.label}>看板周期</span>
        )}
        <div className={styles.datebox}>
          <Icon name="calendar" />
          {range.map((x) => x.replace(/-/g, ".")).join(" — ")}
        </div>
        <div className={styles.role}>
          当前角色：{account?.role} <Icon name="building" />
        </div>
      </div>

      <section className={`${styles.panel} ${styles.overview}`}>
        <h2>本期规划概览</h2>
        <div className={styles.stats}>
          {stats.map((s) => (
            <div className={styles.stat} key={s.title}>
              <div className={styles.bubble}>
                <Icon name={s.icon} />
              </div>
              <div>
                <strong>{s.n}</strong>
                <b>{s.title}</b>
                <p>
                  {s.detail}
                  <em className={s.up ? styles.up : ""}>{s.val}</em>
                </p>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className={`${styles.panel} ${styles.focus}`}>
        <div className={styles.sectionHead}>
          <h2>当前工作重点</h2>
          <div className={styles.tabs}>
            {BOARD_TABS.map((t) => (
              <button
                key={t}
                className={cx(styles.tab, boardTab === t && styles.active)}
                onClick={() => setBoardTab(t)}
              >
                {t}（
                {t === "全部"
                  ? all.length
                  : all.filter((p) => p.source === t).length}
                ）
              </button>
            ))}
          </div>
          <div
            className={`${styles.switch} ${styles.boardViewSwitch}`}
            aria-label="工作重点展示模式"
          >
            <button
              className={boardMode === "calendar" ? styles.active : ""}
              aria-pressed={boardMode === "calendar"}
              onClick={() => setBoardMode("calendar")}
            >
              <Icon name="calendar" />
              看板模式
            </button>
            <button
              className={boardMode === "list" ? styles.active : ""}
              aria-pressed={boardMode === "list"}
              onClick={() => setBoardMode("list")}
            >
              <Icon name="list" />
              列表模式
            </button>
          </div>
        </div>
        {boardMode === "calendar" ? (
          <Calendar
            visible={visible}
            dimension={dimension}
            anchor={anchor}
            onShift={(delta) => {
              if (dimension === "week") {
                setAnchor((a) => addCalendarDays(a, delta * 7));
              } else {
                setAnchor((a) => {
                  const d = calendarDate(a);
                  return dateKey(
                    new Date(d.getFullYear(), d.getMonth() + delta, 1, 12),
                  );
                });
              }
            }}
            onToday={() => setAnchor(todayKey())}
            onDimension={setDimension}
            onShowDetails={showCalendarDetails}
          />
        ) : (
          <div className={styles.scrollX}>
            <div className={styles.planList}>
              {visible.length ? (
                visible.map((p, i) => (
                  <article className={styles.planRow} key={p.id}>
                    <span className={styles.rank}>{i + 1}</span>
                    <div>
                      <span
                        className={cx(
                          styles.tag,
                          p.source === "分行关注" ? styles.red : styles.purple,
                        )}
                      >
                        {p.source}
                      </span>
                    </div>
                    <div>
                      <div className={styles.planTitle}>{p.name}</div>
                      <p className={styles.planDesc}>{p.desc}</p>
                      <div className={styles.planDate}>
                        <Icon name="calendar" />
                        {p.period}（{p.start.slice(5).replace("-", ".")} -{" "}
                        {p.end.slice(5).replace("-", ".")}）
                        <span className={styles.planFrequency}>
                          {planFrequency(p)}
                        </span>
                      </div>
                    </div>
                    <div className={styles.metric}>
                      <small>目标客户</small>
                      <strong>{p.customers} 人</strong>
                    </div>
                    <div className={styles.metric}>
                      <small>已生成任务</small>
                      <strong>{p.tasks} 个</strong>
                    </div>
                    <div className={styles.metric}>
                      <small>任务执行率</small>
                      <div className={styles.progressLine}>
                        <div className={styles.progress}>
                          <span style={{ width: `${p.rate}%` }}></span>
                        </div>
                        <b>{p.rate}%</b>
                      </div>
                    </div>
                    <div className={styles.metric}>
                      <small>状态</small>
                      <span
                        className={cx(styles.status, statusClass(p.status))}
                      >
                        <i className={styles.dot}></i>
                        {p.status}
                      </span>
                    </div>
                    <div className={styles.actions}>
                      {p.editable !== false ? (
                        <>
                          <button
                            className={`${styles.btn} ${styles.sm} ${styles.soft}`}
                            onClick={() => onEditPlan(p.id)}
                          >
                            修改规划
                          </button>
                          <button
                            className={`${styles.btn} ${styles.sm}`}
                            onClick={() => onRemovePlan(p.id)}
                          >
                            移除规划
                          </button>
                        </>
                      ) : (
                        <span className={styles.readonlyHint}>只读</span>
                      )}
                    </div>
                  </article>
                ))
              ) : (
                <div className={styles.empty}>当前筛选条件下暂无规划</div>
              )}
            </div>
          </div>
        )}
      </section>
    </>
  );
}
