import {
  DeleteOutlined,
  DownOutlined,
  PlusOutlined,
  UpOutlined,
} from "@ant-design/icons";
import {
  Button,
  Drawer,
  Empty,
  Popconfirm,
  Select,
  Space,
  Tabs,
  Tag,
} from "antd";
import type { ReactNode } from "react";
import { useState } from "react";

import { eventMetadata } from "../eventMetadata";
import type {
  HookEventName,
  HookHandlerType,
  HookMatcherGroupDraft,
} from "../types";

import styles from "../index.module.less";

type EventEditorDrawerProps = {
  event: HookEventName | null;
  groups: HookMatcherGroupDraft[];
  templateLabel?: string;
  basicDetails: ReactNode;
  details: ReactNode;
  dirty: boolean;
  scopeDetails: ReactNode;
  saving: boolean;
  testDetails: ReactNode;
  onAddGroup: () => void;
  onAddHandler: (groupId: string, type: HookHandlerType) => void;
  onClose: () => void;
  onMoveHandler: (groupId: string, fromIndex: number, toIndex: number) => void;
  onRemoveEvent: () => void;
  onRemoveGroup: (groupId: string) => void;
  onRemoveHandler: (groupId: string, handlerId: string) => void;
  onSave: () => void;
  onSelectGroup: (groupId: string) => void;
  onSelectHandler: (groupId: string, handlerId: string) => void;
};

function describeMatcher(group: HookMatcherGroupDraft): string {
  return group.matcher.tools.length
    ? `仅 ${group.matcher.tools.join("、")} 工具`
    : "所有工具";
}

function handlerSummary(
  handler: HookMatcherGroupDraft["hooks"][number],
): string {
  if (handler.type === "http") return String(handler.url || "等待填写请求地址");
  if (handler.type === "prompt")
    return String(handler.prompt || "等待填写 Prompt");
  return Array.isArray(handler.argv)
    ? handler.argv.filter(Boolean).join(" ") || "等待填写命令参数"
    : "等待填写命令参数";
}

export function EventEditorDrawer({
  event,
  groups,
  templateLabel,
  basicDetails,
  details,
  dirty,
  scopeDetails,
  saving,
  testDetails,
  onAddGroup,
  onAddHandler,
  onClose,
  onMoveHandler,
  onRemoveEvent,
  onRemoveGroup,
  onRemoveHandler,
  onSave,
  onSelectGroup,
  onSelectHandler,
}: EventEditorDrawerProps) {
  const [activeTab, setActiveTab] = useState("pipeline");
  if (!event) return null;
  const metadata = eventMetadata[event];

  const scopeContent = (
    <div className={styles.scopeWorkspace}>
      <div className={styles.drawerSection}>
        <h3>适用范围</h3>
        <p>{metadata.description}</p>
      </div>
      {groups.length ? (
        groups.map((group) => (
          <div key={group.id} className={styles.groupPanel}>
            <div className={styles.groupSummary}>
              <Button
                type="text"
                onClick={() => {
                  onSelectGroup(group.id);
                  setActiveTab("pipeline");
                }}
              >
                <strong>{describeMatcher(group)}</strong>
              </Button>
              <span>{group.hooks.length} 个处理器</span>
              <Button
                danger
                size="small"
                type="text"
                onClick={() => onRemoveGroup(group.id)}
              >
                删除分组
              </Button>
            </div>
          </div>
        ))
      ) : (
        <Empty description="尚未添加匹配分组" />
      )}
      <Button icon={<PlusOutlined />} type="dashed" onClick={onAddGroup}>
        添加分组
      </Button>
      {scopeDetails}
    </div>
  );

  const pipelineContent = (
    <div className={styles.pipelineWorkspace}>
      <section className={styles.pipelineList}>
        <div className={styles.pipelineHeading}>
          <div>
            <h3>执行顺序</h3>
            <p>处理器按阶段执行；追加处理器并发生成，按配置顺序展示。</p>
          </div>
          <span>
            {groups.reduce((count, group) => count + group.hooks.length, 0)} 个
          </span>
        </div>
        {groups.length ? (
          groups.map((group) => (
            <div key={group.id} className={styles.pipelineGroup}>
              <span className={styles.pipelineGroupLabel}>
                {describeMatcher(group)}
              </span>
              {group.hooks.map((handler, index) => (
                <div className={styles.pipelineStep} key={handler.id}>
                  <span className={styles.pipelineStepNumber}>{index + 1}</span>
                  <Button
                    aria-label={`编辑 ${handler.id}`}
                    className={styles.pipelineStepButton}
                    type="text"
                    onClick={() => onSelectHandler(group.id, handler.id)}
                  >
                    <strong>{handler.id}</strong>
                    <span>{handlerSummary(handler)}</span>
                    <Tag>{handler.type}</Tag>
                    {handler.outputTransform && (
                      <Tag color="blue">输出转换</Tag>
                    )}
                  </Button>
                  <Space className={styles.pipelineActions} size={0}>
                    <Button
                      aria-label={`${handler.id} 上移`}
                      disabled={index === 0}
                      icon={<UpOutlined />}
                      size="small"
                      type="text"
                      onClick={() => onMoveHandler(group.id, index, index - 1)}
                    />
                    <Button
                      aria-label={`${handler.id} 下移`}
                      disabled={index === group.hooks.length - 1}
                      icon={<DownOutlined />}
                      size="small"
                      type="text"
                      onClick={() => onMoveHandler(group.id, index, index + 1)}
                    />
                    <Button
                      aria-label={`删除 ${handler.id}`}
                      danger
                      icon={<DeleteOutlined />}
                      size="small"
                      type="text"
                      onClick={() => onRemoveHandler(group.id, handler.id)}
                    />
                  </Space>
                </div>
              ))}
              <Select
                className={styles.handlerTypeSelect}
                placeholder="添加处理器"
                options={(
                  ["command", "http", "prompt"] as HookHandlerType[]
                ).map((type) => ({ value: type, label: type }))}
                onChange={(type: HookHandlerType) =>
                  onAddHandler(group.id, type)
                }
              />
            </div>
          ))
        ) : (
          <Empty description="请先添加匹配分组" />
        )}
      </section>
      <section className={styles.handlerDetailPanel}>{details}</section>
    </div>
  );

  return (
    <Drawer
      aria-label={`编辑 ${event}`}
      destroyOnClose
      open
      placement="right"
      title={
        <div className={styles.drawerTitle}>
          <strong>编辑 {event}</strong>
          {dirty && <Tag color="warning">草稿</Tag>}
          {templateLabel && <Tag color="blue">{templateLabel}</Tag>}
        </div>
      }
      width="min(1040px, 100vw)"
      onClose={onClose}
      extra={
        <Space>
          <Popconfirm
            cancelText="取消"
            description="删除后该事件不会再执行任何 Hook。"
            okButtonProps={{ danger: true }}
            okText="确认删除"
            title={`删除 ${event} 事件？`}
            onConfirm={onRemoveEvent}
          >
            <Button danger>删除事件</Button>
          </Popconfirm>
          <Button
            aria-label={`保存并激活 ${event}`}
            type="primary"
            loading={saving}
            onClick={onSave}
          >
            保存并激活
          </Button>
        </Space>
      }
    >
      <Tabs
        activeKey={activeTab}
        className={styles.drawerTabs}
        items={[
          { key: "basic", label: "基本设置", children: basicDetails },
          { key: "scope", label: "适用范围", children: scopeContent },
          { key: "pipeline", label: "处理器编排", children: pipelineContent },
          { key: "test", label: "测试与发布", children: testDetails },
        ]}
        onChange={setActiveTab}
      />
    </Drawer>
  );
}
