import type { DistributeTarget } from "../types";
import type { Institution } from "./api";
import { InstitutionIndex } from "./tree";

export const BATCH_WINDOW_MS = 500;
type RequestStatus = "waiting" | "loading" | "error";
interface Intent {
  selected: boolean;
  order: number;
}
export interface DistributionSnapshot {
  current: string | null;
  selected: DistributeTarget[];
  people: DistributeTarget[] | undefined;
  statuses: Map<string, RequestStatus>;
  errors: Map<string, string>;
  anchors: string[];
  emptyInstitutions: string[];
  blocked: boolean;
}
type QueryPeople = (
  nodes: Institution[],
  signal: AbortSignal,
) => Promise<DistributeTarget[]>;

/** One form owns its queue/cache. The tree cache has a separate, longer lifetime. */
export class DistributionSession {
  readonly index: InstitutionIndex;
  private cache = new Map<string, DistributeTarget[]>();
  private selected = new Map<string, DistributeTarget>();
  private intents = new Map<string, Intent>();
  private pendingSelections = new Set<string>();
  private personIntents = new Map<string, Intent>();
  private anchors = new Set<string>();
  private emptyInstitutions = new Set<string>();
  private statuses = new Map<string, RequestStatus>();
  private errors = new Map<string, string>();
  private queue = new Map<string, number>();
  private current: string | null = null;
  private order = 0;
  private active: AbortController | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private disposed = false;
  private listeners = new Set<() => void>();
  private snapshot: DistributionSnapshot;

  constructor(
    root: Institution,
    initial: DistributeTarget[],
    private query: QueryPeople,
    private onChange: (snapshot: DistributionSnapshot) => void = () => {},
  ) {
    this.index = new InstitutionIndex(root);
    initial.forEach((user) => this.selected.set(user.sapId, user));
    this.snapshot = this.buildSnapshot();
  }
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  getSnapshot = () => this.snapshot;
  dispose() {
    this.disposed = true;
    this.active?.abort();
    if (this.timer) clearTimeout(this.timer);
    this.listeners.clear();
  }
  people(code: string): DistributeTarget[] | undefined {
    if (this.cache.has(code)) return this.cache.get(code);
    for (const [ancestor, users] of this.cache) {
      if (!this.index.contains(ancestor, code)) continue;
      const result = users.filter((user) => this.index.includes(code, user));
      this.cache.set(code, result);
      return result;
    }
  }
  private orgIntent(code: string): Intent | undefined {
    let latest: Intent | undefined;
    for (const [scope, intent] of this.intents) {
      if (
        this.index.contains(scope, code) &&
        (!latest || latest.order < intent.order)
      )
        latest = intent;
    }
    return latest;
  }
  isPendingSelection(code: string) {
    return (
      this.wants(code) &&
      [...this.pendingSelections].some((scope) =>
        this.index.contains(scope, code),
      )
    );
  }
  private hasUnselectedEmptyDescendant(code: string) {
    return [...(this.index.descendants.get(code) ?? [])].some(
      (scope) =>
        this.people(scope)?.length === 0 && !this.emptyInstitutions.has(scope),
    );
  }
  isInstitutionSelected(code: string) {
    const people = this.people(code);
    if (!people) return false;
    if (!people.length && !this.emptyInstitutions.has(code)) return false;
    return (
      people.every((user) => this.selected.has(user.sapId)) &&
      !this.hasUnselectedEmptyDescendant(code)
    );
  }
  cancelPending(code: string) {
    this.intents.set(code, { selected: false, order: ++this.order });
    this.clearPendingSelections(code);
    // Cancelling a failed bulk operation must preserve recipients selected before it.
    for (const user of this.selected.values()) {
      if (this.index.includes(code, user))
        this.personIntents.set(user.sapId, {
          selected: true,
          order: ++this.order,
        });
    }
    this.prune();
    this.emit();
    this.schedule();
  }
  private wants(code: string) {
    return this.orgIntent(code)?.selected === true;
  }
  private applyIntents(users: DistributeTarget[], scopes: string[]) {
    for (const user of users) {
      // Lookup results may refresh metadata, but only unfinished choices add recipients.
      if (!scopes.some((code) => this.index.includes(code, user))) {
        if (this.selected.has(user.sapId)) this.selected.set(user.sapId, user);
        continue;
      }
      let latest = this.personIntents.get(user.sapId);
      for (const [scope, intent] of this.intents) {
        if (
          this.index.includes(scope, user) &&
          (!latest || latest.order < intent.order)
        )
          latest = intent;
      }
      if (latest?.selected || (!latest && this.selected.has(user.sapId)))
        this.selected.set(user.sapId, user);
      else if (latest) this.selected.delete(user.sapId);
    }
  }
  private buildSnapshot(): DistributionSnapshot {
    const blocked = [...this.statuses].some(
      ([code, status]) =>
        status !== "error" || (this.wants(code) && !this.people(code)),
    );
    return {
      current: this.current,
      selected: [...this.selected.values()],
      people: this.current ? this.people(this.current) : undefined,
      statuses: new Map(this.statuses),
      errors: new Map(this.errors),
      anchors: [...this.anchors],
      emptyInstitutions: [...this.emptyInstitutions],
      blocked,
    };
  }
  private emit() {
    if (this.disposed) return;
    const selected = [...this.selected.values()].filter((user) =>
      this.index.nodes.has(user.bbkOrgId ?? ""),
    );
    for (const code of this.emptyInstitutions) {
      if (this.people(code)?.length || this.hasUnselectedEmptyDescendant(code))
        this.emptyInstitutions.delete(code);
    }
    for (const anchor of this.anchors) {
      if (
        !this.emptyInstitutions.has(anchor) &&
        !selected.some((user) => this.index.includes(anchor, user))
      )
        this.anchors.delete(anchor);
    }
    this.snapshot = this.buildSnapshot();
    this.onChange(this.snapshot);
    this.listeners.forEach((listener) => listener());
  }
  view(code: string) {
    this.current = code;
    this.enqueue(code);
    this.prune();
    this.emit();
    this.schedule();
  }
  toggleInstitution(code: string) {
    const people = this.people(code);
    const pending =
      this.wants(code) &&
      [...this.pendingSelections].some((scope) => {
        const status = this.statuses.get(scope);
        return (
          this.index.contains(scope, code) &&
          (status === "waiting" || status === "loading")
        );
      });
    const full = this.isInstitutionSelected(code);
    if (pending && !full) {
      this.cancelPending(code);
      return;
    }
    if (full) {
      this.removeInstitution(code);
      return;
    }
    this.current = code;
    this.intents.set(code, { selected: true, order: ++this.order });
    if (people) {
      this.applyIntents(people, [code]);
      this.addSelectionAnchor(code);
    } else {
      this.pendingSelections.add(code);
      this.enqueue(code);
    }
    this.prune();
    this.emit();
    this.schedule();
  }
  removeInstitution(code: string) {
    this.intents.set(code, { selected: false, order: ++this.order });
    this.clearPendingSelections(code);
    for (const empty of this.emptyInstitutions) {
      if (this.index.contains(code, empty) || this.index.contains(empty, code))
        this.emptyInstitutions.delete(empty);
    }
    for (const [id, user] of this.selected) {
      if (this.index.includes(code, user)) this.selected.delete(id);
    }
    for (const anchor of this.anchors) {
      if (this.index.contains(code, anchor)) this.anchors.delete(anchor);
    }
    this.prune();
    this.emit();
    this.schedule();
  }
  togglePerson(user: DistributeTarget) {
    const selected = !this.selected.has(user.sapId);
    this.personIntents.set(user.sapId, { selected, order: ++this.order });
    if (selected) {
      this.selected.set(user.sapId, user);
      if (this.current) this.anchors.add(this.current);
    } else this.selected.delete(user.sapId);
    this.emit();
  }
  selectPeople(users: DistributeTarget[]) {
    const selected = !users.every((user) => this.selected.has(user.sapId));
    const order = ++this.order;
    for (const user of users) {
      this.personIntents.set(user.sapId, { selected, order });
      if (selected) this.selected.set(user.sapId, user);
      else this.selected.delete(user.sapId);
    }
    if (selected && this.current) this.anchors.add(this.current);
    this.emit();
  }
  removeUnknown() {
    for (const [id, user] of this.selected) {
      if (!this.index.nodes.has(user.bbkOrgId ?? "")) {
        this.selected.delete(id);
        this.personIntents.set(id, { selected: false, order: ++this.order });
      }
    }
    this.emit();
  }
  clear() {
    this.removeInstitution(this.index.root.mOrgCode);
  }
  retry(code: string) {
    this.enqueue(code);
    this.emit();
    this.schedule();
  }
  private clearPendingSelections(code: string) {
    for (const pending of this.pendingSelections) {
      if (this.index.contains(code, pending))
        this.pendingSelections.delete(pending);
    }
  }
  private addSelectionAnchor(code: string) {
    for (const scope of this.index.descendants.get(code) ?? []) {
      if (this.people(scope)?.length === 0 && this.wants(scope))
        this.emptyInstitutions.add(scope);
    }
    if (this.people(code)?.length === 0) {
      if (this.isInstitutionSelected(code)) this.anchors.add(code);
      return;
    }
    // Pending bulk choices must not regroup recipients already selected elsewhere.
    if (
      this.people(code)?.some(
        (user) =>
          this.index.nodes.has(user.bbkOrgId ?? "") &&
          this.selected.has(user.sapId),
      )
    )
      this.anchors.add(code);
  }
  private cachePeople(codes: string[], users: DistributeTarget[]) {
    // A newer complete ancestor result supersedes exact and derived descendant caches.
    for (const cached of this.cache.keys()) {
      if (codes.some((code) => this.index.contains(code, cached)))
        this.cache.delete(cached);
    }
    codes.forEach((code) =>
      this.cache.set(
        code,
        users.filter((user) => this.index.includes(code, user)),
      ),
    );
  }
  private enqueue(code: string) {
    if (
      this.people(code) ||
      this.statuses.get(code) === "loading" ||
      this.queue.has(code)
    )
      return;
    this.queue.set(code, Date.now());
    this.statuses.set(code, "waiting");
    this.errors.delete(code);
  }
  private prune() {
    for (const [code, status] of this.statuses) {
      if (status === "loading") continue;
      if (this.people(code) || (!this.wants(code) && this.current !== code)) {
        this.statuses.delete(code);
        this.errors.delete(code);
        this.queue.delete(code);
      }
    }
  }
  private schedule() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (this.disposed || this.active || !this.queue.size) return;
    const first = Math.min(...this.queue.values());
    const remaining = BATCH_WINDOW_MS - (Date.now() - first);
    if (remaining > 0)
      this.timer = setTimeout(() => {
        void this.flush();
      }, remaining);
    else void this.flush();
  }
  private async flush() {
    this.timer = null;
    if (this.disposed || this.active) return;
    this.prune();
    // The API adapter expands each retained scope into all of its descendant orgList entries.
    const codes = [...this.queue.keys()].filter(
      (code, _, all) =>
        !all.some(
          (other) => other !== code && this.index.contains(other, code),
        ),
    );
    if (!codes.length) {
      this.emit();
      return;
    }
    const batch = [...this.queue.keys()];
    batch.forEach((code) => {
      this.queue.delete(code);
      this.statuses.set(code, "loading");
    });
    const controller = new AbortController();
    this.active = controller;
    this.emit();
    try {
      const users = await this.query(
        codes.map((code) => this.index.nodes.get(code)!),
        controller.signal,
      );
      if (this.disposed) return;
      this.cachePeople(codes, users);
      const completed = [...this.pendingSelections].filter(
        (code) => this.people(code) !== undefined,
      );
      this.applyIntents(users, completed);
      completed.forEach((code) => {
        this.addSelectionAnchor(code);
        this.pendingSelections.delete(code);
      });
      batch.forEach((code) => {
        this.statuses.delete(code);
        this.errors.delete(code);
      });
      this.prune();
    } catch (error) {
      if (this.disposed) return;
      const message =
        error instanceof Error ? error.message : "人员查询失败，请重试";
      batch.forEach((code) => {
        this.statuses.set(code, "error");
        this.errors.set(code, message);
      });
      this.prune();
    } finally {
      this.active = null;
      if (!this.disposed) {
        this.emit();
        this.schedule();
      }
    }
  }
}
