/**
 * 智能财富工作台 —— 创建计划页（新建 / 修改工作规划）
 * 对应原型 createHTML：场景选择、方向与排程配置、规划名称、
 * 分发目标（仅支行行长/分行中台）、发布确认。
 * 执行排程与控制台定时任务同一模型（每小时/每日/每周/自定义 cron）。
 */
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import cx from "classnames";
import type { CronType } from "@/utils/parseCron";
import styles from "../index.module.less";
import { Icon } from "../components/Icon";
import { TargetPicker } from "../components/TargetPicker";
import { TimeField } from "../components/TimeField";
import { SCENE_CATEGORIES } from "../mock/data";
import { needsDistributeTargets } from "../permissions";
import {
  scheduleLabel,
  selectCurrentAccount,
  useWealthStore,
  validateDraft,
} from "../store";
import { findSceneConflicts } from "../utils";
import type { PlanItem, Scene } from "../types";

const STEP_DEFS = [
  {
    n: 1,
    title: "选择经营场景",
    sub: "选择基础场景与经营场景",
    target: "scene",
  },
  {
    n: 2,
    title: "设置经营方向",
    sub: "配置经营目标与执行要求",
    target: "direction",
  },
  {
    n: 3,
    title: "任务周期与排程",
    sub: "按场景设置有效期与执行排程",
    target: "direction",
  },
] as const;

/** 仅支行行长/分行中台可见的第 4 步 */
const TARGET_STEP = {
  n: 4,
  title: "选择分发目标",
  sub: "选择接收规划的客户经理",
  target: "target",
} as const;

const CYCLES = ["本月", "本季", "今日", "T+1日", "自定义"];

/** 执行频率选项：与控制台定时任务表单一致 */
const SCHEDULE_TYPES: { value: CronType; label: string }[] = [
  { value: "hourly", label: "每小时" },
  { value: "daily", label: "每日" },
  { value: "weekly", label: "每周" },
  { value: "custom", label: "自定义" },
];

const WEEKDAYS: { value: string; label: string }[] = [
  { value: "mon", label: "一" },
  { value: "tue", label: "二" },
  { value: "wed", label: "三" },
  { value: "thu", label: "四" },
  { value: "fri", label: "五" },
  { value: "sat", label: "六" },
  { value: "sun", label: "日" },
];

/**
 * 发布确认弹窗里的分发目标名单：
 * 默认最多 2 行，超出省略并出现「查看更多 / 收起」开关。
 * 溢出判定用隐藏孪生节点测量真实高度——line-clamp 下 scrollHeight
 * 也会被截断，不能直接用来判断是否溢出。
 */
function TargetNamesSummary({ sapIds }: { sapIds: string[] }) {
  const targets = useWealthStore((s) => s.targets);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const ref = useRef<HTMLParagraphElement>(null);
  const measureRef = useRef<HTMLParagraphElement>(null);
  const names = sapIds
    .map((id) => targets.find((t) => t.sapId === id)?.name ?? id)
    .join("、");

  useEffect(() => {
    const el = ref.current;
    const measure = measureRef.current;
    if (!el || !measure) return;
    const update = () => {
      const lineHeight = parseFloat(getComputedStyle(el).lineHeight) || 21;
      setOverflowing(measure.scrollHeight > lineHeight * 2 + 1);
    };
    update();
    // 弹窗宽度变化（如窗口缩放）时重新判定是否溢出
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [names]);

  return (
    <div className={styles.targetSummary}>
      <small>分发目标（{sapIds.length} 人）</small>
      <p
        ref={ref}
        className={cx(styles.targetNames, !expanded && styles.clamped)}
      >
        {names}
      </p>
      {/* 隐藏孪生节点：同宽、不截断，用于测量真实内容高度 */}
      <p
        ref={measureRef}
        className={cx(styles.targetNames, styles.measure)}
        aria-hidden="true"
      >
        {names}
      </p>
      {(overflowing || expanded) && (
        <button
          className={styles.targetMore}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "收起" : "查看更多"}
        </button>
      )}
    </div>
  );
}

/**
 * 单个场景的排程配置块：执行频率模型与控制台定时任务一致
 * （每小时 / 每日 / 每周 / 自定义 cron），序列化复用 @/utils/parseCron。
 */
function ScheduleEditor({
  item,
  sceneName,
}: {
  item: PlanItem;
  sceneName: string;
}) {
  const updateSchedule = useWealthStore((s) => s.updateSchedule);
  const schedule = item.schedule;
  const type = schedule.type;

  return (
    <div className={styles.scheduleBlock}>
      <div className={styles.cycle}>
        <span>执行频率</span>
        <div className={styles.cycleGroup}>
          {SCHEDULE_TYPES.map((t) => (
            <button
              key={t.value}
              className={type === t.value ? styles.active : ""}
              aria-pressed={type === t.value}
              onClick={() => updateSchedule(item.id, { type: t.value })}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      {(type === "daily" || type === "weekly") && (
        <div className={styles.scheduleField}>
          <span>执行时刻</span>
          <TimeField
            hour={schedule.hour ?? 9}
            minute={schedule.minute ?? 0}
            onChange={(hour, minute) =>
              updateSchedule(item.id, { hour, minute })
            }
            ariaLabel={`${sceneName}执行时刻`}
          />
        </div>
      )}
      {type === "weekly" && (
        <div className={styles.scheduleField}>
          <span>执行日</span>
          <div className={styles.cycleGroup}>
            {WEEKDAYS.map((d) => {
              const active = schedule.daysOfWeek?.includes(d.value) ?? false;
              return (
                <button
                  key={d.value}
                  className={active ? styles.active : ""}
                  aria-pressed={active}
                  onClick={() => {
                    const current = schedule.daysOfWeek ?? [];
                    updateSchedule(item.id, {
                      daysOfWeek: active
                        ? current.filter((x) => x !== d.value)
                        : [...current, d.value],
                    });
                  }}
                >
                  {d.label}
                </button>
              );
            })}
          </div>
        </div>
      )}
      {type === "custom" && (
        <label className={styles.scheduleField}>
          <span>cron 表达式</span>
          <input
            aria-label={`${sceneName}cron表达式`}
            placeholder="0 9 * * *"
            value={schedule.rawCron ?? ""}
            onChange={(e) =>
              updateSchedule(item.id, { rawCron: e.target.value })
            }
          />
        </label>
      )}
      <div className={styles.taskRangeLabel}>{scheduleLabel(schedule)}</div>
    </div>
  );
}

/** 单个场景的任务周期（有效期）配置块 */
function TaskSchedule({ item, scene }: { item: PlanItem; scene: Scene }) {
  const setTaskCycle = useWealthStore((s) => s.setTaskCycle);
  const setItemDates = useWealthStore((s) => s.setItemDates);
  const startDateRef = useRef<HTMLInputElement>(null);
  const endDateRef = useRef<HTMLInputElement>(null);

  const openDatePicker = (ref: { current: HTMLInputElement | null }) => {
    ref.current?.showPicker?.();
  };

  return (
    <>
      <div className={styles.taskCycleBlock}>
        <div className={styles.cycle}>
          <span>任务周期</span>
          <div className={styles.cycleGroup}>
            {CYCLES.map((c) => (
              <button
                key={c}
                className={item.cycle === c ? styles.active : ""}
                aria-pressed={item.cycle === c}
                onClick={() => setTaskCycle(scene.id, c)}
              >
                {c}
              </button>
            ))}
          </div>
        </div>
        {item.cycle === "自定义" ? (
          <div className={styles.taskCustomDates}>
            <input
              ref={startDateRef}
              type="date"
              aria-label={`${scene.name}任务开始日期`}
              value={item.start ?? ""}
              onClick={() => openDatePicker(startDateRef)}
              onChange={(e) => setItemDates(scene.id, "start", e.target.value)}
            />
            <span>至</span>
            <input
              ref={endDateRef}
              type="date"
              aria-label={`${scene.name}任务结束日期`}
              value={item.end ?? ""}
              onClick={() => openDatePicker(endDateRef)}
              onChange={(e) => setItemDates(scene.id, "end", e.target.value)}
            />
          </div>
        ) : (
          <div className={styles.taskRangeLabel}>
            {item.start} 至 {item.end}
          </div>
        )}
      </div>
      <ScheduleEditor item={item} sceneName={scene.name} />
    </>
  );
}

export default function Create() {
  const account = useWealthStore(selectCurrentAccount);
  const draft = useWealthStore((s) => s.draft);
  const scenesByCategory = useWealthStore((s) => s.scenesByCategory);
  const scenesLoading = useWealthStore((s) => s.scenesLoading);
  const loadScenes = useWealthStore((s) => s.loadScenes);
  // 「保存草稿」功能暂缓上线，入口已注释；恢复时一并解开下方 saveDraft 与 savedAt
  // const savedAt = useWealthStore((s) => s.savedAt);
  const editingId = useWealthStore((s) => s.editingId);
  const toggleScene = useWealthStore((s) => s.toggleScene);
  const moveScene = useWealthStore((s) => s.moveScene);
  const dropScene = useWealthStore((s) => s.dropScene);
  const setItemDirection = useWealthStore((s) => s.setItemDirection);
  const setDraftName = useWealthStore((s) => s.setDraftName);
  const normalizeDraft = useWealthStore((s) => s.normalizeDraft);
  const clearEditingId = useWealthStore((s) => s.clearEditingId);
  // const saveDraft = useWealthStore((s) => s.saveDraft);
  const publishPlan = useWealthStore((s) => s.publishPlan);
  const openDialog = useWealthStore((s) => s.openDialog);
  const toast = useWealthStore((s) => s.toast);
  const targetSapIds = useWealthStore((s) => s.targetSapIds);
  const plans = useWealthStore((s) => s.plans);
  const navigate = useNavigate();

  const [filterCategory, setFilterCategory] = useState("全部");
  const [activeSteps, setActiveSteps] = useState<number[]>([0, 1]);
  const dragId = useRef<string | null>(null);

  const sceneRef = useRef<HTMLElement>(null);
  const directionRef = useRef<HTMLElement>(null);
  const targetRef = useRef<HTMLElement>(null);

  const needsTargets = needsDistributeTargets(account?.id ?? "rm");
  const stepDefs = needsTargets ? [...STEP_DEFS, TARGET_STEP] : STEP_DEFS;

  // 与原型一致：进入创建页时归一化草稿周期/排程；离开时清除编辑态（草稿保留）
  useEffect(() => {
    normalizeDraft();
    return () => clearEditingId();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 大类标签 → 英文 code；「全部」传空串。按当前选中大类懒加载场景（会话级缓存）
  const filterCode =
    filterCategory === "全部"
      ? ""
      : SCENE_CATEGORIES.find((c) => c.label === filterCategory)?.code ?? "";
  useEffect(() => {
    void loadScenes(filterCode);
  }, [filterCode, loadScenes]);

  const scrollTo = (
    target: "scene" | "direction" | "target",
    index: number,
  ) => {
    const el =
      target === "scene"
        ? sceneRef.current
        : target === "direction"
        ? directionRef.current
        : targetRef.current;
    el?.scrollIntoView({ behavior: "smooth" });
    setActiveSteps([index]);
  };

  const onPublish = () => {
    const error = validateDraft(draft);
    if (error) {
      toast(error);
      return;
    }
    if (needsTargets && !targetSapIds.length) {
      toast("请至少选择一个分发目标");
      return;
    }
    const currentId = editingId;
    // 行长/中台：草稿场景若已被其他已发布/发布中的规划占用，禁止重复新建发布
    const conflicts = needsTargets
      ? findSceneConflicts(plans, draft, currentId)
      : [];
    openDialog({
      title: currentId ? "保存规划修改" : "发布工作规划",
      body: (
        <>
          <p>请确认以下规划配置：</p>
          {conflicts.length > 0 && (
            <p className={styles.pageNote}>
              <b className={styles.red}>
                以下经营场景已发布过：
                {conflicts
                  .map(
                    (c) =>
                      `「${c.scene.sceneName}」（见规划「${c.planName}」）`,
                  )
                  .join("、")}
                。请前往规划看板编辑对应规划，无需新建。
              </b>
            </p>
          )}
          <div className={styles.detailGrid}>
            <div>
              <small>规划名称</small>
              <strong>{draft.name}</strong>
            </div>
            <div>
              <small>经营场景</small>
              <strong>{draft.items.length} 个</strong>
            </div>
          </div>
          {needsTargets && <TargetNamesSummary sapIds={targetSapIds} />}
          {draft.items.map((x, i) => (
            <div className={styles.recommendation} key={x.id}>
              <h3>
                {i + 1}. {x.sceneName}
              </h3>
              <p style={{ margin: 0 }}>
                任务周期：{x.cycle}（{x.start} 至 {x.end}）
                <br />
                执行排程：{scheduleLabel(x.schedule)}
                <br />
                经营方向：{x.direction}
              </p>
            </div>
          ))}
          <p className={styles.pageNote}>
            确认后规划即刻入库，定时任务创建与分发在后台异步完成，可在看板查看进度。
          </p>
        </>
      ),
      buttons: [
        { label: "继续编辑" },
        {
          label: currentId ? "确认修改" : "确认发布",
          primary: true,
          disabled: conflicts.length > 0,
          onClick: () => {
            void publishPlan().then((ok) => {
              if (ok) navigate("/wealth/board");
            });
          },
        },
      ],
    });
  };

  const visibleScenes = scenesByCategory[filterCode] ?? [];
  const allScenes = Object.values(scenesByCategory).flat();

  return (
    <>
      <div className={styles.pageHeading}>
        <h1>{editingId ? "修改工作规划" : "新建工作规划"}</h1>
        <p>人指方向，Agent 自动生成名单与方案并下发任务</p>
        <div className={styles.actions}>
          <button
            className={styles.btn}
            onClick={() => navigate("/wealth/board")}
          >
            取消
          </button>
          <button
            className={`${styles.btn} ${styles.primary}`}
            onClick={onPublish}
          >
            {editingId ? "保存修改" : "发布规划"}
          </button>
        </div>
      </div>

      <div className={`${styles.panel} ${styles.steps}`}>
        {stepDefs.map((s, i) => (
          <span key={s.n} style={{ display: "contents" }}>
            {i > 0 && <div className={styles.stepLine}></div>}
            <button
              className={cx(
                styles.step,
                activeSteps.includes(i) && styles.active,
              )}
              onClick={() => scrollTo(s.target, i)}
            >
              <span className={styles.stepNum}>
                {s.n === 1 && draft.items.length ? "✓" : s.n}
              </span>
              <span>
                <strong>
                  {s.n}
                  {"　"}
                  {s.title}
                </strong>
                <small>{s.sub}</small>
              </span>
            </button>
          </span>
        ))}
      </div>

      <div className={`${styles.panel} ${styles.createPanel}`}>
        <section className={styles.formSection} ref={sceneRef}>
          <h3>1. 选择经营场景</h3>
          <div className={styles.categoryFilter}>
            <strong>产品大类筛选</strong>
            <div className={styles.pills}>
              {["全部", ...SCENE_CATEGORIES.map((c) => c.label)].map((c) => (
                <button
                  key={c}
                  className={cx(
                    styles.pill,
                    filterCategory === c && styles.active,
                  )}
                  onClick={() => setFilterCategory(c)}
                >
                  {c}
                </button>
              ))}
            </div>
          </div>
          <div className={styles.scenes}>
            {scenesLoading && !(filterCode in scenesByCategory) ? (
              <div className={styles.empty} style={{ gridColumn: "1/-1" }}>
                场景加载中…
              </div>
            ) : visibleScenes.length ? (
              visibleScenes.map((s, i) => {
                const chosen = draft.items.some((x) => x.id === s.id);
                return (
                  <button
                    key={s.id}
                    className={cx(
                      styles.scene,
                      i < 3 && filterCategory === "全部" && styles.featured,
                      chosen && styles.selected,
                    )}
                    aria-pressed={chosen}
                    onClick={() => toggleScene(s.id)}
                  >
                    <span className={styles.sceneCheck}>
                      {chosen ? "✓" : ""}
                    </span>
                    <div className={styles.bubble}>
                      <Icon name={s.icon} />
                    </div>
                    <div>
                      <div className={styles.sceneTitle}>
                        {s.name}
                        <span className={styles.tag}>{s.category}</span>
                      </div>
                      <p>{s.desc}</p>
                      <span className={styles.status}>
                        <i className={styles.dot}></i>
                        AI能力已就绪
                        <span
                          className={styles.muted}
                          style={{ marginLeft: 6 }}
                        >
                          · {s.source}
                        </span>
                      </span>
                    </div>
                  </button>
                );
              })
            ) : (
              <div className={styles.empty} style={{ gridColumn: "1/-1" }}>
                该产品大类暂无可用经营场景
              </div>
            )}
          </div>
        </section>

        <section className={styles.formSection} ref={directionRef}>
          <h3>
            2. 已选择的规划场景{" "}
            <span style={{ color: "var(--blue)" }}>
              （{draft.items.length}）
            </span>
            <span
              className={styles.muted}
              style={{ fontSize: 12, fontWeight: 400, marginLeft: 8 }}
            >
              拖动调整顺序，或使用上下箭头
            </span>
          </h3>
          <div className={styles.selectedList}>
            {draft.items.length ? (
              draft.items.map((x, i) => {
                const s = allScenes.find((sc) => sc.id === x.id);
                const display: Scene =
                  s ??
                  ({
                    id: x.id,
                    name: x.sceneName,
                    category: x.categoryLabel,
                    categoryCode: x.categoryCode,
                    icon: "layer",
                    desc: x.direction,
                    source: "",
                    mcpRelations: x.mcpRelations,
                  } as Scene);
                return (
                  <div
                    className={styles.selectedRow}
                    key={x.id}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={(e) => {
                      e.preventDefault();
                      if (dragId.current != null)
                        dropScene(dragId.current, x.id);
                    }}
                  >
                    <span
                      className={styles.drag}
                      draggable
                      onDragStart={(e) => {
                        dragId.current = x.id;
                        e.dataTransfer.setData("text/plain", x.id);
                      }}
                      title="拖动排序"
                    >
                      ⠿
                    </span>
                    <span className={styles.order}>{i + 1}</span>
                    <div className={styles.selectedMain}>
                      <div className={styles.selectedTitle}>
                        {x.sceneName}
                        <span className={styles.tag}>{x.categoryLabel}</span>
                      </div>
                      <label className={styles.direction}>
                        <span>
                          经营方向 <b style={{ color: "var(--red)" }}>·</b>
                        </span>
                        <input
                          aria-label={`${x.sceneName}的经营方向`}
                          value={x.direction}
                          maxLength={120}
                          onChange={(e) =>
                            setItemDirection(x.id, e.target.value)
                          }
                        />
                      </label>
                    </div>
                    <TaskSchedule item={x} scene={display} />
                    <div className={styles.rowTools}>
                      <button
                        className={styles.iconbtn}
                        title="上移"
                        aria-label={`上移${x.sceneName}`}
                        disabled={i === 0}
                        onClick={() => moveScene(i, -1)}
                      >
                        <Icon name="up" />
                      </button>
                      <button
                        className={styles.iconbtn}
                        title="下移"
                        aria-label={`下移${x.sceneName}`}
                        disabled={i === draft.items.length - 1}
                        onClick={() => moveScene(i, 1)}
                      >
                        <Icon name="down" />
                      </button>
                      <button
                        className={styles.iconbtn}
                        title="移除"
                        aria-label={`移除${x.sceneName}`}
                        onClick={() => toggleScene(x.id)}
                      >
                        <Icon name="trash" />
                      </button>
                    </div>
                  </div>
                );
              })
            ) : (
              <div className={`${styles.empty} ${styles.panel}`}>
                请先选择至少一个经营场景
              </div>
            )}
          </div>
        </section>

        <section className={styles.formSection}>
          <h3>3. 规划名称</h3>
          <div className={styles.bottomForm}>
            <div className={styles.field}>
              <label htmlFor="wealthPlanName">规划名称</label>
              <input
                className={styles.input}
                id="wealthPlanName"
                value={draft.name}
                maxLength={40}
                placeholder="请输入规划名称"
                onChange={(e) => setDraftName(e.target.value)}
              />
            </div>
          </div>
          <p className={styles.pageNote}>
            任务周期是场景的有效期，执行排程决定定时任务的触发节奏（每小时/每日/每周/自定义）；经营名单生成后自动下发。
          </p>
        </section>

        {needsTargets && (
          <section className={styles.formSection} ref={targetRef}>
            <h3>
              4. 选择分发目标{" "}
              <span style={{ color: "var(--blue)" }}>
                （已选 {targetSapIds.length} 人）
              </span>
            </h3>
            <TargetPicker />
          </section>
        )}
      </div>

      <div className={styles.footerActions}>
        {/* 「保存草稿」功能暂缓上线：入口与提示先注释，store/api 能力保留
        <span className={styles.draftNote}>
          {savedAt ? `草稿已保存 · ${savedAt}` : "可保存草稿，稍后继续编辑"}
        </span>
        */}
        <button
          className={styles.btn}
          onClick={() =>
            sceneRef.current?.scrollIntoView({ behavior: "smooth" })
          }
        >
          上一步
        </button>
        {/*
        <button className={styles.btn} onClick={() => void saveDraft()}>
          保存草稿
        </button>
        */}
        <button
          className={`${styles.btn} ${styles.primary}`}
          onClick={onPublish}
        >
          {editingId ? "保存修改" : "发布规划"}
        </button>
      </div>
    </>
  );
}
