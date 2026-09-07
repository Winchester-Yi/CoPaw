import { createGlobalStyle } from "antd-style";

export default createGlobalStyle`
.${(p) => p.theme.prefixCls}-suggestions {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
  padding: 0 4px;

  &-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  &-label {
    color: ${({ theme }) => theme.colorTextSecondary};
    font-size: 12px;
    line-height: 18px;
    font-weight: 500;
  }

  &-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  &-item {
    display: flex;
    align-items: center;
    gap: 6px;
    border: 0;
    padding: 6px 12px;
    background-color: ${({ theme }) => theme.colorFillQuaternary};
    border-radius: 16px;
    cursor: pointer;
    font: inherit;
    text-align: left;
    max-width: 400px; // 扩大宽度限制
    transition: background-color 0.2s ease;

    &-text {
      color: ${({ theme }) => theme.colorText};
      font-size: 13px;
      line-height: 20px;
      // 允许换行显示完整内容
      word-break: break-word;
    }

    &-icon {
      width: 14px;
      height: 14px;
      color: ${({ theme }) => theme.colorTextSecondary};
      flex-shrink: 0; // 图标不缩小
    }

    &:hover {
      background-color: ${({ theme }) => theme.colorFillTertiary};
    }

    &:active {
      background-color: ${({ theme }) => theme.colorFillSecondary};
    }
  }
}

.${(p) => p.theme.prefixCls}-response-process-disclosure {
  width: 100%;
  margin: 0 0 4px;

  &-trigger {
    display: flex;
    align-items: center;
    position: relative;
    width: 100%;
    min-height: 32px;
    gap: 10px;
    padding: 6px 10px;
    border: 1px solid transparent;
    border-radius: 10px;
    background: ${({ theme }) => theme.colorFillAlter};
    color: ${({ theme }) => theme.colorTextSecondary};
    cursor: pointer;
    font: inherit;
    font-size: 12px;
    line-height: 20px;
    text-align: left;
    transition:
      background-color 0.18s ease,
      border-color 0.18s ease,
      color 0.18s ease;

    &:hover {
      background: ${({ theme }) => theme.colorFillTertiary};
      border-color: ${({ theme }) => theme.colorBorderSecondary};
      color: ${({ theme }) => theme.colorText};
    }

    &[aria-expanded="true"] {
      background: ${({ theme }) => theme.colorFillTertiary};
    }

    &:focus-visible {
      outline: 2px solid ${({ theme }) => theme.colorPrimaryBorder};
      outline-offset: 2px;
    }

    &[data-status="failed"] .${(p) =>
      p.theme.prefixCls}-response-process-disclosure-status {
      color: ${({ theme }) => theme.colorWarning};
    }

    &[data-status="running"] .${(p) =>
      p.theme.prefixCls}-response-process-disclosure-status {
      color: ${({ theme }) => theme.colorPrimary};
    }

    &[data-status="canceled"] .${(p) =>
      p.theme.prefixCls}-response-process-disclosure-status {
      color: ${({ theme }) => theme.colorTextTertiary};
    }
  }

  &-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    flex: 0 0 16px;
    border-radius: ${({ theme }) => theme.borderRadiusSM}px;
    background: transparent;
    color: ${({ theme }) => theme.colorTextTertiary};
    transition:
      background-color 0.18s ease,
      color 0.18s ease;

    svg {
      width: 12px;
      height: 12px;
    }
  }

  &-trigger:hover &-chevron {
    color: ${({ theme }) => theme.colorTextSecondary};
  }

  &-trigger[aria-expanded="true"] &-chevron {
    background: transparent;
    color: ${({ theme }) => theme.colorTextSecondary};
  }

  &-copy {
    display: flex;
    align-items: center;
    gap: 18px;
    flex: 1 1 auto;
    min-width: 0;
  }

  &-title {
    color: ${({ theme }) => theme.colorTextSecondary};
    font-weight: 500;
    white-space: nowrap;
  }

  &-meta {
    display: flex;
    align-items: center;
    gap: 14px;
    min-width: 0;
    color: ${({ theme }) => theme.colorTextTertiary};
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &-meta > span {
    flex: 0 0 auto;
  }

  &-metric {
    display: inline-flex;
    align-items: center;
    gap: 4px;

    svg {
      width: 14px;
      height: 14px;
      flex: 0 0 14px;
      color: currentColor;
    }
  }

  &-status {
    padding: 0;
    border-radius: 0;
    background: transparent;
    color: ${({ theme }) => theme.colorSuccess};
    font-size: 12px;
    line-height: 20px;
    font-weight: 500;
    white-space: nowrap;
  }

  &-action {
    flex: 0 0 auto;
    color: ${({ theme }) => theme.colorTextTertiary};
    font-weight: 500;
    white-space: nowrap;
    transition: color 0.18s ease;
  }

  &-body {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-top: 4px;
    overflow: hidden;
    animation: ${(p) =>
      p.theme.prefixCls}-response-process-disclosure-open 0.18s ease-out;
  }

  &-body[hidden] {
    display: none;
  }
}

@keyframes ${(p) => p.theme.prefixCls}-response-process-disclosure-open {
  from {
    opacity: 0;
  }
  to {
    opacity: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  .${(p) => p.theme.prefixCls}-response-process-disclosure {
    &-trigger {
      transition: none;
    }

    &-chevron {
      transition: none;
    }

    &-body {
      animation: none;
    }
  }
}

.${(p) => p.theme.prefixCls}-response-operation-group {
  width: 100%;
  margin: 0 0 4px;

  &-trigger {
    display: flex;
    align-items: center;
    position: relative;
    width: 100%;
    min-height: 32px;
    gap: 10px;
    padding: 6px 10px;
    border: 1px solid transparent;
    border-radius: 10px;
    background: ${({ theme }) => theme.colorFillAlter};
    color: ${({ theme }) => theme.colorTextSecondary};
    cursor: pointer;
    font: inherit;
    font-size: 12px;
    line-height: 20px;
    text-align: left;
    transition:
      background-color 0.18s ease,
      border-color 0.18s ease,
      color 0.18s ease;

    &:hover {
      background: ${({ theme }) => theme.colorFillTertiary};
      border-color: ${({ theme }) => theme.colorBorderSecondary};
      color: ${({ theme }) => theme.colorText};
    }

    &[aria-expanded="true"] {
      background: ${({ theme }) => theme.colorFillTertiary};
    }

    &:focus-visible {
      outline: 2px solid ${({ theme }) => theme.colorPrimaryBorder};
      outline-offset: 2px;
    }
  }

  &-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    flex: 0 0 16px;
    color: ${({ theme }) => theme.colorTextTertiary};

    svg {
      width: 14px;
      height: 14px;
    }

    &[data-status="running"] {
      color: ${({ theme }) => theme.colorPrimary};
    }

    &[data-status="pending"],
    &[data-status="warning"] {
      color: ${({ theme }) => theme.colorWarning};
    }

    &[data-status="failed"] {
      color: ${({ theme }) => theme.colorError};
    }

    &[data-status="success"] {
      color: ${({ theme }) => theme.colorSuccess};
    }

    &[data-status="canceled"] {
      color: ${({ theme }) => theme.colorTextTertiary};
    }
  }

  &-title {
    flex: 1 1 auto;
    min-width: 0;
    color: ${({ theme }) => theme.colorTextSecondary};
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    flex: 0 0 16px;
    color: ${({ theme }) => theme.colorTextTertiary};

    svg {
      width: 12px;
      height: 12px;
    }
  }

  &-body {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-top: 4px;
    animation: ${(p) =>
      p.theme.prefixCls}-response-operation-group-open 0.18s ease-out;
  }

  &-body[hidden] {
    display: none;
  }

  &-tool,
  &-reasoning {
    min-width: 0;
  }

}

@keyframes ${(p) => p.theme.prefixCls}-response-operation-group-open {
  from {
    opacity: 0;
  }

  to {
    opacity: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  .${(p) => p.theme.prefixCls}-response-operation-group {
    &-trigger {
      transition: none;
    }

    &-chevron {
      transition: none;
    }

    &-body {
      animation: none;
    }
  }
}
`;
