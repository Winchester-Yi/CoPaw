# Wealth Workbench keeps prototype styling via CSS Modules under a scoped root class

The 智能财富工作台 pages live inside the CoPaw console at `/wealth/*` as React + TS, but they deliberately do not adopt CoPaw design tokens or antd components: visual fidelity to the standalone prototype (`#0867ff` palette, its own typography and spacing) is a hard product requirement. The prototype stylesheet is ported into `index.module.less` with every selector nested under one `.root` class, CSS custom properties moved off `:root` onto that class, bare element selectors scoped, and kebab-case class names camelCased, so nothing leaks into or inherits from the host console. We rejected iframe isolation (the deliverable must be real React routes inside `console/src`, and CoPaw's existing iframe-embedding message flow must keep working) and a plain global stylesheet (it would bleed into CoPaw pages). Mock data lives behind an in-memory `api.ts` so the pages never persist drafts to localStorage; a refresh resets everything, matching the "replace with real interfaces later" plan.

**Consequences**

- CoPaw-wide token or antd theme changes do not affect `/wealth/*`; conversely, fixing a look inside the workbench never touches console pages.
- The two style systems must not be "unified" by well-meaning cleanups — the divergence is deliberate.
- The workbench carries its own icon set (`Icon.tsx` symbols) and dialog/toast implementations instead of antd equivalents.
- When real APIs land, only `api.ts` changes; components and the zustand store already treat it as the async boundary.
- Isolation is one-directional: the shell's element-level global rules still pierce the workbench subtree. Two known compensations live in `index.module.less`: `.root` is the page's scroll container because the shell sets `html, body { height:100%; overflow: hidden }`, and `.dialog` re-declares `margin: auto` because a shell-injected `* { margin: 0 }` overrides the UA centering of native `<dialog>`. If the shell's global resets change, re-check these two spots first.
