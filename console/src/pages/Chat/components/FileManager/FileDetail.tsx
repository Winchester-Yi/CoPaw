import { Alert, Button, Descriptions, Empty, Spin, Tabs, Typography } from "antd";
import {
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  FileOutlined,
  ReloadOutlined,
  SaveOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import { forwardRef, useEffect, useImperativeHandle, useMemo, useState } from "react";
import type { FileManagerItem, FileManagerTextPreview } from "@/api/modules/chat";
import Markdown from "@/components/agentscope-chat/Markdown";
import { getContentType, getFileIcon } from "@/components/agentscope-chat/FilePreviewModal/fileUtils";
import styles from "./index.module.less";

export interface FileDetailProps {
  entry: FileManagerItem | null;
  preview: FileManagerTextPreview | null;
  binaryPreviewUrl?: string | null;
  loading?: boolean;
  error?: string | null;
  editable: boolean;
  onDownload: () => void;
  onEditStateChange?: (dirty: boolean) => void;
  onSave: (content: string, revision: string) => Promise<FileManagerTextPreview | void>;
  onArchive: () => void;
  onRestore: () => void;
  onPurge: () => void;
}

export interface FileDetailHandle {
  saveDraft: () => Promise<boolean>;
}

function extension(name: string) {
  return name.split(".").pop()?.toLowerCase() || "";
}

function previewKind(name: string) {
  const ext = extension(name);
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext)) return "image";
  if (["mp4", "webm"].includes(ext)) return "video";
  if (["mp3", "wav", "ogg"].includes(ext)) return "audio";
  if (ext === "pdf") return "pdf";
  if (["md", "mdx"].includes(ext)) return "markdown";
  if (["html", "htm"].includes(ext)) return "html";
  return "text";
}

function formattedSize(value?: number | null) {
  if (!value) return "0 B";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

const FileDetail = forwardRef<FileDetailHandle, FileDetailProps>(function FileDetail({
  entry,
  preview,
  binaryPreviewUrl,
  loading = false,
  error,
  editable,
  onDownload,
  onEditStateChange,
  onSave,
  onArchive,
  onRestore,
  onPurge,
}, ref) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const kind = useMemo(() => (entry ? previewKind(entry.name) : "text"), [entry]);

  useEffect(() => {
    setEditing(false);
    setDraft(preview?.content || "");
    onEditStateChange?.(false);
  }, [entry?.path, onEditStateChange, preview?.content]);

  const canEdit = editable && preview?.editable && !preview.is_truncated;
  const content = preview?.content || "";
  const handleDraft = (value: string) => {
    setDraft(value);
    onEditStateChange?.(value !== content);
  };
  const save = async () => {
    if (!preview?.revision) return;
    setSaving(true);
    try {
      await onSave(draft, preview.revision);
      setEditing(false);
      onEditStateChange?.(false);
    } finally {
      setSaving(false);
    }
  };
  const abandonEdit = () => {
    setDraft(content);
    setEditing(false);
    onEditStateChange?.(false);
  };

  useImperativeHandle(ref, () => ({
    saveDraft: async () => {
      if (!editing) return true;
      try { await save(); return true; } catch { return false; }
    },
  }), [draft, editing, preview?.revision]);

  if (!entry) {
    return <section className={styles.detail} aria-label="文件详情"><Empty description="选择文件以预览详情" /></section>;
  }

  const { icon } = entry.kind === "file" ? getFileIcon(entry.name, 32) : { icon: <FileOutlined /> };

  const visualPreview = () => {
    if (loading) return <div className={styles.previewState}><Spin /></div>;
    if (error) return <Alert type="error" showIcon message="无法读取文件" description={error} />;
    if (entry.kind !== "file") return <Alert type="warning" showIcon message="受限项目" description="符号链接和特殊文件不可读取或下载。" />;
    if (preview?.is_truncated) {
      return <>
        <Alert type="info" showIcon message="仅预览前 1 MB 内容" description="文件超过 1 MB，完整内容请下载后处理，不能编辑。" />
        <pre className={styles.textPreview}>{content || "（空文件）"}</pre>
      </>;
    }
    if (!preview?.is_text) {
      if (!binaryPreviewUrl) return <Alert type="info" showIcon message="此格式不提供内嵌预览" description="可下载单个文件后在本地打开。" />;
      if (kind === "image") return <div className={styles.mediaPreview}><img src={binaryPreviewUrl} alt={entry.name} /></div>;
      if (kind === "video") return <div className={styles.mediaPreview}><video controls src={binaryPreviewUrl}>浏览器不支持此视频预览。</video></div>;
      if (kind === "audio") return <div className={styles.mediaPreview}><audio controls src={binaryPreviewUrl}>浏览器不支持此音频预览。</audio></div>;
      if (kind === "pdf") return <iframe className={styles.htmlPreview} title={`${entry.name} PDF 预览`} src={binaryPreviewUrl} />;
      return <Alert type="info" showIcon message="此格式不提供内嵌预览" description="可下载单个文件后在本地打开。" />;
    }
    if (editing) return <textarea aria-label="文件内容" className={styles.editor} value={draft} onChange={(event) => handleDraft(event.target.value)} />;
    if (kind === "markdown") return <div className={styles.markdownPreview}><Markdown content={content} /></div>;
    if (kind === "html") return <iframe className={styles.htmlPreview} title={`${entry.name} 预览`} sandbox="allow-scripts" srcDoc={content} />;
    return <pre className={styles.textPreview}>{content || "（空文件）"}</pre>;
  };

  return (
    <section className={styles.detail} aria-label="文件详情">
      <header className={styles.detailHeader}>
        <span className={styles.detailIcon} aria-hidden="true">{icon}</span>
        <div className={styles.detailTitle}><Typography.Text strong ellipsis>{entry.name}</Typography.Text></div>
        <div className={styles.detailActions}>
          {entry.capabilities.download && <Button size="small" icon={<DownloadOutlined />} onClick={onDownload}>下载</Button>}
          {canEdit && !editing && <Button size="small" icon={<EditOutlined />} onClick={() => setEditing(true)}>编辑</Button>}
          {editing && <Button size="small" type="primary" loading={saving} icon={<SaveOutlined />} onClick={() => void save()}>保存</Button>}
          {entry.archive_item_id ? <><Button size="small" icon={<UndoOutlined />} onClick={onRestore}>还原</Button><Button size="small" danger icon={<DeleteOutlined />} onClick={onPurge}>永久删除</Button></> : entry.capabilities.archive && <Button size="small" danger type="text" icon={<DeleteOutlined />} onClick={onArchive}>移到回收站</Button>}
        </div>
      </header>
      <Tabs
        className={styles.detailTabs}
        defaultActiveKey="preview"
        items={[
          { key: "preview", label: "预览", children: visualPreview() },
          { key: "details", label: "详情", children: <Descriptions size="small" column={1} items={[
            { key: "path", label: "路径", children: entry.path || "工作区根目录" },
            { key: "size", label: "大小", children: formattedSize(entry.size_bytes) },
            { key: "kind", label: "类型", children: entry.kind === "file" ? getContentType(entry.name) : entry.kind },
            { key: "changed", label: "修改时间", children: entry.modified_at || "未知" },
          ]} /> },
        ]}
      />
      {editing && <div className={styles.editorFooter}><Button type="link" icon={<ReloadOutlined />} onClick={abandonEdit}>放弃修改</Button></div>}
    </section>
  );
});

export default FileDetail;
