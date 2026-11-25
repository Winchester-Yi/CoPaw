import React from "react";
import { Input } from "antd";
import { Trash2 } from "lucide-react";
import { IconButton } from "@agentscope-ai/design";
import { SparkEditLine } from "@agentscope-ai/icons";
import { getChannelIconUrl } from "../../../Control/Channels/components";
import styles from "./index.module.less";

interface ChatSessionItemProps {
  /** Session display name */
  name: string;
  /** Pre-formatted creation time string */
  time: string;
  /** Channel key (e.g. console, dingtalk) — used with shared channel icons */
  channelKey?: string;
  /** Localized channel label (e.g. Console, DingTalk) */
  channelLabel?: string;
  /** Whether this is the currently selected session */
  active?: boolean;
  /** Whether the item is in inline-edit mode */
  editing?: boolean;
  /** Current value of the edit input */
  editValue?: string;
  /** Click callback */
  onClick?: () => void;
  /** Edit button callback */
  onEdit?: () => void;
  /** Delete button callback */
  onDelete?: () => void;
  /** Edit input value change callback */
  onEditChange?: (value: string) => void;
  /** Confirm edit callback (Enter key or blur) */
  onEditSubmit?: () => void;
  /** Cancel edit callback */
  onEditCancel?: () => void;
  /** Whether to show edit button (default: true) */
  showEdit?: boolean;
  /** Whether to show timeline indicator (left line + dot) (default: true) */
  showTimeline?: boolean;
  /** Whether to show channel tag (default: true) */
  showChannel?: boolean;
  titleAction?: React.ReactNode;
  className?: string;
  /** Custom style for virtual scrolling positioning */
  style?: React.CSSProperties;
}

const ChatSessionItem: React.FC<ChatSessionItemProps> = (props) => {
  // Apply compact style when timeline and channel are hidden
  const isCompact = props.showTimeline === false && props.showChannel === false;

  const className = [
    styles.chatSessionItem,
    props.active ? styles.active : "",
    props.editing ? styles.editing : "",
    isCompact ? styles.compact : "",
    props.className || "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={className}
      onClick={props.editing ? undefined : props.onClick}
      style={props.style}
    >
      {/* Timeline indicator placeholder */}
      {props.showTimeline !== false && (
        <div className={styles.iconPlaceholder} />
      )}
      <div className={styles.content}>
        {props.editing ? (
          <Input
            autoFocus
            size="small"
            value={props.editValue}
            onChange={(e) => props.onEditChange?.(e.target.value)}
            onPressEnter={props.onEditSubmit}
            onBlur={props.onEditSubmit}
            onClick={(e) => e.stopPropagation()}
          />
        ) : (
          <div className={styles.titleRow}>
            <div className={styles.name} title={props.name}>
              {props.name}
            </div>
            {props.titleAction}
          </div>
        )}
        <div className={styles.metaRow}>
          <span className={styles.time}>{props.time}</span>
          {props.showChannel !== false &&
            (props.channelKey || props.channelLabel) && (
              <span
                className={styles.channelTag}
                title={props.channelLabel || props.channelKey}
              >
                {props.channelKey ? (
                  <img
                    className={styles.channelIcon}
                    src={getChannelIconUrl(props.channelKey)}
                    alt=""
                    loading="lazy"
                    decoding="async"
                  />
                ) : null}
                {props.channelLabel ? (
                  <span className={styles.channelTagText}>
                    {props.channelLabel}
                  </span>
                ) : null}
              </span>
            )}
        </div>
      </div>
      {/* Action buttons visible on hover */}
      {!props.editing && !props.titleAction && (
        <div className={styles.actions}>
          {props.showEdit !== false && (
            <IconButton
              bordered={false}
              size="small"
              icon={<SparkEditLine />}
              onClick={(e) => {
                e.stopPropagation();
                props.onEdit?.();
              }}
            />
          )}
          <IconButton
            bordered={false}
            size="small"
            icon={
              <span className={styles.deleteIcon}>
                <Trash2 size={15} strokeWidth={1.9} />
              </span>
            }
            onClick={(e) => {
              e.stopPropagation();
              props.onDelete?.();
            }}
          />
        </div>
      )}
    </div>
  );
};

export default ChatSessionItem;
