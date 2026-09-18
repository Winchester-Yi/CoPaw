import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Switch,
  Tooltip,
  message,
} from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  DeleteOutlined,
  EditOutlined,
  HolderOutlined,
  PlusOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import {
  marketApi,
  ORPHANED_CATEGORY_ID,
  UNCATEGORIZED_CATEGORY_ID,
  type Category,
} from "../../api/modules/market";

interface CategoryManagementModalProps {
  open: boolean;
  sourceId: string;
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}

type EditingId = number | "new" | null;

function getErrorMessage(error: unknown): string {
  if (!(error instanceof Error)) return "操作失败";
  try {
    const payload = JSON.parse(error.message);
    const detail = payload?.detail;
    if (typeof detail === "string") return detail;
    if (detail?.message) {
      return `${detail.message}${
        detail.skill_count ? `（当前 ${detail.skill_count} 个技能）` : ""
      }`;
    }
  } catch {
    // Keep the original message for non-JSON API errors.
  }
  return error.message || "操作失败";
}

const categoryGridColumns = "32px minmax(0, 1fr) 90px 130px 104px";

export function CategoryManagementModal({
  open,
  sourceId,
  onClose,
  onChanged,
}: CategoryManagementModalProps) {
  const [categories, setCategories] = useState<Category[]>([]);
  const [loading, setLoading] = useState(false);
  const [savingOrder, setSavingOrder] = useState(false);
  const [draggedId, setDraggedId] = useState<number | null>(null);
  const [editingId, setEditingId] = useState<EditingId>(null);
  const [draftName, setDraftName] = useState("");
  const [draftBranchVisible, setDraftBranchVisible] = useState(true);
  const [savingId, setSavingId] = useState<EditingId>(null);
  const [uncategorizedSkillCount, setUncategorizedSkillCount] = useState(0);
  const [orphanedSkillCount, setOrphanedSkillCount] = useState(0);

  const loadCategories = useCallback(async () => {
    setLoading(true);
    try {
      const [categoryData, browseData] = await Promise.all([
        marketApi.listCategories(sourceId),
        marketApi.browseMarket(sourceId, "skill"),
      ]);
      setCategories(categoryData);
      setUncategorizedSkillCount(
        browseData.categories.find(
          (category) => category.id === UNCATEGORIZED_CATEGORY_ID,
        )?.count ?? 0,
      );
      setOrphanedSkillCount(
        browseData.categories.find(
          (category) => category.id === ORPHANED_CATEGORY_ID,
        )?.count ?? 0,
      );
    } catch (error) {
      message.error(getErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }, [sourceId]);

  useEffect(() => {
    if (open) void loadCategories();
  }, [loadCategories, open]);

  const startEdit = (category: Category) => {
    setEditingId(category.id);
    setDraftName(category.name);
    setDraftBranchVisible(category.branch_visible);
  };

  const startAdd = () => {
    setEditingId("new");
    setDraftName("");
    setDraftBranchVisible(true);
  };

  const cancelEdit = () => {
    setEditingId(null);
    setDraftName("");
    setDraftBranchVisible(true);
  };

  const saveEdit = async () => {
    const name = draftName.trim();
    if (!name) {
      message.warning("请输入分类名称");
      return;
    }
    if (editingId === null) return;

    setSavingId(editingId);
    try {
      if (editingId === "new") {
        await marketApi.createCategory(sourceId, name);
        await loadCategories();
        message.success("分类已新增");
      } else {
        const category = categories.find((item) => item.id === editingId);
        if (!category) return;
        const updated = await marketApi.updateCategory(sourceId, category.id, {
          name,
          branch_visible: draftBranchVisible,
        });
        setCategories((current) =>
          current.map((item) => (item.id === updated.id ? updated : item)),
        );
        message.success("分类已更新");
      }
      cancelEdit();
      await onChanged();
    } catch (error) {
      message.error(getErrorMessage(error));
    } finally {
      setSavingId(null);
    }
  };

  const deleteCategory = async (category: Category) => {
    try {
      await marketApi.deleteCategory(sourceId, category.id);
      setCategories((current) =>
        current.filter((item) => item.id !== category.id),
      );
      await onChanged();
      message.success("分类已删除");
    } catch (error) {
      message.error(getErrorMessage(error));
    }
  };

  const handleDrop = (targetId: number) => {
    if (editingId !== null || draggedId === null || draggedId === targetId)
      return;
    setCategories((current) => {
      const next = [...current];
      const from = next.findIndex((item) => item.id === draggedId);
      const to = next.findIndex((item) => item.id === targetId);
      if (from < 0 || to < 0) return current;
      const [item] = next.splice(from, 1);
      next.splice(to, 0, item);
      return next;
    });
    setDraggedId(null);
  };

  const saveOrder = async () => {
    setSavingOrder(true);
    try {
      await marketApi.reorderCategories(
        sourceId,
        categories.map((category) => category.id),
      );
      await onChanged();
      message.success("排序已保存");
    } catch (error) {
      message.error(getErrorMessage(error));
    } finally {
      setSavingOrder(false);
    }
  };

  const renderVisibility = (
    checked: boolean,
    editable: boolean,
    label: string,
  ) => (
    <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
      <Switch
        size="small"
        checked={checked}
        disabled={!editable}
        aria-label={label}
        onChange={editable ? setDraftBranchVisible : undefined}
      />
      <span style={{ color: "#4b5563", fontSize: 13, whiteSpace: "nowrap" }}>
        {checked ? "分行可见" : "仅总行"}
      </span>
    </div>
  );

  const renderEditActions = (label: string, id: EditingId) => (
    <div style={{ display: "flex", gap: 4 }}>
      <Button
        type="text"
        icon={<CheckOutlined />}
        aria-label={`保存${label}`}
        loading={savingId === id}
        onClick={() => void saveEdit()}
      />
      <Button
        type="text"
        icon={<CloseOutlined />}
        aria-label={`取消${label}`}
        disabled={savingId === id}
        onClick={cancelEdit}
      />
    </div>
  );

  return (
    <Modal
      title="分类管理"
      open={open}
      onCancel={onClose}
      footer={null}
      width={800}
      destroyOnHidden
    >
      <div style={{ color: "#4b5563", fontSize: 13, marginBottom: 16 }}>
        仅技能使用此分类体系。分类关闭分行可见后，分行用户将不会看到该分类及其技能；总行仍可见。
      </div>
      {uncategorizedSkillCount > 0 && (
        <Alert
          type="info"
          showIcon
          message={`未分类技能：${uncategorizedSkillCount} 个`}
          description="历史未设置分类的技能会归入“未分类”。该分组仅用于筛选，不支持改名、删除或排序。"
          style={{ marginBottom: 16 }}
        />
      )}
      {orphanedSkillCount > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`待整理技能：${orphanedSkillCount} 个`}
          description="引用已不存在分类的历史技能会归入“待整理分类”，请通过技能编辑逐个设置真实分类。该分组仅用于定位，不支持改名、删除或排序。"
          style={{ marginBottom: 16 }}
        />
      )}
      <Alert
        type="info"
        showIcon
        message="删除分类前请先将其中技能调整到其他分类。非空分类不能删除。"
        style={{ marginBottom: 16 }}
      />
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          marginBottom: 12,
        }}
      >
        <Button
          type="primary"
          icon={<PlusOutlined />}
          disabled={editingId !== null}
          onClick={startAdd}
        >
          新增分类
        </Button>
      </div>

      <div
        style={{
          border: "1px solid #e5e7eb",
          borderRadius: 8,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            display: "grid",
            gridTemplateColumns: categoryGridColumns,
            gap: 12,
            padding: "10px 16px",
            background: "#f7f9fc",
            color: "#8a94a6",
            fontSize: 12,
          }}
        >
          <span />
          <span>分类名称</span>
          <span>技能数量</span>
          <span>分行可见</span>
          <span>操作</span>
        </div>

        {editingId === "new" && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: categoryGridColumns,
              gap: 12,
              alignItems: "center",
              padding: "12px 16px",
              borderTop: "1px solid #eef0f3",
              background: "#eef4ff",
            }}
          >
            <span />
            <Input
              autoFocus
              aria-label="新分类名称"
              placeholder="输入分类名称"
              value={draftName}
              maxLength={128}
              onChange={(event) => setDraftName(event.target.value)}
              onPressEnter={() => void saveEdit()}
            />
            <span style={{ color: "#8a94a6", fontSize: 13 }}>新分类</span>
            {renderVisibility(draftBranchVisible, true, "新分类分行可见")}
            {renderEditActions("新分类", "new")}
          </div>
        )}

        {loading ? (
          <div style={{ padding: 36, textAlign: "center", color: "#8a94a6" }}>
            加载中...
          </div>
        ) : categories.length === 0 ? (
          editingId === "new" ? null : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="暂无分类"
              style={{ padding: 24 }}
            />
          )
        ) : (
          categories.map((category) => {
            const isEditing = editingId === category.id;
            return (
              <div
                key={category.id}
                draggable={editingId === null}
                onDragStart={() => {
                  if (editingId === null) setDraggedId(category.id);
                }}
                onDragOver={(event) => event.preventDefault()}
                onDrop={() => handleDrop(category.id)}
                style={{
                  display: "grid",
                  gridTemplateColumns: categoryGridColumns,
                  gap: 12,
                  alignItems: "center",
                  padding: "12px 16px",
                  borderTop: "1px solid #eef0f3",
                  background:
                    isEditing || draggedId === category.id ? "#eef4ff" : "#fff",
                }}
              >
                <Tooltip title="拖动调整顺序">
                  <HolderOutlined
                    aria-label={`拖动${category.name}`}
                    style={{
                      color: "#8a94a6",
                      cursor: editingId === null ? "grab" : "default",
                    }}
                  />
                </Tooltip>
                {isEditing ? (
                  <>
                    <Input
                      autoFocus
                      aria-label={`分类名称 ${category.name}`}
                      value={draftName}
                      maxLength={128}
                      onChange={(event) => setDraftName(event.target.value)}
                      onPressEnter={() => void saveEdit()}
                    />
                    <span style={{ color: "#4b5563", fontSize: 13 }}>
                      {category.skill_count ?? 0} 个技能
                    </span>
                    {renderVisibility(
                      draftBranchVisible,
                      true,
                      `设置${category.name}分行可见性`,
                    )}
                    {renderEditActions(category.name, category.id)}
                  </>
                ) : (
                  <>
                    <Tooltip title={category.name}>
                      <span
                        style={{
                          minWidth: 0,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                          color: "#111827",
                        }}
                      >
                        {category.name}
                      </span>
                    </Tooltip>
                    <span style={{ color: "#4b5563", fontSize: 13 }}>
                      {category.skill_count ?? 0} 个技能
                    </span>
                    {renderVisibility(
                      category.branch_visible,
                      false,
                      `查看${category.name}分行可见性`,
                    )}
                    <div style={{ display: "flex", gap: 4 }}>
                      <Button
                        type="text"
                        icon={<EditOutlined />}
                        aria-label={`编辑${category.name}`}
                        disabled={editingId !== null}
                        onClick={() => startEdit(category)}
                      />
                      <Popconfirm
                        title={`删除分类“${category.name}”？`}
                        description={
                          category.skill_count > 0
                            ? `当前有 ${category.skill_count} 个技能，删除前请先调整技能分类。`
                            : "此操作不可恢复。"
                        }
                        okText="删除"
                        cancelText="取消"
                        okButtonProps={{
                          danger: true,
                          disabled: category.skill_count > 0,
                        }}
                        onConfirm={() => void deleteCategory(category)}
                      >
                        <Button
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          aria-label={`删除${category.name}`}
                          disabled={editingId !== null}
                        />
                      </Popconfirm>
                    </div>
                  </>
                )}
              </div>
            );
          })
        )}
      </div>
      <div
        style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}
      >
        <Button
          type="primary"
          icon={<SaveOutlined />}
          loading={savingOrder}
          disabled={categories.length < 2 || editingId !== null}
          onClick={() => void saveOrder()}
        >
          保存排序
        </Button>
      </div>
    </Modal>
  );
}
