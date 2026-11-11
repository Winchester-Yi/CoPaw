import type { DistributeTarget } from "../types";
import type { Institution } from "./api";

export class InstitutionIndex {
  readonly nodes = new Map<string, Institution>();
  readonly parents = new Map<string, string>();
  readonly descendants = new Map<string, Set<string>>();
  constructor(readonly root: Institution) {
    const visit = (node: Institution): Set<string> => {
      this.nodes.set(node.mOrgCode, node);
      const codes = new Set([node.mOrgCode]);
      for (const child of node.children) {
        this.parents.set(child.mOrgCode, node.mOrgCode);
        visit(child).forEach((code) => codes.add(code));
      }
      this.descendants.set(node.mOrgCode, codes);
      return codes;
    };
    visit(root);
  }
  contains(parent: string, child: string) {
    return this.descendants.get(parent)?.has(child) ?? false;
  }
  includes(code: string, user: DistributeTarget) {
    return (
      code === this.root.mOrgCode || this.contains(code, user.bbkOrgId ?? "")
    );
  }
  branchOf(user: DistributeTarget): string | undefined {
    let code = user.bbkOrgId;
    if (!code || !this.nodes.has(code)) return undefined;
    while (
      this.parents.has(code) &&
      this.parents.get(code) !== this.root.mOrgCode
    ) {
      code = this.parents.get(code)!;
    }
    return code;
  }
  path(code: string): Institution[] {
    const nodes: Institution[] = [];
    let current: string | undefined = code;
    while (current) {
      const node = this.nodes.get(current);
      if (node) nodes.unshift(node);
      current = this.parents.get(current);
    }
    return nodes;
  }
}
