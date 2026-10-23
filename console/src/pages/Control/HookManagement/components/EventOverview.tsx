import {
  CheckCircleFilled,
  PlusOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { Button, Switch, Tag } from "antd";

import { eventMetadata } from "../eventMetadata";
import { getEventSummary, getLifecycleEvents } from "../overviewModel";
import type { HookConfigDraft, HookEventName } from "../types";

import styles from "../index.module.less";

type EventOverviewProps = {
  config: HookConfigDraft;
  dirty: boolean;
  onEnabledChange: (enabled: boolean) => void;
  onEdit: (event: HookEventName) => void;
  onCreate: () => void;
};

export function EventOverview({
  config,
  dirty,
  onEnabledChange,
  onEdit,
  onCreate,
}: EventOverviewProps) {
  const lifecycleEvents = getLifecycleEvents(config);
  const configuredEvents = lifecycleEvents.filter(
    (event) => getEventSummary(config, event).configured,
  );
  const unconfiguredEvents = lifecycleEvents.filter(
    (event) => !getEventSummary(config, event).configured,
  );
  const handlerCount = configuredEvents.reduce(
    (total, event) => total + getEventSummary(config, event).handlers,
    0,
  );

  return (
    <section className={styles.overview}>
      <div className={styles.overviewHeader}>
        <div>
          <h2>事件配置</h2>
          <p>按生命周期查看规则覆盖与处理器执行链。</p>
        </div>
        <div className={styles.overviewActions}>
          {dirty && <Tag color="processing">未保存更改</Tag>}
          <Button
            aria-label="新建 Hook 规则"
            icon={<PlusOutlined />}
            type="primary"
            onClick={onCreate}
          >
            新建 Hook 规则
          </Button>
        </div>
      </div>

      <section aria-label="Hook 状态" className={styles.statusBanner}>
        <div className={styles.statusCopy}>
          <CheckCircleFilled
            className={
              config.enabled ? styles.statusEnabled : styles.statusDisabled
            }
          />
          <div>
            <strong>{config.enabled ? "Hook 已启用" : "Hook 已停用"}</strong>
            <span>{dirty ? "当前配置存在未保存修改" : "当前配置正在生效"}</span>
          </div>
        </div>
        <span className={styles.globalSwitch}>
          <Switch
            checked={config.enabled}
            onChange={onEnabledChange}
            aria-label="启用 Hook"
          />
          全局启用
        </span>
      </section>

      <div className={styles.metricGrid}>
        <article className={styles.metricCard}>
          <span>已配置事件</span>
          <strong>{configuredEvents.length}</strong>
          <small>共 {lifecycleEvents.length} 个生命周期事件</small>
        </article>
        <article className={styles.metricCard}>
          <span>处理器数量</span>
          <strong>{handlerCount}</strong>
          <small>按事件及处理阶段执行</small>
        </article>
        <article className={styles.metricCard}>
          <span>待发布修改</span>
          <strong>{dirty ? 1 : 0}</strong>
          <small>{dirty ? "保存后才会激活" : "当前没有待发布修改"}</small>
        </article>
      </div>

      <section className={styles.lifecyclePanel}>
        <div className={styles.sectionHeading}>
          <h3>生命周期总览</h3>
          <span>事件按运行时顺序触发</span>
        </div>
        <div className={styles.lifecycleTrack}>
          {lifecycleEvents.map((event, index) => {
            const summary = getEventSummary(config, event);
            return (
              <div className={styles.lifecycleItem} key={event}>
                <button
                  className={styles.lifecycleStep}
                  data-configured={summary.configured}
                  type="button"
                  onClick={() => onEdit(event)}
                >
                  <span className={styles.lifecycleMarker}>
                    {summary.configured ? <CheckCircleFilled /> : index + 1}
                  </span>
                  <span>
                    <strong>{event}</strong>
                    <small>{eventMetadata[event].label}</small>
                  </span>
                </button>
                {index < lifecycleEvents.length - 1 && (
                  <RightOutlined className={styles.lifecycleArrow} />
                )}
              </div>
            );
          })}
        </div>
      </section>

      <section className={styles.eventTable}>
        <div className={`${styles.eventRow} ${styles.eventRowHeader}`}>
          <span>事件</span>
          <span>处理器链路</span>
          <span>状态</span>
          <span>操作</span>
        </div>
        {configuredEvents.map((event) => {
          const summary = getEventSummary(config, event);
          const remainingCount = summary.handlerLabels.length - 3;
          return (
            <div className={styles.eventRow} key={event}>
              <div className={styles.eventName}>
                <strong>{event}</strong>
                <span>{eventMetadata[event].label}</span>
              </div>
              <div className={styles.processorChain}>
                {summary.handlerLabels.slice(0, 3).map((label, index) => (
                  <Tag key={`${event}-${label}-${index}`}>{label}</Tag>
                ))}
                {remainingCount > 0 && <Tag>+{remainingCount}</Tag>}
                {summary.handlers === 0 && <span>尚未添加处理器</span>}
              </div>
              <Tag color={dirty ? "warning" : "success"}>
                {dirty ? "草稿" : "已生效"}
              </Tag>
              <Button
                aria-label={`编辑配置 ${event}`}
                type="link"
                onClick={() => onEdit(event)}
              >
                编辑配置
              </Button>
            </div>
          );
        })}
      </section>

      <section className={styles.unconfiguredEvents}>
        <div className={styles.sectionHeading}>
          <h3>未配置事件 ({unconfiguredEvents.length})</h3>
          <span>配置后才会在对应生命周期执行 Hook。</span>
        </div>
        <div className={styles.unconfiguredList}>
          {unconfiguredEvents.map((event) => (
            <Button
              key={event}
              aria-label={`新建规则 ${event}`}
              className={styles.unconfiguredEvent}
              icon={<PlusOutlined />}
              onClick={() => onEdit(event)}
            >
              <strong>{event}</strong>
              <span>{eventMetadata[event].label}</span>
            </Button>
          ))}
        </div>
      </section>
    </section>
  );
}
