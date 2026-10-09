import { useState, useCallback } from "react";
import {
  marketApi,
  Category,
  MarketSkill,
  MarketSkillDetail,
  MarketBrowseResponse,
  ORPHANED_CATEGORY_ID,
  UNCATEGORIZED_CATEGORY_ID,
} from "../../api/modules/market";

export function useMarket(sourceId: string) {
  const [categories, setCategories] = useState<Category[]>([]);
  const [skills, setSkills] = useState<MarketSkill[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedCategory, setSelectedCategory] = useState<number | null>(null);
  const [selectedBbkId, setSelectedBbkId] = useState<string | null>(null);
  const [selectedSkill, setSelectedSkill] = useState<MarketSkillDetail | null>(
    null,
  );
  const [detailDrawerOpen, setDetailDrawerOpen] = useState(false);
  const [publishModalOpen, setPublishModalOpen] = useState(false);
  const [browse, setBrowse] = useState<MarketBrowseResponse | null>(null);

  const refreshCategories = useCallback(async () => {
    try {
      const data = await marketApi.listCategories(sourceId);
      setCategories(data);
    } catch (err) {
      console.error("Failed to load categories:", err);
    }
  }, [sourceId]);

  const refreshSkills = useCallback(async () => {
    setLoading(true);
    try {
      const data = await marketApi.browseMarket(sourceId, "skill", {
        categoryId:
          selectedCategory === UNCATEGORIZED_CATEGORY_ID
            ? null
            : selectedCategory,
        bbkId: selectedBbkId,
        uncategorized: selectedCategory === UNCATEGORIZED_CATEGORY_ID,
        orphaned: selectedCategory === ORPHANED_CATEGORY_ID,
      });
      setBrowse(data);
      setSkills(data.items as MarketSkill[]);
    } catch (err) {
      console.error("Failed to load skills:", err);
    } finally {
      setLoading(false);
    }
  }, [sourceId, selectedCategory, selectedBbkId]);

  // 刷新当前选中技能的详情
  const refreshSelectedSkill = useCallback(async () => {
    if (!selectedSkill) return;
    try {
      const detail = await marketApi.getSkillDetail(
        sourceId,
        selectedSkill.item_id,
      );
      if (detail) {
        setSelectedSkill(detail);
      }
    } catch (err) {
      console.error("Failed to refresh skill detail:", err);
    }
  }, [sourceId, selectedSkill]);

  // 刷新技能列表和详情
  const refreshSkillsAndDetail = useCallback(async () => {
    await refreshSkills();
    await refreshSelectedSkill();
  }, [refreshSkills, refreshSelectedSkill]);

  const openSkillDetail = useCallback(
    async (itemId: string) => {
      try {
        const detail = await marketApi.getSkillDetail(sourceId, itemId);
        if (detail) {
          setSelectedSkill(detail);
          setDetailDrawerOpen(true);
        }
      } catch (err) {
        console.error("Failed to load skill detail:", err);
      }
    },
    [sourceId],
  );

  return {
    categories,
    skills,
    loading,
    browse,
    selectedCategory,
    setSelectedCategory,
    selectedBbkId,
    setSelectedBbkId,
    selectedSkill,
    detailDrawerOpen,
    setDetailDrawerOpen,
    publishModalOpen,
    setPublishModalOpen,
    refreshCategories,
    refreshSkills,
    refreshSelectedSkill,
    refreshSkillsAndDetail,
    openSkillDetail,
  };
}
