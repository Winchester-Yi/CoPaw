import { readFileSync } from "node:fs";
import ts from "typescript";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../request";
import {
  tokenDiagnosisV2Api,
  type CallDTO,
  type RunDTO,
  type OperationDTO,
} from "./tokenDiagnosisV2";
vi.mock("../request", () => ({ request: vi.fn().mockResolvedValue({}) }));
describe("B diagnosis resource paths", () => {
  beforeEach(() => vi.mocked(request).mockClear());
  it("uses all eleven real GET projections with encoded IDs and abort ownership", async () => {
    const signal = new AbortController().signal;
    const filters = {
      model_name: "a/b 中文",
      start: "2026-10-11T00:00:00Z",
      end: "2026-10-12T00:00:00Z",
    };
    await tokenDiagnosisV2Api.dashboard(signal, filters);
    await tokenDiagnosisV2Api.tasks(signal, filters);
    await tokenDiagnosisV2Api.task("task/1", signal, filters);
    await tokenDiagnosisV2Api.branches("task/1", signal, filters);
    await tokenDiagnosisV2Api.calls("task/1", signal, filters);
    await tokenDiagnosisV2Api.context("ctx/1", signal, filters);
    await tokenDiagnosisV2Api.prefix("call/1", signal, filters);
    await tokenDiagnosisV2Api.operations("task/1", signal, filters, {
      operation_id: "op/1",
      relation_cursor: "a+b=",
    });
    await tokenDiagnosisV2Api.issues(signal, filters);
    await tokenDiagnosisV2Api.instances("s:abc/1", signal, filters);
    await tokenDiagnosisV2Api.evaluation("eval/1", signal, filters);
    expect(vi.mocked(request).mock.calls).toHaveLength(11);
    const paths = [
      "dashboard",
      "tasks",
      "tasks/task%2F1",
      "tasks/task%2F1/branches",
      "tasks/task%2F1/calls",
      "contexts/ctx%2F1",
      "calls/call%2F1/prefix",
      "tasks/task%2F1/operations",
      "issues",
      "issues/s%3Aabc%2F1/instances",
      "evaluations/eval%2F1",
    ];
    vi.mocked(request).mock.calls.forEach(([path, options], i) => {
      expect(path.split("?")[0]).toBe(
        `/monitor/token-diagnostics/v2/${paths[i]}`,
      );
      expect(path).not.toContain("/api");
      expect(new URLSearchParams(path.split("?")[1]).get("model_name")).toBe(
        filters.model_name,
      );
      expect(options?.signal).toBe(signal);
      expect(options?.method).toBeUndefined();
    });
    expect(
      new URLSearchParams(
        vi.mocked(request).mock.calls[7][0].split("?")[1],
      ).get("relation_cursor"),
    ).toBe("a+b=");
  });
  it("keeps nullable persisted call, run, and operation facts unchanged", async () => {
    const call: CallDTO = {
      call_id: "call",
      task_run_id: "task",
      branch_run_id: "branch",
      logical_call_id: null,
      logical_seq: null,
      attempt_no: null,
      agent_id: null,
      provider_id: null,
      model_name: null,
      purpose: null,
      started_at: null,
      ended_at: null,
      execution_state: null,
      input_tokens: null,
      output_tokens: 0,
      estimated_input_tokens: null,
      max_input_tokens: null,
      max_output_tokens: null,
      usage_status: null,
      capture_state: null,
      context_id: null,
      step_id: null,
      parent_step_id: null,
      identity_source: null,
      compaction_epoch: null,
      assembly_stage: null,
      prompt_format_confidence: null,
      context_builder_version: null,
      estimator_version: null,
      usage_origin: null,
      usage_accuracy: null,
      finish_reason: null,
      usage_details: {},
      evaluations: { items: [], limited: false },
    };
    const run: Pick<RunDTO, "role" | "purpose" | "started_at"> = {
      role: null,
      purpose: null,
      started_at: null,
    };
    const operation: OperationDTO = {
      operation_id: "op",
      task_run_id: "task",
      branch_run_id: "branch",
      operation_type: null,
      operation_state: null,
      trigger_reason: null,
      started_at: null,
      ended_at: null,
      before_context_id: null,
      after_context_id: null,
      summary_call_id: "call",
      missing_before: null,
      missing_after: null,
      capture_state: null,
      before_context: null,
      after_context: null,
      summary_call: call,
      comparison: {
        unit: "estimated_tokens",
        before_estimated_tokens: null,
        after_estimated_tokens: null,
        saved_estimated_tokens: null,
        reason_code: "operation_not_successful",
      },
      evidence_expired: {
        before_context: false,
        after_context: false,
        summary_call: false,
      },
      segments: { items: [], next_cursor: null },
    };
    const filename = "/nullable-wire.ts";
    const interfaces = readFileSync(
      "src/api/modules/tokenDiagnosisV2.ts",
      "utf8",
    )
      .replace('import { request } from "../request";', "")
      .split("const root = ")[0];
    const source = `${interfaces}
const call: CallDTO = ${JSON.stringify(call)};
const run: Pick<RunDTO, "role" | "purpose" | "started_at"> = ${JSON.stringify(
      run,
    )};
const operation: OperationDTO = ${JSON.stringify({
      ...operation,
      summary_call: null,
    }).replace('"summary_call":null', '"summary_call":call')};`;
    const options: ts.CompilerOptions = {
      noEmit: true,
      strictNullChecks: true,
      skipLibCheck: true,
      target: ts.ScriptTarget.ES2020,
    };
    const host = ts.createCompilerHost(options);
    const originalGetSourceFile = host.getSourceFile;
    host.getSourceFile = (name, version, onError, createNew) =>
      name === filename
        ? ts.createSourceFile(name, source, version, true)
        : originalGetSourceFile(name, version, onError, createNew);
    const program = ts.createProgram([filename], options, host);
    expect(
      ts
        .getPreEmitDiagnostics(program)
        .map((diagnostic) =>
          ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
        ),
    ).toEqual([]);
    const signal = new AbortController().signal;
    vi.mocked(request).mockResolvedValueOnce({
      items: [call],
      next_cursor: null,
    });
    const calls = await tokenDiagnosisV2Api.calls("task", signal);
    expect(calls.items[0]).toBe(call);
    expect(calls.items[0].logical_seq).toBeNull();
    expect(calls.items[0].input_tokens).toBeNull();
    expect(calls.items[0].output_tokens).toBe(0);
    vi.mocked(request).mockResolvedValueOnce({ root: run });
    expect((await tokenDiagnosisV2Api.task("task", signal)).root).toBe(run);
    vi.mocked(request).mockResolvedValueOnce({
      items: [operation],
      next_cursor: null,
    });
    expect(
      (await tokenDiagnosisV2Api.operations("task", signal)).items[0],
    ).toBe(operation);
  });
});
