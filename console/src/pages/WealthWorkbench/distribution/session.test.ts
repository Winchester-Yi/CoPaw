import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DistributionSession } from "./session";
import type { Institution } from "./api";
import type { DistributeTarget } from "../types";
const node = (code: string, children: Institution[] = []): Institution => ({
  mOrgCode: code,
  mOrgName: code,
  mOrgLvl: 70,
  children,
});
const root = node("root", [node("a", [node("a1")]), node("b"), node("c")]);
const user = (id: string, org: string): DistributeTarget => ({
  sapId: id,
  name: id,
  bbkOrgId: org,
  orgName: org,
});
const users = [
  user("01", "a"),
  user("02", "a1"),
  user("03", "b"),
  user("04", "c"),
];
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
let sessions: DistributionSession[] = [];
function setup(
  query = vi.fn(async (nodes: Institution[], signal: AbortSignal) => {
    void signal;
    return users.filter((user) =>
      nodes.some(
        (node) =>
          node.mOrgCode === "root" ||
          node.mOrgCode === user.bbkOrgId ||
          (node.mOrgCode === "a" && user.bbkOrgId === "a1"),
      ),
    );
  }),
) {
  const session = new DistributionSession(root, [], query);
  sessions.push(session);
  return { session, query };
}
beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  sessions.forEach((s) => s.dispose());
  sessions = [];
  vi.useRealTimers();
});

describe("分发人员批量队列", () => {
  it("父级全选联动非空和空子级，取消空子级使父级半选，补全父级恢复联动", async () => {
    const { session } = setup(vi.fn().mockResolvedValue([users[0], users[2]]));
    session.toggleInstitution("root");
    expect(session.isPendingSelection("a1")).toBe(true);
    await vi.advanceTimersByTimeAsync(500);
    for (const code of ["root", "a", "a1", "b", "c"])
      expect(session.isInstitutionSelected(code)).toBe(true);
    session.toggleInstitution("a1");
    expect(session.isInstitutionSelected("a1")).toBe(false);
    expect(session.isInstitutionSelected("a")).toBe(false);
    expect(session.isInstitutionSelected("root")).toBe(false);
    expect(session.isInstitutionSelected("b")).toBe(true);
    session.toggleInstitution("root");
    expect(session.isInstitutionSelected("a1")).toBe(true);
    session.toggleInstitution("root");
    expect(session.getSnapshot().emptyInstitutions).toEqual([]);
    expect(session.getSnapshot().selected).toEqual([]);
  });
  it("全空机构树父子联动，取消子级不影响兄弟，加载时排除子级后响应不补回", async () => {
    const { session } = setup(vi.fn().mockResolvedValue([]));
    session.toggleInstitution("root");
    session.toggleInstitution("a");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.isInstitutionSelected("a")).toBe(false);
    expect(session.isInstitutionSelected("a1")).toBe(false);
    expect(session.isInstitutionSelected("b")).toBe(true);
    expect(session.isInstitutionSelected("root")).toBe(false);
    session.toggleInstitution("root");
    expect(session.isInstitutionSelected("a1")).toBe(true);
    expect(session.isInstitutionSelected("root")).toBe(true);
    session.toggleInstitution("a1");
    expect(session.isInstitutionSelected("a")).toBe(false);
    expect(session.isInstitutionSelected("root")).toBe(false);
    expect(session.isInstitutionSelected("b")).toBe(true);
  });
  it("仅查看空机构不选中，明确勾选后保留零人机构，支持取消和缓存复用", async () => {
    const query = vi.fn().mockResolvedValue([]);
    const { session } = setup(query);
    session.view("a");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.isInstitutionSelected("a")).toBe(false);
    session.toggleInstitution("a");
    expect(session.isInstitutionSelected("a")).toBe(true);
    expect(session.getSnapshot().emptyInstitutions).toEqual(["a", "a1"]);
    expect(session.getSnapshot().anchors).toEqual(["a"]);
    expect(session.getSnapshot().selected).toEqual([]);
    expect(session.getSnapshot().blocked).toBe(false);
    session.toggleInstitution("a");
    expect(session.isInstitutionSelected("a")).toBe(false);
    expect(session.getSnapshot().emptyInstitutions).toEqual([]);
    expect(session.getSnapshot().anchors).toEqual([]);
    expect(query).toHaveBeenCalledTimes(1);
  });
  it("批量全选空机构后可单独移除，清空全部也清理零人机构", async () => {
    const { session } = setup(vi.fn().mockResolvedValue([]));
    session.toggleInstitution("a");
    session.toggleInstitution("b");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.getSnapshot().emptyInstitutions).toEqual(["a", "a1", "b"]);
    session.removeInstitution("a");
    expect(session.getSnapshot().emptyInstitutions).toEqual(["b"]);
    expect(session.isInstitutionSelected("b")).toBe(true);
    session.clear();
    expect(session.getSnapshot().emptyInstitutions).toEqual([]);
    expect(session.getSnapshot().anchors).toEqual([]);
  });
  it("空机构出现新人员后不隐式选人，取消待选的空响应也不保留勾选", async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce(users);
    const { session } = setup(query);
    session.toggleInstitution("a");
    await vi.advanceTimersByTimeAsync(500);
    session.view("root");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.isInstitutionSelected("a")).toBe(false);
    expect(session.getSnapshot().emptyInstitutions).toEqual([]);
    expect(session.getSnapshot().selected).toEqual([]);
    const pending = deferred<DistributeTarget[]>();
    const { session: cancelled } = setup(
      vi.fn().mockReturnValue(pending.promise),
    );
    cancelled.toggleInstitution("b");
    await vi.advanceTimersByTimeAsync(500);
    cancelled.toggleInstitution("b");
    pending.resolve([]);
    await vi.advanceTimersByTimeAsync(0);
    expect(cancelled.getSnapshot().emptyInstitutions).toEqual([]);
    expect(cancelled.isInstitutionSelected("b")).toBe(false);
  });
  it("勾选切换视图时移除尚未发送的旧纯查看请求", async () => {
    const { session, query } = setup();
    session.view("a");
    session.toggleInstitution("b");
    await vi.advanceTimersByTimeAsync(500);
    expect(query).toHaveBeenCalledTimes(1);
    expect(query.mock.calls[0][0].map((n: Institution) => n.mOrgCode)).toEqual([
      "b",
    ]);
    expect(session.getSnapshot().people).toEqual([users[2]]);
    expect(session.getSnapshot().selected).toEqual([users[2]]);
  });
  it("500ms 固定窗口，不因第二次点击延后，合并多机构请求", async () => {
    const { session, query } = setup();
    session.toggleInstitution("a");
    await vi.advanceTimersByTimeAsync(300);
    session.toggleInstitution("b");
    expect(session.getSnapshot().current).toBe("b");
    await vi.advanceTimersByTimeAsync(199);
    expect(query).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(query).toHaveBeenCalledTimes(1);
    expect(query.mock.calls[0][0].map((n: Institution) => n.mOrgCode)).toEqual([
      "a",
      "b",
    ]);
    expect(session.getSnapshot().selected.map((u) => u.sapId)).toEqual([
      "01",
      "02",
      "03",
    ]);
  });
  it("一个请求在途，第二批排队时间计入窗口，失败后继续执行", async () => {
    const first = deferred<DistributeTarget[]>();
    const query = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce([users[2]]);
    const { session } = setup(query);
    session.toggleInstitution("a");
    await vi.advanceTimersByTimeAsync(500);
    session.toggleInstitution("b");
    await vi.advanceTimersByTimeAsync(700);
    expect(query).toHaveBeenCalledTimes(1);
    first.reject(new Error("首批失败"));
    await vi.advanceTimersByTimeAsync(0);
    expect(query).toHaveBeenCalledTimes(2);
    expect(session.getSnapshot().selected.map((u) => u.sapId)).toEqual(["03"]);
    expect(session.getSnapshot().blocked).toBe(true);
    session.cancelPending("a");
    expect(session.getSnapshot().blocked).toBe(false);
  });
  it.each(["发送前", "发送后"])(
    "%s取消保留查看需求，响应不会重新选中",
    async (timing) => {
      const pending = deferred<DistributeTarget[]>();
      const query = vi.fn().mockReturnValue(pending.promise);
      const { session } = setup(query);
      session.toggleInstitution("a");
      if (timing === "发送后") await vi.advanceTimersByTimeAsync(500);
      session.toggleInstitution("a");
      await vi.advanceTimersByTimeAsync(500);
      expect(query).toHaveBeenCalledTimes(1);
      expect(session.getSnapshot().current).toBe("a");
      pending.resolve(users.slice(0, 2));
      await vi.advanceTimersByTimeAsync(0);
      expect(session.getSnapshot().selected).toEqual([]);
      expect(session.getSnapshot().people).toEqual(users.slice(0, 2));
      session.view("a");
      expect(query).toHaveBeenCalledTimes(1);
    },
  );
  it("勾选切换查看，取消全选保留名单，点击名称不改变选择", async () => {
    const { session, query } = setup();
    session.toggleInstitution("a");
    expect(session.getSnapshot().current).toBe("a");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.getSnapshot().people).toEqual(users.slice(0, 2));
    session.toggleInstitution("a");
    expect(session.getSnapshot().current).toBe("a");
    expect(session.getSnapshot().people).toEqual(users.slice(0, 2));
    expect(session.getSnapshot().selected).toEqual([]);
    session.view("b");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.getSnapshot().selected).toEqual([]);
    session.toggleInstitution("a");
    expect(session.getSnapshot().current).toBe("a");
    expect(session.getSnapshot().selected).toEqual(users.slice(0, 2));
    expect(query).toHaveBeenCalledTimes(2);
  });
  it("已加载上级复用下级，但下级不冒充上级完整名单", async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce([users[1]])
      .mockResolvedValueOnce(users);
    const { session } = setup(query);
    session.view("a1");
    await vi.advanceTimersByTimeAsync(500);
    session.view("root");
    await vi.advanceTimersByTimeAsync(500);
    expect(query).toHaveBeenCalledTimes(2);
    session.view("a");
    session.view("b");
    await vi.advanceTimersByTimeAsync(500);
    expect(query).toHaveBeenCalledTimes(2);
    expect(session.getSnapshot().people).toEqual([users[2]]);
  });
  it("半选补全、父级覆盖下级，取消单人保持其他机构选择", async () => {
    const { session } = setup();
    session.view("root");
    await vi.advanceTimersByTimeAsync(500);
    session.view("a1");
    session.togglePerson(users[1]);
    session.toggleInstitution("a");
    expect(
      session
        .getSnapshot()
        .selected.map((u) => u.sapId)
        .sort(),
    ).toEqual(["01", "02"]);
    session.toggleInstitution("b");
    session.togglePerson(users[1]);
    expect(
      session
        .getSnapshot()
        .selected.map((u) => u.sapId)
        .sort(),
    ).toEqual(["01", "03"]);
    session.toggleInstitution("a");
    expect(session.getSnapshot().selected).toHaveLength(3);
    session.toggleInstitution("a");
    expect(session.getSnapshot().selected).toEqual([users[2]]);
  });
  it("空名单缓存，查看失败不永久阻塞发布，失败全选重试后解除阻塞", async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce([])
      .mockRejectedValueOnce(new Error("失败"))
      .mockResolvedValueOnce([users[2]]);
    const { session } = setup(query);
    session.view("a");
    await vi.advanceTimersByTimeAsync(500);
    session.view("a");
    await vi.advanceTimersByTimeAsync(500);
    expect(query).toHaveBeenCalledTimes(1);
    session.view("b");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.getSnapshot().blocked).toBe(false);
    session.toggleInstitution("b");
    expect(session.getSnapshot().blocked).toBe(true);
    await vi.advanceTimersByTimeAsync(500);
    expect(session.getSnapshot().blocked).toBe(false);
    expect(session.getSnapshot().selected).toEqual([users[2]]);
  });
  it("取消失败的全选保留原有人员，之后查看成功也不会抹掉历史选择", async () => {
    const query = vi
      .fn()
      .mockRejectedValueOnce(new Error("失败"))
      .mockResolvedValueOnce(users.slice(0, 2));
    const session = new DistributionSession(root, [users[0]], query);
    sessions.push(session);
    session.toggleInstitution("a");
    await vi.advanceTimersByTimeAsync(500);
    session.cancelPending("a");
    expect(session.getSnapshot().selected).toEqual([users[0]]);
    session.view("a");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.getSnapshot().selected).toEqual([users[0]]);
  });
  it("完成机构全选后仅查看上级新名单，不自动增加接收人", async () => {
    const added = user("new-a", "a");
    const query = vi
      .fn()
      .mockResolvedValueOnce([users[0]])
      .mockResolvedValueOnce([users[0], added, users[2]]);
    const { session } = setup(query);
    session.toggleInstitution("a");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.getSnapshot().selected).toEqual([users[0]]);
    session.view("root");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.getSnapshot().selected).toEqual([users[0]]);
    expect(session.people("a")).toEqual([users[0], added]);
  });
  it("取得上级新名单后替换过期下级缓存，全选使用更新后的完整名单", async () => {
    const added = user("new-a", "a1");
    const query = vi
      .fn()
      .mockResolvedValueOnce([users[0]])
      .mockResolvedValueOnce([users[0], added, users[2]]);
    const { session } = setup(query);
    session.view("a");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.people("a1")).toEqual([]);
    session.view("root");
    await vi.advanceTimersByTimeAsync(500);
    session.view("a");
    expect(session.getSnapshot().people).toEqual([users[0], added]);
    expect(session.people("a1")).toEqual([added]);
    session.toggleInstitution("a");
    expect(session.getSnapshot().selected).toEqual([users[0], added]);
    expect(query).toHaveBeenCalledTimes(2);
  });
  it("全选失败后的半选点击重试补全，保留原有人员直到查询成功", async () => {
    const pending = deferred<DistributeTarget[]>();
    const query = vi
      .fn()
      .mockRejectedValueOnce(new Error("失败"))
      .mockReturnValueOnce(pending.promise);
    const session = new DistributionSession(root, [users[0]], query);
    sessions.push(session);
    session.toggleInstitution("a");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.getSnapshot().blocked).toBe(true);
    session.toggleInstitution("a");
    expect(session.getSnapshot().selected).toEqual([users[0]]);
    expect(session.getSnapshot().statuses.get("a")).toBe("waiting");
    await vi.advanceTimersByTimeAsync(500);
    expect(query).toHaveBeenCalledTimes(2);
    expect(session.getSnapshot().selected).toEqual([users[0]]);
    pending.resolve(users.slice(0, 2));
    await vi.advanceTimersByTimeAsync(0);
    expect(session.getSnapshot().selected).toEqual(users.slice(0, 2));
    expect(session.getSnapshot().blocked).toBe(false);
  });
  it("未匹配历史人员不应使已清空的分行分组重新生效", async () => {
    const unknown = user("historical", "removed-org");
    const session = new DistributionSession(root, [unknown], async () => users);
    sessions.push(session);
    session.toggleInstitution("root");
    await vi.advanceTimersByTimeAsync(500);
    expect(session.getSnapshot().anchors).toEqual(["root"]);
    session.selectPeople(users);
    expect(session.getSnapshot().selected).toEqual([unknown]);
    expect(session.getSnapshot().anchors).toEqual([]);
    session.toggleInstitution("a1");
    expect(session.getSnapshot().anchors).toEqual(["a1"]);
    expect(session.getSnapshot().selected).toEqual([unknown, users[1]]);
  });
  it("会话销毁使旧响应失效，不更新下一份计划", async () => {
    const pending = deferred<DistributeTarget[]>();
    const changed = vi.fn();
    const session = new DistributionSession(
      root,
      [],
      () => pending.promise,
      changed,
    );
    sessions.push(session);
    session.toggleInstitution("root");
    await vi.advanceTimersByTimeAsync(500);
    session.dispose();
    changed.mockClear();
    pending.resolve(users);
    await vi.advanceTimersByTimeAsync(0);
    expect(changed).not.toHaveBeenCalled();
  });
});
