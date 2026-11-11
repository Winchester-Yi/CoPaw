import { useEffect, useRef } from "react";
import { useIframeStore } from "../../../stores/iframeStore";
import { operationsIdentity, useWealthOperations } from "../operations";
import { useWealthStore } from "../store";
import { distributionContext } from "./api";
import {
  preloadInstitutions,
  resetInstitutionCache,
  useInstitutionCache,
} from "./institutionCache";

export function institutionIdentity() {
  const { enabled, branchId } = useWealthOperations.getState();
  const { contextVersion } = useWealthStore.getState();
  return JSON.stringify([
    operationsIdentity(),
    enabled,
    branchId,
    contextVersion,
  ]);
}

export function useInstitutionIdentity() {
  const identity = useIframeStore(operationsIdentity);
  const enabled = useWealthOperations((s) => s.enabled);
  const branch = useWealthOperations((s) => s.branchId);
  const version = useWealthStore((s) => s.contextVersion);
  return JSON.stringify([identity, enabled, branch, version]);
}

export function loadInstitutions(key: string) {
  try {
    return preloadInstitutions(key, distributionContext());
  } catch (error) {
    if (useInstitutionCache.getState().key !== key) resetInstitutionCache(key);
    useInstitutionCache.setState({
      status: "error",
      error: error instanceof Error ? error.message : "机构查询失败",
    });
    return Promise.resolve();
  }
}

export function useInstitutionPreload(enabled: boolean) {
  const key = useInstitutionIdentity();
  const previous = useRef(key);
  useEffect(() => {
    if (previous.current !== key) {
      useWealthStore.getState().resetPlanForm();
      previous.current = key;
    }
    if (useInstitutionCache.getState().key !== key) resetInstitutionCache(key);
    if (enabled) void loadInstitutions(key);
    return () => resetInstitutionCache();
  }, [enabled, key]);
}
