import { create } from "zustand";
import {
  queryInstitutionTree,
  type DistributionContext,
  type Institution,
} from "./api";

interface TreeState {
  key: string;
  root: Institution | null;
  status: "idle" | "loading" | "ready" | "error";
  error: string;
}
export const useInstitutionCache = create<TreeState>(() => ({
  key: "",
  root: null,
  status: "idle",
  error: "",
}));
let active: {
  key: string;
  controller: AbortController;
  promise: Promise<void>;
} | null = null;

export function resetInstitutionCache(key = "") {
  active?.controller.abort();
  active = null;
  useInstitutionCache.setState({ key, root: null, status: "idle", error: "" });
}

export function preloadInstitutions(
  key: string,
  context: DistributionContext,
): Promise<void> {
  const state = useInstitutionCache.getState();
  if (state.key !== key) resetInstitutionCache(key);
  if (active?.key === key) return active.promise;
  if (useInstitutionCache.getState().status === "ready")
    return Promise.resolve();
  const controller = new AbortController();
  useInstitutionCache.setState({ status: "loading", error: "" });
  const promise = queryInstitutionTree(context, controller.signal)
    .then((root) => {
      if (
        !controller.signal.aborted &&
        useInstitutionCache.getState().key === key
      )
        useInstitutionCache.setState({ root, status: "ready" });
    })
    .catch((error: unknown) => {
      if (
        !controller.signal.aborted &&
        useInstitutionCache.getState().key === key
      )
        useInstitutionCache.setState({
          status: "error",
          error:
            error instanceof Error ? error.message : "机构查询失败，请重试",
        });
    })
    .finally(() => {
      if (active?.controller === controller) active = null;
    });
  active = { key, controller, promise };
  return promise;
}
