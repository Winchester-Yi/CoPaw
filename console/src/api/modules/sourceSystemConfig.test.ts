import { beforeEach, describe, expect, it, vi } from "vitest";
import { sourceSystemConfigApi } from "./sourceSystemConfig";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
}));

vi.mock("../request", () => ({
  request: mocks.request,
}));

describe("sourceSystemConfigApi", () => {
  beforeEach(() => {
    mocks.request.mockReset();
  });

  it("reads effective source config", async () => {
    mocks.request.mockResolvedValueOnce({ source_id: "source-a" });

    await sourceSystemConfigApi.getEffective();

    expect(mocks.request).toHaveBeenCalledWith(
      "/source-system-config/effective",
    );
  });

  it("reads manager source config by encoded source id", async () => {
    mocks.request.mockResolvedValueOnce({ source_id: "source/a" });

    await sourceSystemConfigApi.getSource("source/a");

    expect(mocks.request).toHaveBeenCalledWith(
      "/source-system-config/sources/source%2Fa",
      {
        headers: {
          "X-User-Role": "manager",
        },
      },
    );
  });

  it("upserts manager source config", async () => {
    mocks.request.mockResolvedValueOnce({ source_id: "source-a" });

    await sourceSystemConfigApi.upsertSource("source-a", {
      config: { source_name: "Source A" },
    });

    expect(mocks.request).toHaveBeenCalledWith(
      "/source-system-config/sources/source-a",
      {
        method: "PUT",
        headers: {
          "X-User-Role": "manager",
        },
        body: JSON.stringify({
          config: { source_name: "Source A" },
        }),
      },
    );
  });
});
