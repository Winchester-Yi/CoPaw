import { OperateCard, useProviderContext } from "@/components/agentscope-chat";
import {
  SparkCheckCircleFill,
  SparkCopyLine,
  SparkErrorCircleFill,
  SparkLoadingLine,
  SparkLockFill,
  SparkStopCircleLine,
  SparkTimeLine,
  SparkToolLine,
  SparkTrueLine,
  SparkWarningCircleFill,
} from "@agentscope-ai/icons";
import { CodeBlock, IconButton } from "@agentscope-ai/design";
import { copy } from "../../Util/copy";
import { useRef, useState } from "react";

function extractPlainText(value: any): string | null {
  if (!value) return null;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return value;
    if (
      (trimmed.startsWith("[") && trimmed.endsWith("]")) ||
      (trimmed.startsWith("{") && trimmed.endsWith("}"))
    ) {
      try {
        return extractPlainText(JSON.parse(trimmed));
      } catch {
        return value;
      }
    }
    return value;
  }
  if (Array.isArray(value)) {
    const texts = value
      .map((item) => extractPlainText(item))
      .filter((item): item is string => !!item);
    return texts.length ? texts.join("\n") : null;
  }
  if (value.type === "text" && typeof value.text === "string") {
    return value.text;
  }
  if (typeof value.text === "string") {
    return value.text;
  }
  if (Array.isArray(value.content)) {
    return extractPlainText(value.content);
  }
  return null;
}

function stringifyContent(value: any) {
  const plainText = extractPlainText(value);
  if (plainText !== null) {
    return {
      contentString: plainText,
      displayLanguage: "text" as const,
    };
  }
  return {
    contentString:
      typeof value === "string" ? value : JSON.stringify(value, null, 2),
    displayLanguage: "json" as const,
  };
}

function Block(props: {
  title: string;
  content: any;
  summary?: string;
  expandEnabled?: boolean;
  defaultExpanded?: boolean;
  language?: "json" | "text";
}) {
  const { getPrefixCls } = useProviderContext();
  const prefixCls = getPrefixCls("operate-card");
  const { expandEnabled = false, defaultExpanded = true, summary } = props;
  const { contentString, displayLanguage } = stringifyContent(props.content);
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(defaultExpanded);
  const timer = useRef<NodeJS.Timeout | null>(null);

  const showContent = expanded || (summary && !expanded);
  const displayContent = summary && !expanded ? summary : contentString;

  return (
    <div className={`${prefixCls}-tool-call-block`}>
      <div
        className={`${prefixCls}-tool-call-block-header`}
        onClick={() => {
          if (expandEnabled === true) {
            setExpanded((prev) => !prev);
          }
        }}
      >
        <span className={`${prefixCls}-tool-call-block-title`}>
          {props.title}
        </span>
        {expandEnabled && summary && (
          <span className={`${prefixCls}-tool-call-block-expand-indicator`}>
            {expanded ? "收起" : "展开详情"}
          </span>
        )}
        <div
          className={`${prefixCls}-tool-call-block-extra`}
          onClick={(e) => e.stopPropagation()}
        >
          <IconButton
            size="small"
            style={{ marginRight: "-6px" }}
            icon={copied ? <SparkTrueLine /> : <SparkCopyLine />}
            bordered={false}
            onClick={() => {
              copy(contentString)
                .then(() => {
                  clearTimeout(timer.current);
                  setCopied(true);
                  timer.current = setTimeout(() => {
                    setCopied(false);
                  }, 2000);
                })
                .catch(() => {
                  console.warn("Copy failed");
                });
            }}
          />
        </div>
      </div>
      {showContent && (
        <div className={`${prefixCls}-tool-call-block-content`}>
          {/* @ts-ignore */}
          <CodeBlock
            language={props.language || displayLanguage}
            value={displayContent}
            readOnly={true}
            basicSetup={{ lineNumbers: false, foldGutter: false }}
          />
        </div>
      )}
    </div>
  );
}

export interface IToolCallProps {
  /**
   * @description 标题
   * @descriptionEn Title
   * @default 'Call Tool'
   */
  title?: string;
  /**
   * @description 副标题
   * @descriptionEn Subtitle
   * @default ''
   */
  subTitle?: string;
  /**
   * @description 工具调用入参
   * @descriptionEn Tool Call Input
   * @default ''
   */
  input: any;
  /**
   * @description 工具调用输出
   * @descriptionEn Tool Call Output
   * @default ''
   */
  output: any;
  /**
   * @description 输出摘要
   * @descriptionEn Output Summary
   */
  outputSummary?: string;
  /**
   * @description 默认展开
   * @descriptionEn Default Open
   */
  defaultOpen?: boolean;
  /**
   * @description 是否正在生成
   * @descriptionEn Whether is generating
   * @default false
   */
  loading?: boolean;
  /**
   * @description 消息状态
   * @descriptionEn Message status
   */
  msgStatus?: string;
  outputBlock?: { language?: "json" | "text" };
  inputBlock?: { language?: "json" | "text" };
}

export default function (props: IToolCallProps) {
  const {
    title = "Call Tool",
    subTitle,
    defaultOpen = true,
    loading = false,
    msgStatus,
    outputSummary,
  } = props;

  const badgeStyle: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 3,
    padding: "1px 6px",
    fontSize: 11,
    lineHeight: "16px",
    borderRadius: 99,
    whiteSpace: "nowrap",
  };

  let statusBadge: React.ReactNode = null;
  if (!loading) {
    switch (msgStatus) {
      case "completed":
        statusBadge = (
          <span
            style={{
              ...badgeStyle,
              color: "#52c41a",
              border: "1px solid #52c41a",
            }}
          >
            <SparkCheckCircleFill />
            完成
          </span>
        );
        break;
      case "failed":
        statusBadge = (
          <span
            style={{
              ...badgeStyle,
              color: "#ff4d4f",
              border: "1px solid #ff4d4f",
            }}
          >
            <SparkErrorCircleFill />
            失败
          </span>
        );
        break;
      case "canceled":
        statusBadge = (
          <span
            style={{
              ...badgeStyle,
              color: "#8c8c8c",
              border: "1px solid #8c8c8c",
            }}
          >
            <SparkStopCircleLine />
            已取消
          </span>
        );
        break;
      case "rejected":
        statusBadge = (
          <span
            style={{
              ...badgeStyle,
              color: "#fa8c16",
              border: "1px solid #fa8c16",
            }}
          >
            <SparkWarningCircleFill />
            已拒绝
          </span>
        );
        break;
      case "pending":
        statusBadge = (
          <span
            style={{
              ...badgeStyle,
              color: "#d48806",
              border: "1px solid #d48806",
            }}
          >
            <SparkTimeLine />
            待审批
          </span>
        );
        break;
      case "blocked":
        statusBadge = (
          <span
            style={{
              ...badgeStyle,
              color: "#fa8c16",
              border: "1px solid #fa8c16",
            }}
          >
            <SparkLockFill />
            已拦截
          </span>
        );
        break;
    }
  }

  return (
    <OperateCard
      header={{
        icon: loading ? <SparkLoadingLine spin /> : <SparkToolLine />,
        title: title,
        description: subTitle,
        extra: statusBadge,
      }}
      body={{
        defaultOpen: defaultOpen,
        children: (
          <OperateCard.LineBody>
            <Block
              title="输入"
              content={props.input}
              language={props.inputBlock?.language}
              expandEnabled={true}
              defaultExpanded={true}
            />
            <Block
              title="输出"
              content={props.output}
              summary={outputSummary}
              language={props.outputBlock?.language}
              expandEnabled={!!outputSummary}
              defaultExpanded={true}
            />
          </OperateCard.LineBody>
        ),
      }}
    ></OperateCard>
  );
}
