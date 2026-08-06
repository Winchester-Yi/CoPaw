import { createGlobalStyle } from "antd-style";
import { DESIGN_TOKENS } from "@/config/designTokens";

export default createGlobalStyle`
.welcome-center-layout {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  width: 100%;
  padding: 40px 20px;
  gap: 0;
  background: url('/chat-bg.png') center/cover no-repeat;
  background: linear-gradient(180deg, #E8EEFF 0%, #F1F2F7 40%, #F5F5FA 100%);
}

.welcome-greeting {
  font-size: 22px;
  font-weight: 600;
  color: ${DESIGN_TOKENS.colorTextDark};
  line-height: 33px;
  margin-bottom: 40px;
  text-align: center;
}

.welcome-input-card {
  position: relative;
  width: ${DESIGN_TOKENS.inputCardWidth}px;
  max-width: 100%;
  background-color: ${DESIGN_TOKENS.colorBgCard};
  border-radius: ${DESIGN_TOKENS.radiusCard}px;
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 28px;
  box-shadow: 0px 0px 8px 0px rgba(0, 0, 0, 0.08);
}

.welcome-input-placeholder {
  font-size: 14px;
  line-height: 22px;
  color: ${DESIGN_TOKENS.colorTextMuted};
  resize: none;
  border: none;
  outline: none;
  background: transparent;
  width: 100%;
  min-height: 24px;
  max-height: 200px;
  font-family: inherit;
  padding: 4px 0;
  overflow: auto;
}

.welcome-skill-editor {
  min-height: 24px;
  white-space: pre-wrap;

  &:empty::before {
    color: ${DESIGN_TOKENS.colorTextMuted};
    content: attr(data-placeholder);
    pointer-events: none;
  }
}

.welcome-input-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-left: -6px;
}

.welcome-input-actions-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.welcome-input-send-btn {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background-color: ${DESIGN_TOKENS.colorPrimary};
  border: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: opacity 0.15s ease;

  &:hover {
    opacity: 0.85;
  }

  &:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
}

.welcome-tabs-area {
  width: ${DESIGN_TOKENS.inputCardWidth}px;
  max-width: 100%;
  margin-bottom: 28px;
}

.welcome-cases-area {
  width: 935px;
  margin: 0 auto;
}
`;
