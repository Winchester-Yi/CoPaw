import { cleanup, render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it } from "vitest";
import { getBrandTheme } from "../config/brandThemes";
import { useIframeStore } from "../stores/iframeStore";
import { BrandThemeProvider } from "./BrandThemeContext";

afterEach(() => {
  cleanup();
  useIframeStore.getState().clearContext();
});

it.each([
  ["/chat-share/token?source=pfmind", "pfmind"],
  ["/chat-share/token?source=ruice&hideMenu=true", "ruice"],
  ["/chat-share/token", "WDYT"],
  ["/chat-share/token?source=unknown", "WDYT"],
  ["/chat-share/token?source=constructor", "WDYT"],
  ["/chat?source=pfmind", "WDYT"],
])("uses the expected brand title for %s", (url, expectedSource) => {
  useIframeStore.getState().setContext({ source: "WDYT" });
  render(
    <MemoryRouter initialEntries={[url]}>
      <BrandThemeProvider>{null}</BrandThemeProvider>
    </MemoryRouter>,
  );

  expect(document.title).toBe(getBrandTheme(expectedSource).brandName);
});
