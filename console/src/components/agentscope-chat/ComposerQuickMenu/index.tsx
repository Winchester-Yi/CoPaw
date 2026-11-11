import { PlusOutlined, RightOutlined } from "@ant-design/icons";
import classNames from "classnames";
import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import styles from "./index.module.less";

const ParentMenuPositionContext = createContext<React.CSSProperties | null>(
  null,
);

export interface ComposerQuickMenuProps {
  children?: React.ReactNode;
  disabled?: boolean;
  triggerLabel: string;
}

export interface ComposerQuickMenuItemProps {
  icon?: React.ReactNode;
  label: React.ReactNode;
  extra?: React.ReactNode;
  onClick?: () => void;
  className?: string;
  disabled?: boolean;
  interactive?: boolean;
}

export function ComposerQuickMenuItem(props: ComposerQuickMenuItemProps) {
  const {
    className,
    disabled = false,
    extra,
    icon,
    interactive = false,
    label,
    onClick,
  } = props;
  const clickable = Boolean(onClick) && !disabled;
  const interactiveItem = (clickable || interactive) && !disabled;

  return (
    <div
      className={classNames(
        styles.item,
        interactiveItem && styles.itemClickable,
        disabled && styles.itemDisabled,
        className,
      )}
      aria-disabled={onClick ? disabled : undefined}
      onClick={clickable ? onClick : undefined}
      role={onClick ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={
        clickable
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onClick();
              }
            }
          : undefined
      }
    >
      {icon ? <span className={styles.icon}>{icon}</span> : null}
      <span className={styles.label}>{label}</span>
      {extra ? <span className={styles.extra}>{extra}</span> : null}
    </div>
  );
}

export interface ComposerQuickMenuSubmenuProps
  extends Omit<ComposerQuickMenuItemProps, "extra" | "onClick"> {
  children?: React.ReactNode;
  panelWidth?: string;
}

export function ComposerQuickMenuSubmenu(props: ComposerQuickMenuSubmenuProps) {
  const {
    children,
    className,
    disabled = false,
    icon,
    interactive: _interactive,
    label,
    panelWidth,
  } = props;
  const [open, setOpen] = useState(false);
  const submenuRef = useRef<HTMLDivElement>(null);
  const submenuPanelRef = useRef<HTMLDivElement>(null);
  const [submenuPanelStyle, setSubmenuPanelStyle] =
    useState<React.CSSProperties | null>(null);
  const [submenuOpensLeft, setSubmenuOpensLeft] = useState(false);
  const parentPosition = useContext(ParentMenuPositionContext);
  const items = useMemo(
    () => React.Children.toArray(children).filter(Boolean),
    [children],
  );

  useEffect(() => {
    if (disabled) {
      setOpen(false);
    }
  }, [disabled]);

  const expanded = open && !disabled;

  useLayoutEffect(() => {
    if (!expanded || !submenuRef.current || !submenuPanelRef.current) {
      setSubmenuPanelStyle(null);
      setSubmenuOpensLeft(false);
      return undefined;
    }

    const updateSubmenuPosition = () => {
      const gap = 8;
      const viewportPadding = 16;
      const submenuRect = submenuRef.current?.getBoundingClientRect();
      const panelRect = submenuPanelRef.current?.getBoundingClientRect();
      if (!submenuRect || !panelRect) {
        return;
      }

      const availableRight = Math.max(
        0,
        window.innerWidth - submenuRect.right - gap - viewportPadding,
      );
      const availableLeft = Math.max(
        0,
        submenuRect.left - gap - viewportPadding,
      );
      const opensLeft =
        availableRight < panelRect.width && availableLeft > availableRight;
      const availableWidth = opensLeft ? availableLeft : availableRight;

      setSubmenuOpensLeft(opensLeft);
      setSubmenuPanelStyle({
        width: `${Math.min(panelRect.width, availableWidth)}px`,
        left: opensLeft ? "auto" : `calc(100% + ${gap}px)`,
        right: opensLeft ? `calc(100% + ${gap}px)` : "auto",
        top: `${Math.max(
          viewportPadding - submenuRect.top,
          Math.min(
            0,
            window.innerHeight -
              viewportPadding -
              submenuRect.top -
              panelRect.height,
          ),
        )}px`,
      });
    };

    updateSubmenuPosition();
    window.addEventListener("resize", updateSubmenuPosition);
    window.addEventListener("scroll", updateSubmenuPosition, true);
    return () => {
      window.removeEventListener("resize", updateSubmenuPosition);
      window.removeEventListener("scroll", updateSubmenuPosition, true);
    };
  }, [expanded, panelWidth, parentPosition?.left, parentPosition?.top]);

  if (items.length === 0) {
    return (
      <ComposerQuickMenuItem
        className={className}
        disabled={disabled}
        icon={icon}
        label={label}
      />
    );
  }

  return (
    <div
      className={styles.submenu}
      ref={submenuRef}
      onMouseEnter={() => {
        if (!disabled) {
          setOpen(true);
        }
      }}
      onMouseLeave={() => setOpen(false)}
    >
      <div
        className={classNames(
          styles.item,
          styles.itemClickable,
          disabled && styles.itemDisabled,
          className,
        )}
        aria-disabled={disabled}
        aria-expanded={expanded}
        aria-haspopup="menu"
        onClick={(event) => {
          event.stopPropagation();
          if (!disabled) {
            setOpen((prev) => !prev);
          }
        }}
        onFocus={() => {
          if (!disabled) {
            setOpen(true);
          }
        }}
        onKeyDown={(event) => {
          if (disabled) {
            return;
          }
          if (
            event.key === "Enter" ||
            event.key === " " ||
            event.key === "ArrowRight"
          ) {
            event.preventDefault();
            setOpen(true);
          }
          if (event.key === "Escape") {
            event.preventDefault();
            setOpen(false);
          }
        }}
        role="button"
        tabIndex={disabled ? undefined : 0}
      >
        {icon ? <span className={styles.icon}>{icon}</span> : null}
        <span className={styles.label}>{label}</span>
        <span aria-hidden="true" className={styles.extra}>
          <RightOutlined className={styles.submenuArrow} />
        </span>
      </div>
      {expanded ? (
        <div
          ref={submenuPanelRef}
          className={styles.submenuPanel}
          data-opens-left={submenuOpensLeft || undefined}
          role="menu"
          style={
            submenuPanelStyle ||
            (panelWidth ? { width: panelWidth } : undefined)
          }
        >
          <div className={styles.submenuContent}>
            {items.map((item, index) => (
              <div
                key={(React.isValidElement(item) && item.key) || index}
                className={styles.itemWrap}
              >
                {item}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function ComposerQuickMenu(props: ComposerQuickMenuProps) {
  const { children, disabled = false, triggerLabel } = props;
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [panelStyle, setPanelStyle] = useState<React.CSSProperties | null>(
    null,
  );
  const items = useMemo(
    () => React.Children.toArray(children).filter(Boolean),
    [children],
  );

  const updatePanelPosition = useCallback(() => {
    if (!triggerRef.current || !panelRef.current) {
      return;
    }

    const gap = 12;
    const viewportPadding = 16;
    const triggerRect = triggerRef.current.getBoundingClientRect();
    const panelRect = panelRef.current.getBoundingClientRect();
    const maxLeft = Math.max(
      viewportPadding,
      window.innerWidth - panelRect.width - viewportPadding,
    );
    const left = Math.min(Math.max(triggerRect.left, viewportPadding), maxLeft);
    const openAbove =
      triggerRect.top >= panelRect.height + gap + viewportPadding;
    const maxTop = Math.max(
      viewportPadding,
      window.innerHeight - panelRect.height - viewportPadding,
    );
    const top = openAbove
      ? triggerRect.top - panelRect.height - gap
      : Math.min(triggerRect.bottom + gap, maxTop);

    setPanelStyle({
      left,
      top,
      visibility: "visible",
    });
  }, []);

  useEffect(() => {
    if (disabled) {
      setOpen(false);
    }
  }, [disabled]);

  useEffect(() => {
    if (!open) {
      return;
    }

    const handlePointerDown = (event: MouseEvent) => {
      if (
        event.target instanceof Node &&
        !rootRef.current?.contains(event.target) &&
        !panelRef.current?.contains(event.target)
      ) {
        setOpen(false);
      }
    };

    document.addEventListener("mousedown", handlePointerDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
    };
  }, [open]);

  useLayoutEffect(() => {
    if (!open) {
      setPanelStyle(null);
      return;
    }

    updatePanelPosition();

    const handleReposition = () => {
      updatePanelPosition();
    };

    window.addEventListener("resize", handleReposition);
    window.addEventListener("scroll", handleReposition, true);

    return () => {
      window.removeEventListener("resize", handleReposition);
      window.removeEventListener("scroll", handleReposition, true);
    };
  }, [open, updatePanelPosition]);

  if (items.length === 0) {
    return null;
  }

  const panel =
    open && !disabled
      ? createPortal(
          <div className={styles.portal}>
            <div
              ref={panelRef}
              className={styles.panel}
              style={panelStyle || { visibility: "hidden" }}
              onClick={() => setOpen(false)}
              onChange={() => {
                window.setTimeout(() => setOpen(false), 0);
              }}
            >
              <ParentMenuPositionContext.Provider value={panelStyle}>
                {items.map((item, index) => (
                  <div
                    key={(React.isValidElement(item) && item.key) || index}
                    className={styles.itemWrap}
                  >
                    {item}
                  </div>
                ))}
              </ParentMenuPositionContext.Provider>
            </div>
          </div>,
          document.body,
        )
      : null;

  return (
    <div className={styles.root} ref={rootRef}>
      <button
        ref={triggerRef}
        aria-label={triggerLabel}
        className={styles.trigger}
        disabled={disabled}
        type="button"
        onClick={() => {
          if (!disabled) {
            setOpen((prev) => !prev);
          }
        }}
      >
        <PlusOutlined />
      </button>
      {panel}
    </div>
  );
}
