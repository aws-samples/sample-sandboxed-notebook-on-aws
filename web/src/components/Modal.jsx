import { useEffect, useId, useRef } from 'react'
import { createPortal } from 'react-dom'
import './Modal.css'
import { IconX } from './Icons'

/**
 * Backdrop-dismiss that ignores drags. A text-selection drag that STARTS inside
 * the modal (e.g. selecting the name field) but RELEASES over the overlay would
 * otherwise fire the overlay's click and close the modal. We only dismiss when
 * BOTH the mousedown and the click land on the overlay itself.
 */
function useOverlayDismiss(onDismiss) {
  const downOnOverlay = useRef(false)
  return {
    onMouseDown: (e) => { downOnOverlay.current = e.target === e.currentTarget },
    onClick: (e) => {
      if (e.target === e.currentTarget && downOnOverlay.current && onDismiss) onDismiss()
      downOnOverlay.current = false
    },
  }
}

export default function Modal({ title, children, onClose, className = '' }) {
  const overlay = useOverlayDismiss(onClose)
  // Portal to <body> so the fixed overlay is positioned against the viewport and
  // not trapped by an ancestor with a transform/filter (e.g. the sidebar panel or
  // a notebook cell), which would otherwise mis-position or clip the modal.
  return createPortal(
    <div className="modal-overlay" {...overlay}>
      <div className={`modal-card ${className}`.trim()} onClick={e => e.stopPropagation()}>
        {onClose && (
          <button className="modal-close" onClick={onClose} title="Close" aria-label="Close">
            <IconX width={14} height={14} />
          </button>
        )}
        {title && <div className="modal-title">{title}</div>}
        <div className="modal-body">{children}</div>
      </div>
    </div>,
    document.body,
  )
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * Confirmation dialog (role="alertdialog").
 *
 * Accessibility: focus moves to Cancel on open (the safe choice), Tab and Shift+Tab
 * stay inside the dialog, Escape cancels, and focus returns to the element that
 * opened the dialog when it closes. If that element has been removed (for example
 * a deleted cell's button), focus goes to the element matching focusFallbackSelector,
 * if one is given.
 */
export function ConfirmModal({ title, message, confirmLabel = 'Confirm', cancelLabel = 'Cancel', confirmDanger = false, onConfirm, onCancel, children, focusFallbackSelector }) {
  const overlay = useOverlayDismiss(onCancel)
  const titleId = useId()
  const bodyId = useId()
  const cardRef = useRef(null)
  const cancelRef = useRef(null)
  const confirmRef = useRef(null)
  const onCancelRef = useRef(onCancel)
  onCancelRef.current = onCancel
  const fallbackRef = useRef(focusFallbackSelector)
  fallbackRef.current = focusFallbackSelector

  useEffect(() => {
    const anchor = document.activeElement
    const card = cardRef.current
    const first = cancelRef.current || confirmRef.current
    first?.focus()
    const items = () => (card ? Array.from(card.querySelectorAll(FOCUSABLE)) : [])
    // Escape is taken in the capture phase and stopped there, so no other Escape
    // handler (lineage drawer, focus mode, search, version drawer) sees it while
    // the dialog is open. Only the dialog closes.
    const onEscape = (e) => {
      if (e.key !== 'Escape') return
      e.preventDefault()
      e.stopImmediatePropagation()
      onCancelRef.current?.()
    }
    const onKeyDown = (e) => {
      if (e.key !== 'Tab') return
      const list = items()
      if (!list.length) return
      const idx = list.indexOf(document.activeElement)
      if (e.shiftKey && idx <= 0) { e.preventDefault(); list[list.length - 1].focus() }
      else if (!e.shiftKey && (idx === -1 || idx === list.length - 1)) { e.preventDefault(); list[0].focus() }
    }
    // Keep focus inside the dialog even when something outside is clicked.
    const onFocusIn = (e) => {
      if (card && !card.contains(e.target)) (cancelRef.current || confirmRef.current)?.focus()
    }
    window.addEventListener('keydown', onEscape, true)
    window.addEventListener('keydown', onKeyDown)
    document.addEventListener('focusin', onFocusIn)
    return () => {
      window.removeEventListener('keydown', onEscape, true)
      window.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('focusin', onFocusIn)
      // Runs after the DOM commit, so an anchor removed with the dialog's trigger is
      // already disconnected here and the fallback is used instead.
      if (anchor && anchor !== document.body && anchor.isConnected) anchor.focus()
      else if (fallbackRef.current) document.querySelector(fallbackRef.current)?.focus()
    }
  }, [])

  return (
    <div className="modal-overlay" {...overlay}>
      <div
        ref={cardRef}
        className="modal-card"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        aria-describedby={message || children ? bodyId : undefined}
        onClick={e => e.stopPropagation()}
      >
        {title && <div className="modal-title" id={titleId}>{title}</div>}
        <div className="modal-body" id={bodyId}>
          {message && <p className="modal-message">{message}</p>}
          {children}
        </div>
        <div className="modal-actions">
          <button ref={cancelRef} className="modal-btn modal-btn-cancel" onClick={onCancel}>{cancelLabel}</button>
          <button ref={confirmRef} className={`modal-btn ${confirmDanger ? 'modal-btn-danger' : 'modal-btn-confirm'}`} onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  )
}

export function InputModal({ title, fields, onSubmit, onCancel, submitLabel = 'Create' }) {
  const overlay = useOverlayDismiss(onCancel)
  return (
    <div className="modal-overlay" {...overlay}>
      <div className="modal-card" onClick={e => e.stopPropagation()}>
        {title && <div className="modal-title">{title}</div>}
        <form className="modal-body" onSubmit={(e) => { e.preventDefault(); onSubmit() }}>
          {fields}
          <div className="modal-actions">
            <button type="button" className="modal-btn modal-btn-cancel" onClick={onCancel}>Cancel</button>
            <button type="submit" className="modal-btn modal-btn-confirm">{submitLabel}</button>
          </div>
        </form>
      </div>
    </div>
  )
}
