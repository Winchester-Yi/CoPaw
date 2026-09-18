import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import styles from "../index.module.less";
import { SceneDescription, StepIndicator } from "./index";

describe("SceneDescription", () => {
  it("用两行截断样式展示描述，并保留完整文本供悬停查看", () => {
    const description =
      "这是一段超过两行时仍可通过悬停查看全文的经营场景技能描述";

    render(<SceneDescription description={description} />);

    const element = screen.getByText(description);
    expect(element).toHaveClass(styles.sceneDescription);
    expect(element).toHaveAttribute("title", description);
  });
});

describe("StepIndicator", () => {
  it("区分当前、已完成和未开始步骤的图标状态", () => {
    const { rerender } = render(
      <StepIndicator active completed={false} number={1} />,
    );
    expect(screen.getByText("1")).toHaveClass(styles.stepActive);
    expect(screen.getByText("1")).not.toHaveClass(styles.stepComplete);

    rerender(<StepIndicator active={false} completed number={1} />);
    expect(screen.getByText("✓")).toHaveClass(styles.stepComplete);
    expect(screen.getByText("✓")).not.toHaveClass(styles.stepActive);

    rerender(<StepIndicator active={false} completed={false} number={2} />);
    expect(screen.getByText("2")).not.toHaveClass(styles.stepActive);
    expect(screen.getByText("2")).not.toHaveClass(styles.stepComplete);
  });
});
