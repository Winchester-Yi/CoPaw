import { Alert, Button, Empty, Skeleton } from "antd";
import { useEffect, useMemo } from "react";
import {
  Link,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import {
  tokenDiagnosisV2Api,
  type DiagnosisFilters,
  type DiagnosisPage,
  type EvaluationDTO,
  type FindingDTO,
  type IssueGroupDTO,
  type RuleCoverageDTO,
} from "../../../api/modules/tokenDiagnosisV2";
import { FilterBar } from "../TokenDiagnosisShared/FilterBar";
import { EvidenceState } from "../TokenDiagnosisShared/EvidenceState";
import {
  formatCount,
  parseDiagnosisFilters,
  reasonLabel,
  RULE_LABELS,
  stateLabel,
  updateDiagnosisSearch,
} from "../TokenDiagnosisShared/presentation";
import { useDiagnosisResource } from "../TokenDiagnosisShared/useDiagnosisResource";
import {
  EvaluationPanel,
  Pager,
  ResourceNotice,
} from "../TokenExecutions/panels";
import styles from "./index.module.less";

type IssueInstancesPage = DiagnosisPage<FindingDTO> & { group_id: string };
type FindingIdentity = FindingDTO & {
  group_id?: string;
  source_id?: string;
  tenant_id?: string;
};
type EvaluationWithExpiry = EvaluationDTO & { evidence_expired?: boolean };
const INSTANCE_CURSOR = "instances_cursor";
const SELECTION_KEYS = ["group_id", "finding_id", INSTANCE_CURSOR];
const EXECUTION_STATES = new Set(["candidate"]);

function resourceKey(kind: string, filters: DiagnosisFilters, id?: string) {
  return JSON.stringify([kind, id || "", filters]);
}
function metricLabel(group: IssueGroupDTO) {
  return `${formatCount(group.hit_count)} 次命中 / ${formatCount(
    group.distinct_task_count,
  )} 个 Task`;
}
function Coverage({ coverage }: { coverage: RuleCoverageDTO[] }) {
  if (coverage.length === 0) return null;
  return (
    <section className={styles.section} aria-label="规则覆盖状态">
      <h2>规则覆盖状态</h2>
      <p className={styles.caption}>
        覆盖状态是当前窗口的评估完整性，不计入问题命中；不足、不适用、待评估与失败保持独立。
      </p>
      <div className={styles.coverageRows}>
        {coverage.map((item) => (
          <div className={styles.coverageRow} key={item.rule_id}>
            <strong>{RULE_LABELS[item.rule_id]}</strong>
            <span>候选 {formatCount(item.candidate)}</span>
            <span>未命中 {formatCount(item.not_hit)}</span>
            <span>证据不足 {formatCount(item.insufficient_evidence)}</span>
            <span>不适用 {formatCount(item.not_applicable)}</span>
            <span>待评估 {formatCount(item.pending)}</span>
            <span>分析失败 {formatCount(item.failed)}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
function GroupCard({
  group,
  selected,
  onSelect,
}: {
  group: IssueGroupDTO;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <article className={`${styles.group} ${selected ? styles.selected : ""}`}>
      <Button
        className={styles.groupButton}
        aria-label={`查看问题组 ${RULE_LABELS[group.rule_id]}`}
        aria-pressed={selected}
        onClick={onSelect}
      >
        <span className={styles.groupTitle}>
          <strong>{RULE_LABELS[group.rule_id]}</strong>
          <code>{group.group_id}</code>
        </span>
        <span>{group.recommendation_code}</span>
      </Button>
      {group.reliable_grouping ? (
        <div className={styles.metrics}>
          <strong>{metricLabel(group)}</strong>
          <span>
            首次 {group.first_seen} · 最近 {group.latest_seen}
          </span>
        </div>
      ) : (
        <div className={styles.unreliable}>
          <strong>无法可靠聚合</strong>
          <span>
            {formatCount(group.hit_count)} 个未聚合实例 · 不推断共同根因
          </span>
          <span>
            首次 {group.first_seen} · 最近 {group.latest_seen}
          </span>
        </div>
      )}
    </article>
  );
}
function findingIdentityMatches(
  finding: FindingIdentity,
  page: IssueInstancesPage,
) {
  return (
    (!finding.source_id || finding.source_id === page.scope.source_id) &&
    (!finding.tenant_id || finding.tenant_id === page.scope.tenant_id) &&
    !!finding.task_run_id &&
    !!finding.branch_run_id &&
    !!finding.evaluation_id &&
    EXECUTION_STATES.has(finding.evaluation_state || "")
  );
}
function executionHref(
  finding: FindingIdentity,
  returnTo: string,
  page: IssueInstancesPage,
  search: URLSearchParams,
) {
  if (!findingIdentityMatches(finding, page)) return null;
  const params = new URLSearchParams();
  const filters = parseDiagnosisFilters(search);
  for (const key of [
    "start",
    "end",
    "agent_id",
    "root_agent_id",
    "model_name",
  ] as const) {
    const value = filters[key];
    if (value) params.set(key, value);
  }
  const origin = search.get("origin");
  if (origin) params.set("origin", origin);
  const hideMenu = search.get("hideMenu");
  if (hideMenu === "true" || hideMenu === "false")
    params.set("hideMenu", hideMenu);
  params.set("branch_run_id", finding.branch_run_id);
  if (finding.call_id) params.set("call_id", finding.call_id);
  if (finding.context_id) params.set("context_id", finding.context_id);
  params.set("evaluation_id", finding.evaluation_id);
  params.set("returnTo", returnTo);
  return `/analytics/token-executions/${encodeURIComponent(
    finding.task_run_id,
  )}?${params.toString()}`;
}
function findingMatchesGroup(
  finding: FindingIdentity,
  group: IssueGroupDTO,
  scope: IssueInstancesPage["scope"],
) {
  const reliableGrouping = finding.reliable_grouping && !!finding.signature;
  const expectedGroupId = reliableGrouping
    ? finding.signature
      ? `s:${finding.signature}`
      : ""
    : `f:${finding.finding_id}`;
  return (
    group.group_id === expectedGroupId &&
    (!finding.group_id || finding.group_id === group.group_id) &&
    finding.rule_id === group.rule_id &&
    finding.recommendation_code === group.recommendation_code &&
    reliableGrouping === group.reliable_grouping &&
    (!finding.source_id || finding.source_id === scope.source_id) &&
    (!finding.tenant_id || finding.tenant_id === scope.tenant_id)
  );
}
function evaluationExpired(data: EvaluationWithExpiry) {
  return (
    data.evidence_expired === true ||
    data.refs.some((reference) => reference.evidence_expired)
  );
}
function evaluationMatchesFinding(
  data: EvaluationWithExpiry,
  finding: FindingIdentity,
  scope: IssueInstancesPage["scope"],
  requestWindow: IssueInstancesPage["window"],
) {
  const objectMatches =
    data.evaluation.object_type === "call"
      ? !!finding.call_id && data.evaluation.object_id === finding.call_id
      : data.evaluation.object_type === "step"
      ? !!finding.step_id && data.evaluation.object_id === finding.step_id
      : data.evaluation.object_type === "branch" &&
        data.evaluation.object_id === finding.branch_run_id;
  return (
    data.scope.source_id === scope.source_id &&
    data.scope.tenant_id === scope.tenant_id &&
    data.window.start === requestWindow.start &&
    data.window.end === requestWindow.end &&
    data.evaluation.evaluation_id === finding.evaluation_id &&
    data.evaluation.task_run_id === finding.task_run_id &&
    data.evaluation.branch_run_id === finding.branch_run_id &&
    data.evaluation.rule_id === finding.rule_id &&
    objectMatches
  );
}
function FindingRow({
  finding,
  page,
  returnTo,
  search,
  selected,
  onSelect,
  executionAllowed = true,
  blockedMessage = "规则证据未就绪，拒绝跳转",
}: {
  finding: FindingDTO;
  page: IssueInstancesPage;
  returnTo: string;
  search: URLSearchParams;
  selected: boolean;
  onSelect: () => void;
  executionAllowed?: boolean;
  blockedMessage?: string;
}) {
  const href = executionAllowed
    ? executionHref(finding as FindingIdentity, returnTo, page, search)
    : null;
  return (
    <div className={`${styles.finding} ${selected ? styles.selected : ""}`}>
      <Button
        className={styles.findingButton}
        aria-pressed={selected}
        onClick={onSelect}
      >
        <strong>{finding.instance_key}</strong>
        <code>{finding.finding_id}</code>
      </Button>
      <span>
        Task {finding.task_run_id} · Branch {finding.branch_run_id} ·{" "}
        {stateLabel(finding.evaluation_state)}
      </span>
      <span>
        Call {finding.call_id || "未知"} · Context{" "}
        {finding.context_id || "未知"}
      </span>
      {!selected ? (
        <span>先选择实例查看规则证据</span>
      ) : href ? (
        <Link to={href}>打开执行详情</Link>
      ) : (
        <span className={styles.blocked} role="status">
          {blockedMessage}
        </span>
      )}
    </div>
  );
}
function EvaluationNotice({
  resource,
  data,
  matches,
}: {
  resource: { state: string; error?: string; reload: () => void };
  data?: EvaluationWithExpiry;
  matches: boolean;
}) {
  if (resource.state === "loading")
    return (
      <div role="status" aria-label="加载规则实例">
        <Skeleton active paragraph={{ rows: 2 }} />
      </div>
    );
  if (["error", "not_found", "unavailable"].includes(resource.state))
    return <ResourceNotice resource={resource} name="规则实例" />;
  if (data && !matches)
    return (
      <Alert
        type="warning"
        message="规则实例身份与当前问题实例不匹配；拒绝绑定外部或过期证据。"
      />
    );
  if (data && evaluationExpired(data))
    return (
      <Alert
        type="warning"
        message="规则实例证据已过期；仅保留只读历史详情，不计入当前问题统计。"
      />
    );
  if (data && !data.is_current)
    return (
      <Alert
        type="info"
        message="这是历史规则结果（已落后），可查看证据但不计入当前问题组统计。"
      />
    );
  return null;
}
export default function TokenIssuesPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const filters = useMemo(() => parseDiagnosisFilters(search), [search]);
  const groupId = search.get("group_id") || "";
  const findingId = search.get("finding_id") || "";
  const issues = useDiagnosisResource(
    resourceKey("issues", filters),
    (signal) => tokenDiagnosisV2Api.issues(signal, filters),
  );
  const window = issues.data?.window;
  const requestFilters = useMemo(
    () => ({
      ...filters,
      start: filters.start || window?.start,
      end: filters.end || window?.end,
    }),
    [filters, window],
  );
  const pinned =
    !!window && filters.start === window.start && filters.end === window.end;
  useEffect(() => {
    if (!window || pinned) return;
    const next = new URLSearchParams(search);
    next.set("start", window.start);
    next.set("end", window.end);
    navigate(
      {
        pathname: location.pathname,
        search: `?${next.toString()}`,
        hash: location.hash,
      },
      { replace: true },
    );
  }, [window, pinned, search, navigate, location.pathname, location.hash]);
  const coverageFilters = { ...requestFilters, cursor: undefined };
  const coverage = useDiagnosisResource(
    resourceKey("coverage", coverageFilters),
    (signal) => tokenDiagnosisV2Api.dashboard(signal, coverageFilters),
    pinned,
  );
  const instancesFilters = {
    ...requestFilters,
    cursor: search.get(INSTANCE_CURSOR) || undefined,
  };
  const groups = issues.data?.items || [];
  const selectedGroup = groups.find((group) => group.group_id === groupId);
  const instances = useDiagnosisResource<IssueInstancesPage>(
    resourceKey("instances", instancesFilters, groupId),
    (signal) =>
      tokenDiagnosisV2Api.instances(groupId, signal, instancesFilters),
    pinned && !!selectedGroup,
  );
  const rawInstancePage =
    instances.data && instances.data.group_id === groupId
      ? instances.data
      : undefined;
  const instanceScopeMatches =
    !!rawInstancePage &&
    (!issues.data ||
      (rawInstancePage.scope.source_id === issues.data.scope.source_id &&
        rawInstancePage.scope.tenant_id === issues.data.scope.tenant_id &&
        rawInstancePage.window.start === requestFilters.start &&
        rawInstancePage.window.end === requestFilters.end));
  const validItems =
    rawInstancePage && selectedGroup && instanceScopeMatches
      ? rawInstancePage.items.filter((item) =>
          findingMatchesGroup(
            item as FindingIdentity,
            selectedGroup,
            rawInstancePage.scope,
          ),
        )
      : [];
  const instancePage =
    rawInstancePage && instanceScopeMatches
      ? { ...rawInstancePage, items: validItems }
      : undefined;
  const instanceHasMismatches =
    !!rawInstancePage &&
    (!instanceScopeMatches ||
      validItems.length !== rawInstancePage.items.length);
  const finding = instancePage?.items.find(
    (item) => item.finding_id === findingId,
  );
  const evaluationId = finding?.evaluation_id || "";
  const evaluation = useDiagnosisResource(
    resourceKey("evaluation", coverageFilters, evaluationId),
    (signal) =>
      tokenDiagnosisV2Api.evaluation(evaluationId, signal, coverageFilters),
    pinned && !!evaluationId && !!finding,
  );
  const evaluationData = evaluation.data as EvaluationWithExpiry | undefined;
  const evaluationMatches =
    !!finding &&
    !!instancePage &&
    !!evaluationData &&
    evaluationMatchesFinding(
      evaluationData,
      finding as FindingIdentity,
      instancePage.scope,
      requestFilters as IssueInstancesPage["window"],
    );
  const evaluationAllowsExecution =
    evaluation.state === "ready" &&
    !!evaluationData &&
    evaluationMatches &&
    evaluationData.is_current &&
    !evaluationExpired(evaluationData) &&
    EXECUTION_STATES.has(evaluationData.evaluation.state);
  const evaluationBlockedMessage =
    evaluation.state === "loading" || evaluation.state === "idle"
      ? "正在核验规则证据"
      : evaluation.state === "error" ||
        evaluation.state === "not_found" ||
        evaluation.state === "unavailable"
      ? "规则证据加载失败，拒绝跳转"
      : evaluationData && !evaluationMatches
      ? "规则实例身份不匹配，拒绝跳转"
      : evaluationData &&
        (!evaluationData.is_current || evaluationExpired(evaluationData))
      ? "规则证据已过期或为历史结果，拒绝跳转"
      : "规则证据未就绪，拒绝跳转";
  const returnTo = `${location.pathname}${location.search}${location.hash}`;
  function updateSearch(next: URLSearchParams) {
    navigate({
      pathname: location.pathname,
      search: next.toString() ? `?${next.toString()}` : "",
      hash: location.hash,
    });
  }
  function selectGroup(id: string) {
    const next = new URLSearchParams(search);
    next.set("group_id", id);
    next.delete("finding_id");
    next.delete(INSTANCE_CURSOR);
    updateSearch(next);
  }
  function selectFinding(id: string) {
    const next = new URLSearchParams(search);
    next.set("finding_id", id);
    updateSearch(next);
  }
  function applyFilters(nextFilters: DiagnosisFilters) {
    const next = updateDiagnosisSearch(search, nextFilters);
    for (const key of SELECTION_KEYS) next.delete(key);
    updateSearch(next);
  }
  function page(name: string, key: string, nextCursor?: string | null) {
    return (
      <Pager
        name={name}
        cursor={search.get(key) || undefined}
        next={nextCursor}
        pinned={pinned}
        onChange={(cursor) => {
          const next = new URLSearchParams(search);
          if (cursor) next.set(key, cursor);
          else next.delete(key);
          if (key === "cursor") {
            next.delete("group_id");
            next.delete("finding_id");
            next.delete(INSTANCE_CURSOR);
          }
          updateSearch(next);
        }}
      />
    );
  }
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <p className={styles.eyebrow}>Analytics / Token diagnostics</p>
        <h1>Token 问题</h1>
        <p className={styles.caption}>
          当前候选问题按可靠身份分组；估算量保留原始重叠，不相加为节省。
        </p>
      </header>
      <FilterBar
        filters={filters}
        window={window}
        showRule
        showState
        onChange={applyFilters}
      />
      <ResourceNotice resource={issues} name="问题组" />
      {issues.data && (
        <p className={styles.meta}>
          作用域：{issues.data.scope.source_id} / {issues.data.scope.tenant_id}{" "}
          · 窗口：{issues.data.window.start} → {issues.data.window.end} · as of{" "}
          {issues.data.as_of}
        </p>
      )}
      <ResourceNotice resource={coverage} name="规则覆盖状态" />
      {coverage.state === "ready" && (
        <Coverage coverage={coverage.data?.rule_coverage || []} />
      )}
      <section className={styles.section} aria-label="问题组列表">
        <div className={styles.sectionHeader}>
          <h2>当前候选问题组</h2>
          {page("问题组", "cursor", issues.data?.next_cursor)}
        </div>
        {groups.length === 0 && issues.state === "ready" ? (
          <Empty
            description="当前筛选没有候选问题"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        ) : (
          <div className={styles.groups}>
            {groups.map((group) => (
              <GroupCard
                key={group.group_id}
                group={group}
                selected={group.group_id === groupId}
                onSelect={() => selectGroup(group.group_id)}
              />
            ))}
          </div>
        )}
        {issues.state === "ready" && groupId && !selectedGroup && (
          <Alert
            type="info"
            message="当前页未找到指定问题组；请返回问题组列表或调整筛选。"
          />
        )}
      </section>
      {groupId && (
        <section className={styles.section} aria-label="问题实例列表">
          <div className={styles.sectionHeader}>
            <div>
              <h2>问题实例</h2>
              <p className={styles.caption}>
                仅显示当前问题组真实返回的实例；列表受分页与证据保留限制。
              </p>
            </div>
            {page("问题实例", INSTANCE_CURSOR, instancePage?.next_cursor)}
          </div>
          <ResourceNotice resource={instances} name="问题实例" />
          {instances.data && (!instancePage || instanceHasMismatches) && (
            <Alert
              type="warning"
              message="问题实例身份不匹配；拒绝绑定返回数据。"
            />
          )}
          {instances.state === "ready" &&
            instancePage &&
            findingId &&
            !finding && (
              <Alert
                type="info"
                message="当前页未找到指定问题实例；请返回实例列表或调整分页。"
              />
            )}
          {instancePage?.items.length === 0 && (
            <Empty
              description="当前页无问题实例"
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          )}
          <div className={styles.findings}>
            {instancePage?.items.map((item) => (
              <FindingRow
                key={item.finding_id}
                finding={item}
                page={instancePage}
                returnTo={returnTo}
                search={search}
                selected={item.finding_id === findingId}
                executionAllowed={
                  pinned &&
                  item.finding_id === findingId &&
                  evaluationAllowsExecution
                }
                blockedMessage={evaluationBlockedMessage}
                onSelect={() => selectFinding(item.finding_id)}
              />
            ))}
          </div>
          {finding && (
            <>
              <EvaluationNotice
                resource={evaluation}
                data={evaluation.data}
                matches={evaluationMatches}
              />
              {evaluation.data && evaluationMatches && (
                <EvaluationPanel data={evaluation.data} />
              )}
              {!evaluationId && <p>当前实例没有可定位的规则评估。</p>}
              {evaluation.data &&
                evaluationMatches &&
                evaluation.data.evaluation.state !== "candidate" && (
                  <EvidenceState state={evaluation.data?.evaluation.state} />
                )}
              {evaluation.data &&
                evaluationMatches &&
                evaluation.data.evaluation.state ===
                  "insufficient_evidence" && (
                  <p>证据不足：此结果不计入问题命中。</p>
                )}
              {evaluation.data &&
                evaluationMatches &&
                evaluation.data.evaluation.state === "not_applicable" && (
                  <p>不适用：此结果不计入问题命中。</p>
                )}
              {evaluation.data &&
                evaluationMatches &&
                evaluation.data.evaluation.state === "candidate" &&
                evaluation.data.gaps.map((gap) => (
                  <p key={gap.reason_code}>
                    缺口：{reasonLabel(gap.reason_code)}
                  </p>
                ))}
            </>
          )}
        </section>
      )}
    </div>
  );
}
