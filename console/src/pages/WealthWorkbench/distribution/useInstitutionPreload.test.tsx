import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useIframeStore } from "../../../stores/iframeStore";
import { operationsIdentity, useWealthOperations } from "../operations";
import { useWealthStore } from "../store";
import { queryInstitutionTree, type Institution } from "./api";
import { resetInstitutionCache, useInstitutionCache } from "./institutionCache";
import {
  institutionIdentity,
  useInstitutionIdentity,
  useInstitutionPreload,
} from "./useInstitutionPreload";

vi.mock("../../../constants/bbk", () => ({
  BBK_ID_MAP: [
    { value: "110", label: "A分行" },
    { value: "120", label: "B分行" },
  ],
  BBK_ID_TO_NAME_MAP: { "110": "A分行", "120": "B分行" },
}));
vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  fetchBootstrap: vi.fn(async () => ({
    plans: [],
    draft: { name: "", items: [] },
    savedAt: "",
  })),
}));
vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  queryInstitutionTree: vi.fn(),
}));
const queryTree = vi.mocked(queryInstitutionTree);
const originalEnv = window.__env__;
const root = (code: string, name = code): Institution => ({
  mOrgCode: code,
  mOrgName: name,
  mOrgLvl: 30,
  children: [],
});
function mount() {
  return renderHook(() => {
    // The real Workbench also subscribes to contextVersion to remount its page.
    useWealthStore((s) => s.contextVersion);
    useInstitutionPreload(true);
    return useInstitutionIdentity();
  });
}
async function switchBackInOneBatch() {
  await act(async () => {
    const toB = useWealthStore.getState().setOperationsMode(true, "120");
    const toA = useWealthStore.getState().setOperationsMode(true, "110");
    await Promise.all([toB, toA]);
  });
}
beforeEach(() => {
  resetInstitutionCache();
  queryTree.mockReset();
  queryTree.mockImplementation(async (context) =>
    root(context.bbk, context.name),
  );
  window.__env__ = { operationsUsersByBranch: { "110": ["qa"] } };
  useIframeStore.getState().setContext({
    userId: "qa",
    bbk: "110",
    orgCode: "110",
    orgLvl: "30",
    sysId: "retail",
    positionId: "RB0301",
  });
  useWealthOperations.setState({
    enabled: true,
    branchId: "110",
    identity: operationsIdentity(),
  });
  useWealthStore.setState(
    {
      ...useWealthStore.getInitialState(),
      initialized: true,
      accountId: "middle",
    },
    true,
  );
});
afterEach(() => {
  cleanup();
  resetInstitutionCache();
  window.__env__ = originalEnv;
});

describe("机构缓存的上下文代次", () => {
  it("同批 A→B→A 也刷新原 A 缓存，命令式和 Hook 的标识保持一致", async () => {
    const { result } = mount();
    await waitFor(() =>
      expect(useInstitutionCache.getState().status).toBe("ready"),
    );
    const oldKey = result.current;
    await switchBackInOneBatch();
    await waitFor(() => expect(queryTree).toHaveBeenCalledTimes(2));
    expect(result.current).not.toBe(oldKey);
    expect(result.current).toBe(institutionIdentity());
    expect(useInstitutionCache.getState().key).toBe(result.current);
    expect(useWealthStore.getState().contextVersion).toBe(2);
  });
  it("同批往返使旧 A 在途请求失效，迟到响应不覆盖新 A", async () => {
    let resolve!: (value: Institution) => void;
    queryTree.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    queryTree.mockResolvedValueOnce(root("110", "新 A"));
    mount();
    await waitFor(() => expect(queryTree).toHaveBeenCalledTimes(1));
    const oldSignal = queryTree.mock.calls[0][1]!;
    await switchBackInOneBatch();
    await waitFor(() =>
      expect(useInstitutionCache.getState().root?.mOrgName).toBe("新 A"),
    );
    expect(oldSignal.aborted).toBe(true);
    await act(async () => {
      resolve(root("110", "旧 A"));
    });
    expect(useInstitutionCache.getState().root?.mOrgName).toBe("新 A");
  });
  it("同分行新建计划和普通重渲染仍复用机构缓存", async () => {
    const { rerender, result } = mount();
    await waitFor(() =>
      expect(useInstitutionCache.getState().status).toBe("ready"),
    );
    const oldKey = result.current;
    act(() => useWealthStore.getState().resetPlanForm());
    rerender();
    expect(result.current).toBe(oldKey);
    expect(queryTree).toHaveBeenCalledTimes(1);
    expect(useInstitutionCache.getState().status).toBe("ready");
  });
  it("普通 A→B→A 切换依然分别重新加载", async () => {
    mount();
    await waitFor(() =>
      expect(useInstitutionCache.getState().root?.mOrgCode).toBe("110"),
    );
    await act(async () => {
      await useWealthStore.getState().setOperationsMode(true, "120");
    });
    await waitFor(() =>
      expect(useInstitutionCache.getState().root?.mOrgCode).toBe("120"),
    );
    await act(async () => {
      await useWealthStore.getState().setOperationsMode(true, "110");
    });
    await waitFor(() =>
      expect(useInstitutionCache.getState().root?.mOrgCode).toBe("110"),
    );
    expect(queryTree).toHaveBeenCalledTimes(3);
  });
});
