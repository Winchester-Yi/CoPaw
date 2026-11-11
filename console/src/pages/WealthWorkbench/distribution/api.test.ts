import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { request } from "../../../api/request";
import { useIframeStore } from "../../../stores/iframeStore";
import {
  distributionContext,
  importExcelPeople,
  queryInstitutionPeople,
  queryInstitutionTree,
} from "./api";
import {
  preloadInstitutions,
  resetInstitutionCache,
  useInstitutionCache,
} from "./institutionCache";
vi.mock("../../../api/request", () => ({ request: vi.fn() }));
const mock = vi.mocked(request);
const context = { bbk: "110", name: "北京分行", level: 30, sysId: "retail" };
const root = {
  mOrgCode: "110",
  mOrgName: "北京分行",
  mOrgLvl: 30,
  children: [],
};
beforeEach(() => {
  mock.mockReset();
  resetInstitutionCache();
});
afterEach(resetInstitutionCache);
it("机构范围展开为本级及全部后代，父子重叠和批量机构不重复传参", async () => {
  const leaf = { ...root, mOrgCode: "a1", mOrgLvl: 80 };
  const branch = { ...root, mOrgCode: "a", mOrgLvl: 70, children: [leaf] };
  const other = { ...root, mOrgCode: "b", mOrgLvl: 70 };
  const tree = { ...root, children: [branch, other] };
  mock.mockResolvedValue({ code: "200", data: [] });
  await queryInstitutionPeople(context, [tree, branch, leaf]);
  expect(JSON.parse(mock.mock.calls[0][1]!.body as string).orgList).toEqual([
    { bbkOrgId: "110", mmOrgCode: "110", mmOrgLvl: 30 },
    { bbkOrgId: "110", mmOrgCode: "a", mmOrgLvl: 70 },
    { bbkOrgId: "110", mmOrgCode: "a1", mmOrgLvl: 80 },
    { bbkOrgId: "110", mmOrgCode: "b", mmOrgLvl: 70 },
  ]);
  await queryInstitutionPeople(context, [branch, other, leaf]);
  expect(
    JSON.parse(mock.mock.calls[1][1]!.body as string).orgList.map(
      (item: { mmOrgCode: string }) => item.mmOrgCode,
    ),
  ).toEqual(["a", "a1", "b"]);
});
it("机构树使用新接口、映射名称、规范级别并保留旧过滤名称的机构", async () => {
  useIframeStore
    .getState()
    .setContext({ bbk: "110", orgLvl: "70", sysId: "retail" });
  expect(distributionContext()).toEqual({ ...context, level: 70 });
  mock.mockResolvedValue({
    code: "200",
    data: {
      ...root,
      mOrgLvl: "30",
      children: [
        {
          mOrgCode: "121516",
          mOrgName: "私人银行",
          mOrgLvl: "80",
          leaf: false,
        },
      ],
    },
  });
  const tree = await queryInstitutionTree(context);
  expect(tree.children[0]).toEqual({
    mOrgCode: "121516",
    mOrgName: "私人银行",
    mOrgLvl: 80,
    children: [],
  });
  expect(mock).toHaveBeenCalledWith(
    "/zt/wealth/wmadm/bbkTree",
    expect.objectContaining({
      body: JSON.stringify({
        mmOrgCode: "110",
        mmOrgName: "北京分行",
        mmOrgLvl: 30,
        sysId: "retail",
      }),
    }),
  );
});
it("人员批量请求保留工号前导零及实际所属机构", async () => {
  mock.mockResolvedValue({
    code: "200",
    data: [
      {
        sapID: "00012",
        userName: "张三",
        branchID: "110",
        subBranchID: "11001",
        subBranchName: "下级支行",
      },
    ],
  });
  expect(await queryInstitutionPeople(context, [root])).toEqual([
    {
      sapId: "00012",
      name: "张三",
      orgName: "下级支行",
      bbkId: "110",
      bbkOrgId: "11001",
    },
  ]);
  expect(mock).toHaveBeenCalledWith(
    "/zt/wealth/wmadm/orgSaps",
    expect.objectContaining({
      body: JSON.stringify({
        orgList: [{ bbkOrgId: "110", mmOrgCode: "110", mmOrgLvl: 30 }],
      }),
    }),
  );
  mock.mockResolvedValue({ code: "500", message: "整批失败", data: [] });
  await expect(queryInstitutionPeople(context, [root])).rejects.toThrow(
    "整批失败",
  );
});
it("预加载复用在途与成功缓存，切分行时旧响应不得回写，切回强制请求", async () => {
  let resolve!: (data: unknown) => void;
  mock.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const first = preloadInstitutions("A", context);
  expect(preloadInstitutions("A", context)).toBe(first);
  resetInstitutionCache("B");
  mock.mockResolvedValue({ code: "200", data: { ...root, mOrgCode: "120" } });
  await preloadInstitutions("B", { ...context, bbk: "120" });
  resolve({ code: "200", data: root });
  await first;
  expect(useInstitutionCache.getState().root?.mOrgCode).toBe("120");
  await preloadInstitutions("B", { ...context, bbk: "120" });
  expect(mock).toHaveBeenCalledTimes(2);
  mock.mockResolvedValue({ code: "200", data: root });
  await preloadInstitutions("A", context);
  expect(mock).toHaveBeenCalledTimes(3);
});
it("错误分行根及畸形人员响应不能当作成功空数据", async () => {
  mock.mockResolvedValue({ code: "200", data: { ...root, mOrgCode: "120" } });
  await expect(queryInstitutionTree(context)).rejects.toThrow("不一致");
  mock.mockResolvedValue({ code: "200", data: {} });
  await expect(queryInstitutionPeople(context, [root])).rejects.toThrow(
    "格式异常",
  );
});

it("Excel 导入上传原始文件，按服务端结果回填人员及机构编码", async () => {
  const file = new File(["excel fixture"], "分发名单.xlsx");
  mock.mockResolvedValue({
    code: "200",
    data: {
      users: [
        {
          sapID: "00012",
          userName: "李四",
          branchID: "120",
          subBranchID: "12001",
          subBranchName: "异地支行",
        },
      ],
      duplicates: 1,
    },
  });
  await expect(
    importExcelPeople(file, new AbortController().signal),
  ).resolves.toEqual({
    people: [
      {
        sapId: "00012",
        name: "李四",
        orgName: "异地支行",
        bbkId: "120",
        bbkOrgId: "12001",
      },
    ],
    errors: [],
    duplicates: 1,
  });
  expect(mock).toHaveBeenCalledOnce();
  expect(mock).toHaveBeenCalledWith(
    "/agent/workspace/users/import-excel",
    expect.objectContaining({ method: "POST", body: expect.any(FormData) }),
  );
  const body = mock.mock.calls[0][1]?.body as FormData;
  expect(body.get("file")).toBe(file);
});

it("Excel 导入返回逐行错误，超限文件不发送请求", async () => {
  const signal = new AbortController().signal;
  const oversized = new File(
    [new Uint8Array(5 * 1024 * 1024 + 1)],
    "超限.xlsx",
  );
  await expect(importExcelPeople(oversized, signal)).rejects.toThrow(
    "不超过 5 MB",
  );
  expect(mock).not.toHaveBeenCalled();
  mock.mockResolvedValue({
    code: "200",
    data: { users: [], errors: ["第 2 行：客户经理不存在"] },
  });
  await expect(
    importExcelPeople(new File(["fixture"], "名单.xlsx"), signal),
  ).resolves.toEqual({
    people: [],
    errors: ["第 2 行：客户经理不存在"],
    duplicates: 0,
  });
});
