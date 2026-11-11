import { Dropdown } from "antd";
import { ArrowDown, ArrowUp, ArrowUpDown, Filter } from "lucide-react";
import type { ListSort, SortKey } from "./listOrder";
import workbenchStyles from "../../index.module.less";
import styles from "./index.module.less";

export function TableHeading({
  label,
  sort,
  onSort,
  filter,
}: {
  label: string;
  sort: ListSort;
  onSort: (key: SortKey) => void;
  filter?: {
    value: number | "all";
    labels: Readonly<Record<number, string>>;
    allLabel: string;
    onChange: (value: number | "all") => void;
  };
}) {
  const key =
    label === "资产" ? "assets" : label === "接触计划" ? "contactPlan" : null;
  if (key) {
    const direction = sort?.key === key ? sort.direction : null;
    const SortIcon =
      direction === "asc"
        ? ArrowUp
        : direction === "desc"
        ? ArrowDown
        : ArrowUpDown;
    return (
      <th
        scope="col"
        aria-sort={
          direction === "asc"
            ? "ascending"
            : direction === "desc"
            ? "descending"
            : "none"
        }
      >
        <button
          type="button"
          className={styles.sortTrigger}
          title={
            key === "assets" ? "按资产金额排序" : "按最新一条接触计划的日期排序"
          }
          aria-label={`${label}排序，当前${
            direction === "asc"
              ? "升序"
              : direction === "desc"
              ? "降序"
              : "默认顺序"
          }`}
          onClick={() => onSort(key)}
        >
          {label}
          <SortIcon size={14} aria-hidden />
        </button>
      </th>
    );
  }
  if (!filter) return <th scope="col">{label}</th>;
  return (
    <th scope="col">
      <Dropdown
        trigger={["click"]}
        menu={{
          selectable: true,
          selectedKeys: [String(filter.value)],
          items: [
            { key: "all", label: filter.allLabel },
            ...Object.entries(filter.labels).map(([key, value]) => ({
              key,
              label: value,
            })),
          ],
          onClick: ({ key }) =>
            filter.onChange(key === "all" ? "all" : Number(key)),
        }}
      >
        <button
          type="button"
          className={`${workbenchStyles.tagFilterTrigger} ${
            styles.filterTrigger
          } ${filter.value !== "all" ? workbenchStyles.filtered : ""}`}
          aria-label={`筛选${label}`}
        >
          {label}
          <Filter size={14} aria-hidden />
        </button>
      </Dropdown>
      {filter.value !== "all" && (
        <div className={workbenchStyles.tagFilterApplied}>
          <span>{filter.labels[filter.value]}</span>
          <button
            type="button"
            aria-label={`清除${label}筛选`}
            onClick={() => filter.onChange("all")}
          >
            ×
          </button>
        </div>
      )}
    </th>
  );
}
