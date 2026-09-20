import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from './Icon';
import { tr } from '../../lib/i18n';

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  sub?: ReactNode;
  children: ReactNode;
  /** 底部操作区（按钮行）。 */
  footer?: ReactNode;
  width?: number;
}

// 打开中的弹窗栈：Esc 只关最上面（最后打开）的那个，避免嵌套弹窗被一起关掉
const openModals: object[] = [];

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'iframe',
  'object',
  'embed',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function focusableElements(panel: HTMLElement): HTMLElement[] {
  return Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
    .filter((element) => element.tabIndex >= 0 && !element.closest('[aria-hidden="true"]'));
}

/** 居中对话框（scrim + panel）。 */
export function Modal({ open, onClose, title, sub, children, footer, width = 520 }: ModalProps) {
  const onCloseRef = useRef(onClose);
  const panelRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descriptionId = useId();
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const token = {};
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    openModals.push(token);
    const onKey = (e: KeyboardEvent) => {
      if (openModals[openModals.length - 1] !== token) return;
      if (e.key === 'Escape') {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab') return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusable = focusableElements(panel);
      if (focusable.length === 0) {
        e.preventDefault();
        panel.focus();
        return;
      }
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      const active = document.activeElement;
      if (!panel.contains(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    const frame = window.requestAnimationFrame(() => {
      const panel = panelRef.current;
      if (!panel || openModals[openModals.length - 1] !== token) return;
      const current = document.activeElement;
      if (current instanceof HTMLElement && panel.contains(current)) return;
      const initial = panel.querySelector<HTMLElement>('[autofocus]')
        ?? focusableElements(panel)[0]
        ?? panel;
      initial.focus();
    });
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener('keydown', onKey);
      const i = openModals.indexOf(token);
      if (i >= 0) openModals.splice(i, 1);
      const previous = previousFocusRef.current;
      if (previous?.isConnected) previous.focus();
      previousFocusRef.current = null;
    };
  }, [open]);

  if (!open) return null;
  // 通过 portal 挂到 body：避免祖先的 transform / backdrop-filter（如 .topbar）
  // 成为 fixed 定位的包含块，导致 scrim 不再相对视口居中。
  return createPortal(
    <div className="modal-scrim" onClick={onClose}>
      <div
        ref={panelRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={sub ? descriptionId : undefined}
        tabIndex={-1}
        style={{ width: `min(${width}px, 92vw)` }}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          className="row"
          style={{ padding: '16px 20px', borderBottom: '0.5px solid var(--border)', justifyContent: 'space-between' }}
        >
          <div>
            <div id={titleId} className="row gap8" style={{ fontSize: 14.5, fontWeight: 660 }}>
              {title}
            </div>
            {sub && <div id={descriptionId} style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 3 }}>{sub}</div>}
          </div>
          <button className="icon-btn" onClick={onClose} aria-label={tr('关闭', 'Close')}>
            <Icon name="x" size={15} />
          </button>
        </div>
        <div className="scroll" style={{ padding: '18px 20px', overflowY: 'auto', maxHeight: '64vh' }}>
          {children}
        </div>
        {footer && (
          <div className="row gap8" style={{ padding: '14px 20px', borderTop: '0.5px solid var(--border)', justifyContent: 'flex-end' }}>
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
