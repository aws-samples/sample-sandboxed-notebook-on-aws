// Shared check for "is a modal dialog on screen right now".
//
// Non-modal layers (the lineage drawer, focus mode, search, the version drawer)
// call this to stand down while a Modal / ConfirmModal / InputModal is open, so the
// topmost dialog is the only layer that reacts to Escape and Tab. Every modal in
// the app renders a `.modal-overlay` element while it is open.

/** True while any `.modal-overlay` is mounted. Safe to call when `document` is absent. */
export function isModalOpen() {
  return typeof document !== 'undefined' && document.querySelector('.modal-overlay') !== null
}
