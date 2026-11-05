import { expect, it, vi } from "vitest";
import { clearAuthToken, setAuthToken } from "./config";
import { clearExternalToken, setExternalToken } from "./externalToken";

it.each([clearAuthToken, clearExternalToken])(
  "清空认证时通知内存任务快照退出",
  (clear) => {
    const listener = vi.fn();
    window.addEventListener("swe:auth-cleared", listener);
    try {
      clear();
      expect(listener).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener("swe:auth-cleared", listener);
    }
  },
);

it.each([() => setAuthToken("token"), () => setExternalToken("token", 600)])(
  "重新认证后通知任务区域重新确认身份",
  (restore) => {
    const listener = vi.fn();
    window.addEventListener("swe:auth-ready", listener);
    try {
      restore();
      expect(listener).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener("swe:auth-ready", listener);
    }
  },
);
