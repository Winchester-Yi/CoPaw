import { MarketSkills } from "./MarketSkills";
import { useIframeStore } from "../../stores/iframeStore";
import { getUserId } from "../../utils/identity";
import { DEFAULT_SOURCE_ID } from "../../constants/identity";

export default function MarketPage() {
  const sourceId = useIframeStore((state) => state.source) || DEFAULT_SOURCE_ID;
  const userId = getUserId();
  const manager = useIframeStore((state) => state.manager);
  const bbkId = useIframeStore((state) => state.bbk) || "100";
  const canManage = manager || userId === "default";

  return (
    <MarketSkills
      sourceId={sourceId}
      isManager={canManage}
      bbkId={bbkId}
    />
  );
}
