import { useEffect, useMemo, useRef, useState } from "react";
import {
  DownloadOutlined,
  FileExcelOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import cx from "classnames";
import { BBK_ID_TO_NAME_MAP } from "../../../constants/bbk";
import { importExcelPeople, MAX_EXCEL_FILE_MB } from "../distribution/api";
import type { DistributeTarget } from "../types";
import styles from "./ExcelTargetPicker.module.less";
import cardStyles from "./MiddleTargetPicker.module.less";
import workbenchStyles from "../index.module.less";
import { Icon } from "./Icon";

type UploadMode = "append" | "replace";

interface Props {
  currentTargets?: DistributeTarget[];
  onPendingChange?: (pending: boolean) => void;
  onChange: (targets: DistributeTarget[]) => void;
}

export function ExcelTargetPicker({
  currentTargets,
  onPendingChange,
  onChange,
}: Props) {
  const editing = currentTargets !== undefined;
  const uploadIntent = useRef<UploadMode>("replace");
  const input = useRef<HTMLInputElement>(null);
  const primaryUpload = useRef<HTMLButtonElement>(null);
  const request = useRef<AbortController>();
  const [file, setFile] = useState<File>();
  const [mode, setMode] = useState<UploadMode>("replace");
  const [alreadySelected, setAlreadySelected] = useState(0);
  const [stage, setStage] = useState("");
  const [error, setError] = useState("");
  const [issues, setIssues] = useState<string[]>([]);
  const [duplicates, setDuplicates] = useState(0);
  const [people, setPeople] = useState<DistributeTarget[]>([]);
  useEffect(() => () => request.current?.abort(), []);

  function cancelUpload() {
    request.current?.abort();
    setFile(undefined);
    setStage("");
    setError("");
    setIssues([]);
    setPeople([]);
    setDuplicates(0);
    setAlreadySelected(0);
    onPendingChange?.(false);
    primaryUpload.current?.focus();
  }

  function chooseFile(intent: UploadMode) {
    uploadIntent.current = intent;
    input.current?.click();
  }

  async function load(next: File, intent: UploadMode) {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setFile(next);
    setMode(intent);
    setAlreadySelected(0);
    setStage("正在上传并解析名单…");
    setError("");
    setIssues([]);
    setDuplicates(0);
    setPeople([]);
    if (editing) onPendingChange?.(true);
    else onChange([]);
    try {
      const roster = await importExcelPeople(next, controller.signal);
      if (controller.signal.aborted) return;
      setDuplicates(roster.duplicates);
      setIssues(roster.errors);
      if (roster.errors.length) return;
      const currentIds = new Set(currentTargets?.map(({ sapId }) => sapId));
      const matched =
        intent === "append"
          ? roster.people.filter(({ sapId }) => !currentIds.has(sapId))
          : roster.people;
      setAlreadySelected(roster.people.length - matched.length);
      setPeople(matched);
      if (!editing) onChange(matched);
    } catch (reason) {
      if (!controller.signal.aborted)
        setError(reason instanceof Error ? reason.message : "名单读取失败");
    } finally {
      if (!controller.signal.aborted) setStage("");
    }
  }

  const ready = Boolean(file && !stage && !error && !issues.length);
  const nextTargets =
    mode === "append" ? [...(currentTargets ?? []), ...people] : people;
  const existingIds = new Set(currentTargets?.map(({ sapId }) => sapId));
  const nextIds = new Set(nextTargets.map(({ sapId }) => sapId));
  const additions = nextTargets.filter(
    ({ sapId }) => !existingIds.has(sapId),
  ).length;
  const removals =
    currentTargets?.filter(({ sapId }) => !nextIds.has(sapId)).length ?? 0;

  return (
    <div className={styles.panel}>
      <div className={styles.uploadRow}>
        <span className={styles.fileIcon}>
          <FileExcelOutlined aria-hidden="true" />
        </span>
        <div className={styles.fileInfo}>
          <strong>
            {editing ? "当前分发名单" : file?.name ?? "导入本次分发的完整名单"}
          </strong>
          <p>
            {editing
              ? "追加时仅填写要新增的人员，替换时填写最终的完整名单。"
              : "按模板填写文本格式的客户经理ID，姓名选填。每行一人。"}
            文件不超过 {MAX_EXCEL_FILE_MB} MB。
          </p>
        </div>
        <div className={styles.actions}>
          <a
            className={workbenchStyles.btn}
            href={`${
              import.meta.env.BASE_URL
            }templates/wealth-excel-roster.xlsx`}
            download="分发名单模板.xlsx"
          >
            <DownloadOutlined aria-hidden="true" /> 下载模板
          </a>
          {editing && (
            <button
              type="button"
              className={workbenchStyles.btn}
              onClick={() => chooseFile("replace")}
            >
              上传 Excel 替换名单
            </button>
          )}
          <button
            type="button"
            className={cx(workbenchStyles.btn, workbenchStyles.primary)}
            ref={primaryUpload}
            onClick={() => chooseFile(editing ? "append" : "replace")}
          >
            <UploadOutlined aria-hidden="true" />
            {editing ? "上传 Excel 追加人员" : file ? "更换文件" : "上传 Excel"}
          </button>
        </div>
        <input
          ref={input}
          type="file"
          hidden
          accept=".xlsx"
          aria-label="选择 Excel 名单文件"
          onChange={(event) => {
            const next = event.currentTarget.files?.[0];
            event.currentTarget.value = "";
            if (next) void load(next, uploadIntent.current);
          }}
        />
      </div>
      {stage && (
        <p className={styles.feedback} role="status">
          {stage}
        </p>
      )}
      {error && (
        <div className={styles.feedback} role="alert">
          {error}{" "}
          {file && (
            <button type="button" onClick={() => void load(file, mode)}>
              重试
            </button>
          )}
        </div>
      )}
      {issues.length > 0 && (
        <details className={styles.feedback} open>
          <summary>
            有 {issues.length} 行待修正，请修改 Excel 后重新上传
          </summary>
          <ul
            className={styles.scrollList}
            tabIndex={0}
            aria-label="异常行列表"
          >
            {issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </details>
      )}
      {editing && file && (
        <section className={styles.importReview} aria-label="Excel 导入预览">
          <strong>
            {mode === "append" ? "追加预览" : "替换预览"} · {file.name}
          </strong>
          {ready && (
            <p role="status">
              新增 {additions} 人
              {mode === "replace" && ` · 移除 ${removals} 人`}
              {alreadySelected > 0 && ` · ${alreadySelected} 人已在名单中`}
              {duplicates > 0 && ` · 已去除 ${duplicates} 条重复记录`}
              {" · "}确认后共 {nextTargets.length} 人
            </p>
          )}
          {ready && people.length > 0 && (
            <details>
              <summary>
                {mode === "append" ? "查看新增人员" : "查看替换名单"}
              </summary>
              <ExcelRosterSummary
                people={people}
                countLabel={mode === "append" ? "待追加" : "替换名单共"}
              />
            </details>
          )}
          <div className={styles.actions}>
            <button
              type="button"
              className={workbenchStyles.btn}
              onClick={cancelUpload}
            >
              取消导入
            </button>
            {ready && (mode === "replace" || additions > 0) && (
              <button
                type="button"
                className={cx(workbenchStyles.btn, workbenchStyles.primary)}
                onClick={() => {
                  onChange(nextTargets);
                  cancelUpload();
                }}
              >
                {mode === "append" ? "确认追加" : "确认替换"}
              </button>
            )}
          </div>
        </section>
      )}
      {editing && (
        <ExcelRosterSummary
          people={currentTargets}
          onRemove={
            file
              ? undefined
              : (sapId) =>
                  onChange(
                    currentTargets.filter((person) => person.sapId !== sapId),
                  )
          }
        />
      )}
      {!editing && ready && (
        <ExcelRosterSummary
          people={people}
          duplicates={duplicates}
          onRemove={(sapId) => {
            const remaining = people.filter((person) => person.sapId !== sapId);
            setPeople(remaining);
            onChange(remaining);
          }}
        />
      )}
    </div>
  );
}

const PEOPLE_PER_PAGE = 24;

export function ExcelRosterSummary({
  people,
  duplicates = 0,
  countLabel = "已选",
  onRemove,
}: {
  people: DistributeTarget[];
  duplicates?: number;
  countLabel?: string;
  onRemove?: (sapId: string) => void;
}) {
  const [selectedBranch, setSelectedBranch] = useState("*");
  const [keyword, setKeyword] = useState("");
  const [page, setPage] = useState(0);
  const searchRef = useRef<HTMLInputElement>(null);
  const headingRef = useRef<HTMLParagraphElement>(null);
  const gridRef = useRef<HTMLUListElement>(null);

  const branches = useMemo(() => {
    const groups = new Map<string, { name: string; count: number }>();
    for (const person of people) {
      const id = person.bbkId ?? "";
      const group = groups.get(id) ?? { name: "", count: 0 };
      if (!group.name && typeof person.branchName === "string") {
        group.name = person.branchName.trim();
      }
      group.count++;
      groups.set(id, group);
    }
    for (const [id, group] of groups) {
      group.name ||= BBK_ID_TO_NAME_MAP[id] || id || "未提供分行";
    }
    return groups;
  }, [people]);
  const branchFilter = branches.has(selectedBranch) ? selectedBranch : "*";
  const search = keyword.trim().toLowerCase();
  const visible = people.filter((person) => {
    const id = person.bbkId ?? "";
    return (
      (branchFilter === "*" || branchFilter === id) &&
      (!search ||
        `${person.name} ${person.sapId} ${branches.get(id)?.name} ${id} ${
          person.orgName
        } ${person.bbkOrgId ?? ""}`
          .toLowerCase()
          .includes(search))
    );
  });
  const pageCount = Math.ceil(visible.length / PEOPLE_PER_PAGE);
  const currentPage = Math.min(page, Math.max(0, pageCount - 1));
  useEffect(() => {
    if (gridRef.current) gridRef.current.scrollTop = 0;
  }, [currentPage, branchFilter, search]);

  return (
    <div className={styles.roster}>
      <p ref={headingRef} tabIndex={-1} role="status">
        <strong>
          {countLabel} {people.length} 人
        </strong>{" "}
        · {branches.size} 家分行
        {duplicates > 0 && ` · 已去除 ${duplicates} 条重复记录`}
      </p>
      {people.length ? (
        <div className={styles.rosterBody}>
          <div className={styles.peopleToolbar}>
            <input
              ref={searchRef}
              className={cardStyles.search}
              type="search"
              aria-label="搜索已选人员"
              placeholder="搜索姓名 / SAP 用户 ID / 分行 / 机构名称或编码"
              value={keyword}
              onChange={(event) => {
                setKeyword(event.currentTarget.value);
                setPage(0);
              }}
            />
            {(branches.size > 1 || branchFilter !== "*") && (
              <select
                className={cardStyles.search}
                aria-label="筛选分行"
                value={branchFilter}
                onChange={(event) => {
                  setSelectedBranch(event.currentTarget.value);
                  setPage(0);
                }}
              >
                <option value="*">全部分行</option>
                {[...branches].map(([id, group]) => (
                  <option key={id} value={id}>
                    {group.name}（{group.count} 人）
                  </option>
                ))}
              </select>
            )}
          </div>
          {visible.length ? (
            <ul
              ref={gridRef}
              className={styles.peopleGrid}
              aria-label="人员名单"
            >
              {visible
                .slice(
                  currentPage * PEOPLE_PER_PAGE,
                  (currentPage + 1) * PEOPLE_PER_PAGE,
                )
                .map((person) => {
                  const branchName = branches.get(person.bbkId ?? "")?.name;
                  const org = person.orgName.trim() || person.bbkOrgId;
                  const metadata = [
                    person.sapId,
                    branchName,
                    org === branchName ? undefined : org,
                  ]
                    .filter(Boolean)
                    .join(" · ");
                  return (
                    <li
                      className={cx(cardStyles.person, cardStyles.selected)}
                      key={person.sapId}
                    >
                      <span className={cardStyles.avatar} aria-hidden="true">
                        {person.name.slice(0, 1)}
                      </span>
                      <span
                        className={cardStyles.personInfo}
                        title={`${person.name} · ${metadata}`}
                      >
                        <strong>{person.name}</strong>
                        <small>{metadata}</small>
                      </span>
                      {onRemove && (
                        <button
                          type="button"
                          className={cx(workbenchStyles.iconbtn, styles.remove)}
                          aria-label={`移除 ${person.name} ${person.sapId}`}
                          title="从本次分发名单移除"
                          onClick={() => {
                            if (people.length === 1)
                              headingRef.current?.focus();
                            else searchRef.current?.focus();
                            if (
                              branchFilter !== "*" &&
                              branches.get(branchFilter)?.count === 1
                            ) {
                              setSelectedBranch("*");
                              setPage(0);
                            } else {
                              setPage(currentPage);
                            }
                            onRemove(person.sapId);
                          }}
                        >
                          <Icon name="close" />
                        </button>
                      )}
                    </li>
                  );
                })}
            </ul>
          ) : (
            <p className={styles.emptyPeople}>没有符合筛选条件的人员</p>
          )}
          {pageCount > 1 && (
            <div className={styles.pagination} aria-label="已选人员分页">
              <button
                type="button"
                className={cx(workbenchStyles.btn, workbenchStyles.sm)}
                disabled={currentPage === 0}
                onClick={() => setPage(currentPage - 1)}
              >
                上一页
              </button>
              <span role="status">
                {currentPage + 1} / {pageCount} 页
              </span>
              <button
                type="button"
                className={cx(workbenchStyles.btn, workbenchStyles.sm)}
                disabled={currentPage + 1 === pageCount}
                onClick={() => setPage(currentPage + 1)}
              >
                下一页
              </button>
            </div>
          )}
        </div>
      ) : (
        <p className={styles.emptyPeople}>名单已清空，可更换文件重新导入。</p>
      )}
    </div>
  );
}
