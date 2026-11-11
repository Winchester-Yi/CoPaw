import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type RefObject,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import cx from "classnames";
import { useWealthStore } from "../store";
import type { DistributeTarget } from "../types";
import {
  distributionContext,
  queryInstitutionPeople,
  type Institution,
} from "../distribution/api";
import { useInstitutionCache } from "../distribution/institutionCache";
import {
  institutionIdentity,
  loadInstitutions,
  useInstitutionIdentity,
} from "../distribution/useInstitutionPreload";
import {
  DistributionSession,
  type DistributionSnapshot,
} from "../distribution/session";
import styles from "./MiddleTargetPicker.module.less";

export function MiddleTargetPicker() {
  const key = useInstitutionIdentity();
  const tree = useInstitutionCache();
  const version = useWealthStore((s) => s.planFormVersion);
  useEffect(() => {
    if (tree.key !== key || tree.status === "idle") void loadInstitutions(key);
  }, [key, tree.key, tree.status]);
  if (tree.key !== key || tree.status === "idle" || tree.status === "loading")
    return (
      <div className={styles.treeLoading} role="status">
        正在加载分行机构…
      </div>
    );
  if (tree.status === "error" || !tree.root)
    return (
      <div className={styles.treeLoading} role="alert">
        <span>{tree.error || "机构查询失败"}</span>
        <button type="button" onClick={() => void loadInstitutions(key)}>
          重试
        </button>
      </div>
    );
  return <InstitutionPicker key={`${key}-${version}`} root={tree.root} />;
}

function InstitutionPicker({ root }: { root: Institution }) {
  const [session] = useState(() => {
    const state = useWealthStore.getState();
    const version = state.planFormVersion;
    const contextVersion = state.contextVersion;
    const identity = institutionIdentity();
    const context = distributionContext();
    const initial = state.targetSapIds.map(
      (sapId) =>
        state.targets.find((u) => u.sapId === sapId) ?? {
          sapId,
          name: sapId,
          orgName: "",
        },
    );
    return new DistributionSession(
      root,
      initial,
      (nodes, signal) => queryInstitutionPeople(context, nodes, signal),
      (snapshot) => {
        const current = useWealthStore.getState();
        if (
          current.planFormVersion !== version ||
          current.contextVersion !== contextVersion ||
          identity !== institutionIdentity()
        )
          return;
        useWealthStore.setState({
          targets: snapshot.selected,
          targetSapIds: snapshot.selected.map((u) => u.sapId),
          distributionBlocked: snapshot.blocked,
          targetsLoaded: snapshot.people !== undefined,
        });
      },
    );
  });
  useEffect(() => () => session.dispose(), [session]);
  const snapshot = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const summaryRef = useRef<HTMLDivElement>(null);
  const summaryColumns = useCardColumns(summaryRef, 210);
  const [orgKeyword, setOrgKeyword] = useState("");
  const [keyword, setKeyword] = useState("");
  const [expanded, setExpanded] = useState(() => new Set([root.mOrgCode]));
  const selectedIds = useMemo(
    () => new Set(snapshot.selected.map((u) => u.sapId)),
    [snapshot.selected],
  );
  const selectView = (code: string) => {
    setKeyword("");
    session.view(code);
  };
  const rows = useMemo(() => {
    const matches = new Set<string>();
    const kw = orgKeyword.trim().toLowerCase();
    if (kw)
      session.index.nodes.forEach((node, code) => {
        if (`${node.mOrgName} ${code}`.toLowerCase().includes(kw))
          session.index
            .path(code)
            .forEach((ancestor) => matches.add(ancestor.mOrgCode));
      });
    const result: { node: Institution; depth: number }[] = [];
    const visit = (node: Institution, depth: number) => {
      if (kw && !matches.has(node.mOrgCode)) return;
      result.push({ node, depth });
      if (kw || expanded.has(node.mOrgCode))
        node.children.forEach((child) => visit(child, depth + 1));
    };
    visit(root, 0);
    return result;
  }, [root, session, orgKeyword, expanded]);
  const current = snapshot.current
    ? session.index.nodes.get(snapshot.current)
    : undefined;
  const status = snapshot.current
    ? snapshot.statuses.get(snapshot.current)
    : undefined;
  const visible = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return (snapshot.people ?? []).filter((u) =>
      `${u.name} ${u.sapId} ${u.orgName}`.toLowerCase().includes(kw),
    );
  }, [keyword, snapshot.people]);
  const full = Boolean(
    current && session.isInstitutionSelected(current.mOrgCode),
  );
  const groups = useMemo(
    () => groupSelection(session, snapshot),
    [session, snapshot],
  );
  const errors = [...snapshot.statuses].filter(
    ([, state]) => state === "error",
  );
  return (
    <div className={styles.picker}>
      <p className={styles.intro}>
        勾选机构可选择并查看本级及所有下级人员；点击机构名称，可单独调整人员。
      </p>
      <div className={styles.workspace}>
        <aside className={styles.orgPane} aria-label="分发机构">
          <input
            className={styles.search}
            aria-label="搜索机构"
            placeholder="搜索机构名称 / 编码"
            value={orgKeyword}
            onChange={(e) => setOrgKeyword(e.target.value)}
          />
          <div className={styles.orgList} role="group" aria-label="机构列表">
            {rows.length ? (
              rows.map(({ node, depth }) => {
                const code = node.mOrgCode;
                const count = snapshot.selected.filter((u) =>
                  session.index.includes(code, u),
                ).length;
                const checked = session.isInstitutionSelected(code);
                const mixed =
                  !checked &&
                  (count > 0 ||
                    session.isPendingSelection(code) ||
                    snapshot.emptyInstitutions.some((empty) =>
                      session.index.contains(code, empty),
                    ));
                const state = snapshot.statuses.get(code);
                return (
                  <div
                    key={code}
                    className={cx(
                      styles.orgRow,
                      snapshot.current === code && styles.current,
                      depth === 0 && styles.rootRow,
                    )}
                    style={{ paddingLeft: 8 + Math.min(depth, 5) * 14 }}
                  >
                    <input
                      type="checkbox"
                      aria-label={`选择${node.mOrgName}全部人员`}
                      aria-checked={mixed ? "mixed" : checked}
                      ref={(input) => {
                        if (input) input.indeterminate = mixed;
                      }}
                      checked={checked}
                      onChange={() => {
                        session.toggleInstitution(code);
                        if (session.getSnapshot().current !== snapshot.current)
                          setKeyword("");
                      }}
                    />
                    <button
                      type="button"
                      className={styles.orgName}
                      title={`${node.mOrgName}（${code}）`}
                      aria-current={
                        snapshot.current === code ? "true" : undefined
                      }
                      onClick={() => selectView(code)}
                    >
                      {node.mOrgName}
                    </button>
                    {state && (
                      <span
                        className={cx(
                          styles.requestStatus,
                          state === "error" && styles.errorText,
                        )}
                      >
                        {statusLabel(state)}
                      </span>
                    )}
                    {node.children.length > 0 && (
                      <button
                        type="button"
                        className={styles.expand}
                        aria-label={`${expanded.has(code) ? "收起" : "展开"}${
                          node.mOrgName
                        }`}
                        aria-expanded={expanded.has(code)}
                        onClick={() =>
                          setExpanded((old) => {
                            const next = new Set(old);
                            if (next.has(code)) next.delete(code);
                            else next.add(code);
                            return next;
                          })
                        }
                      >
                        {expanded.has(code) ? "⌄" : "›"}
                      </button>
                    )}
                  </div>
                );
              })
            ) : (
              <p className={styles.empty}>未找到匹配机构</p>
            )}
          </div>
        </aside>
        <div className={styles.peoplePane}>
          <div className={styles.peopleHead}>
            <div>
              <div className={styles.path}>
                {current
                  ? session.index
                      .path(current.mOrgCode)
                      .map((node) => node.mOrgName)
                      .join(" / ")
                  : root.mOrgName}
              </div>
              <div className={styles.peopleTitle}>
                <h4>{current ? `${current.mOrgName}人员` : "请选择机构"}</h4>
                <p>
                  {snapshot.people ? (
                    <>
                      共 {snapshot.people.length} 人
                      {current?.children.length ? "（含下级机构）" : ""} ·{" "}
                      <strong>
                        已选{" "}
                        {
                          snapshot.people.filter((u) =>
                            selectedIds.has(u.sapId),
                          ).length
                        }{" "}
                        人
                      </strong>
                    </>
                  ) : (
                    "点击左侧机构名称，查看并选择人员"
                  )}
                </p>
              </div>
            </div>
            {snapshot.people && (
              <button
                type="button"
                className={styles.outlineButton}
                onClick={() => session.toggleInstitution(current!.mOrgCode)}
              >
                {full
                  ? "取消本机构全选"
                  : `全选本机构 ${snapshot.people.length} 人`}
              </button>
            )}
          </div>
          <div className={styles.tools}>
            <input
              className={styles.search}
              aria-label="搜索分发目标"
              placeholder="搜索当前机构姓名 / 用户 ID / 机构"
              value={keyword}
              disabled={!snapshot.people}
              onChange={(e) => setKeyword(e.target.value)}
            />
            {keyword.trim() && (
              <button
                type="button"
                className={styles.link}
                disabled={!visible.length}
                onClick={() => session.selectPeople(visible)}
              >
                {visible.length &&
                visible.every((u) => selectedIds.has(u.sapId))
                  ? "取消选择搜索结果"
                  : "选择搜索结果"}
              </button>
            )}
          </div>
          {snapshot.people ? (
            visible.length ? (
              <PersonnelCards
                key={snapshot.current}
                users={visible}
                selected={selectedIds}
                onToggle={(user) => session.togglePerson(user)}
              />
            ) : (
              <div className={styles.emptyPanel}>
                {keyword ? "未找到匹配人员" : "该机构暂无可分发人员"}
              </div>
            )
          ) : (
            <div
              className={styles.emptyPanel}
              role={status === "error" ? "alert" : "status"}
            >
              {status === "error" ? (
                <>
                  <span>{snapshot.errors.get(snapshot.current!)}</span>
                  <button
                    type="button"
                    onClick={() => session.retry(snapshot.current!)}
                  >
                    重试人员查询
                  </button>
                </>
              ) : status ? (
                <>
                  <span className={styles.spinner} />
                  <strong>{statusLabel(status)}…</strong>
                </>
              ) : (
                <>
                  <span className={styles.emptyIcon}>▦</span>
                  <strong>先选择机构，再选择人员</strong>
                  <span>也可以勾选机构，直接选择该机构全部人员</span>
                </>
              )}
            </div>
          )}
          <p className={styles.note}>
            切换查看机构不会改变勾选结果；取消个别人员后，该机构自动变为“部分选择”。
          </p>
        </div>
      </div>
      {errors.length > 0 && (
        <div className={styles.failures} role="alert">
          {errors.map(([code]) => (
            <div key={code}>
              <span>
                {session.index.nodes.get(code)?.mOrgName}：
                {snapshot.errors.get(code)}
              </span>
              <button
                type="button"
                className={styles.link}
                onClick={() => session.retry(code)}
              >
                重试
              </button>
              {session.isPendingSelection(code) && (
                <button
                  type="button"
                  className={styles.link}
                  onClick={() => session.cancelPending(code)}
                >
                  取消待选操作
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      <div className={styles.summary}>
        <div className={styles.summaryHead}>
          <span>
            已选 <strong>{groups.length}</strong> 个机构 ·{" "}
            <strong>{snapshot.selected.length}</strong> 人
          </span>
          <button
            type="button"
            className={styles.link}
            onClick={() => session.clear()}
          >
            清空全部
          </button>
        </div>
        <div
          ref={summaryRef}
          className={styles.summaries}
          style={{
            gridTemplateColumns: `repeat(${summaryColumns}, minmax(0, 1fr))`,
          }}
        >
          {groups.map((group) => (
            <div key={group.code} className={styles.summaryCard}>
              <h4>
                <span title={group.name}>{group.name}</span>
                <span
                  className={cx(
                    styles.badge,
                    group.complete && styles.complete,
                  )}
                >
                  {group.complete
                    ? `全部 ${group.users.length} 人`
                    : group.users.length === group.total
                    ? `部分选择 · ${group.users.length} 人`
                    : group.total !== undefined
                    ? `部分 ${group.users.length} / ${group.total} 人`
                    : `已选 ${group.users.length} 人`}
                </span>
              </h4>
              <p title={group.users.map((u) => u.name).join("、")}>
                {group.complete
                  ? "已选择该机构全部可分发人员"
                  : group.users.map((u) => u.name).join("、")}
              </p>
              {group.code === "unmatched" && (
                <div className={styles.unknownPeople}>
                  {group.users.map((user) => (
                    <button
                      key={user.sapId}
                      type="button"
                      aria-label={`移除${user.name}`}
                      onClick={() => session.togglePerson(user)}
                    >
                      {user.name} ×
                    </button>
                  ))}
                </div>
              )}
              <div className={styles.summaryActions}>
                {group.code !== "unmatched" && (
                  <button
                    type="button"
                    className={styles.link}
                    onClick={() => selectView(group.code)}
                  >
                    {group.complete ? "查看人员" : "调整人员"}
                  </button>
                )}
                <button
                  type="button"
                  className={styles.link}
                  onClick={() =>
                    group.code === "unmatched"
                      ? session.removeUnknown()
                      : session.removeInstitution(group.code)
                  }
                >
                  移除该机构
                </button>
              </div>
            </div>
          ))}
          {!groups.length && (
            <p className={styles.empty}>
              从左侧勾选整个机构，或点击机构名称挑选人员
            </p>
          )}
        </div>
        {snapshot.blocked && (
          <p className={styles.note} role="status">
            请等待人员查询完成；如有全选失败的机构，请重试或取消待选操作后再发布。
          </p>
        )}
      </div>
    </div>
  );
}
function statusLabel(status: string) {
  if (status === "waiting") return "等待加载";
  if (status === "loading") return "加载中";
  return "加载失败";
}
function groupSelection(
  session: DistributionSession,
  snapshot: DistributionSnapshot,
) {
  const anchors = snapshot.anchors.filter(
    (code) =>
      snapshot.emptyInstitutions.includes(code) ||
      snapshot.selected.some((u) => session.index.includes(code, u)),
  );
  const groups = new Map<string, DistributeTarget[]>();
  for (const user of snapshot.selected) {
    const candidates = anchors.filter((code) =>
      session.index.includes(code, user),
    );
    const anchor = candidates.find(
      (code) =>
        !candidates.some(
          (other) => other !== code && session.index.contains(other, code),
        ),
    );
    const code = session.index.nodes.has(user.bbkOrgId ?? "")
      ? anchor ?? session.index.branchOf(user) ?? "unmatched"
      : "unmatched";
    const list = groups.get(code) ?? [];
    list.push(user);
    groups.set(code, list);
  }
  for (const empty of snapshot.emptyInstitutions) {
    const candidates = anchors.filter((code) =>
      session.index.contains(code, empty),
    );
    const code =
      candidates.find(
        (candidate) =>
          !candidates.some(
            (other) =>
              other !== candidate && session.index.contains(other, candidate),
          ),
      ) ?? empty;
    if (!groups.has(code)) groups.set(code, []);
  }
  return [...groups].map(([code, users]) => {
    const people = session.people(code);
    return {
      code,
      name: session.index.nodes.get(code)?.mOrgName ?? "未匹配机构",
      users,
      total: people?.length,
      complete: session.isInstitutionSelected(code),
    };
  });
}
function PersonnelCards({
  users,
  selected,
  onToggle,
}: {
  users: DistributeTarget[];
  selected: Set<string>;
  onToggle: (user: DistributeTarget) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const columns = useCardColumns(ref, 190);
  const [focusedIndex, setFocusedIndex] = useState(0);
  const pendingFocus = useRef<number | null>(null);
  const virtual = useVirtualizer({
    count: Math.ceil(users.length / columns),
    getScrollElement: () => ref.current,
    estimateSize: () => 72,
    overscan: 3,
    initialRect: { width: 950, height: 288 },
  });
  const rows = virtual.getVirtualItems();
  const focusedRowRendered =
    focusedIndex < users.length &&
    rows.some((row) => row.index === Math.floor(focusedIndex / columns));
  const firstVisibleRow =
    rows.find((row) => row.end > (virtual.scrollOffset ?? 0)) ?? rows[0];
  // Keep a Tab entry when scrolling unmounts the last focused card, without moving focus.
  const tabStopIndex = focusedRowRendered
    ? focusedIndex
    : (firstVisibleRow?.index ?? 0) * columns;
  useEffect(() => {
    pendingFocus.current = null;
    if (ref.current) ref.current.scrollTop = 0;
    setFocusedIndex(0);
  }, [users]);
  useEffect(() => {
    if (pendingFocus.current === null) return;
    const target = ref.current?.querySelector<HTMLButtonElement>(
      `[data-person-index="${pendingFocus.current}"]`,
    );
    if (target) {
      pendingFocus.current = null;
      target.focus();
    }
  });
  return (
    <div
      ref={ref}
      className={styles.personScroll}
      role="group"
      aria-label="分发目标列表"
      onBlur={(event) => {
        if (
          event.relatedTarget &&
          !event.currentTarget.contains(event.relatedTarget as Node)
        )
          pendingFocus.current = null;
      }}
    >
      <div
        className={styles.virtualSpace}
        style={{ height: virtual.getTotalSize() }}
      >
        {rows.map((row) => (
          <div
            key={row.key}
            className={styles.personRow}
            style={{
              transform: `translateY(${row.start}px)`,
              gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
            }}
          >
            {users
              .slice(row.index * columns, (row.index + 1) * columns)
              .map((user, offset) => (
                <button
                  key={user.sapId}
                  type="button"
                  className={cx(
                    styles.person,
                    selected.has(user.sapId) && styles.selected,
                  )}
                  data-person-index={row.index * columns + offset}
                  tabIndex={
                    tabStopIndex === row.index * columns + offset ? 0 : -1
                  }
                  onFocus={() => setFocusedIndex(row.index * columns + offset)}
                  onKeyDown={(event) => {
                    const offsets: Record<string, number> = {
                      ArrowRight: 1,
                      ArrowLeft: -1,
                      ArrowDown: columns,
                      ArrowUp: -columns,
                    };
                    const delta = offsets[event.key];
                    if (
                      delta === undefined &&
                      event.key !== "Home" &&
                      event.key !== "End"
                    )
                      return;
                    event.preventDefault();
                    const position = row.index * columns + offset;
                    const next =
                      event.key === "Home"
                        ? 0
                        : event.key === "End"
                        ? users.length - 1
                        : Math.max(
                            0,
                            Math.min(users.length - 1, position + delta),
                          );
                    if (next === position) {
                      pendingFocus.current = null;
                      return;
                    }
                    pendingFocus.current = next;
                    virtual.scrollToIndex(Math.floor(next / columns), {
                      align: "auto",
                    });
                    setFocusedIndex(next);
                  }}
                  aria-pressed={selected.has(user.sapId)}
                  aria-label={`${user.name} ${user.sapId}`}
                  title={`${user.name} · ${user.sapId} · ${user.orgName}`}
                  onClick={() => onToggle(user)}
                >
                  <span className={styles.check} aria-hidden="true">
                    {selected.has(user.sapId) ? "✓" : ""}
                  </span>
                  <span className={styles.avatar} aria-hidden="true">
                    {user.name.slice(0, 1)}
                  </span>
                  <span className={styles.personInfo}>
                    <strong>{user.name}</strong>
                    <small>
                      {user.sapId} · {user.orgName || "未提供机构名称"}
                    </small>
                  </span>
                </button>
              ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function useCardColumns(ref: RefObject<HTMLElement>, minimumWidth: number) {
  const [columns, setColumns] = useState(5);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const update = () =>
      setColumns(
        Math.max(
          1,
          Math.min(5, Math.floor((element.clientWidth + 8) / minimumWidth)),
        ),
      );
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, minimumWidth]);
  return columns;
}
