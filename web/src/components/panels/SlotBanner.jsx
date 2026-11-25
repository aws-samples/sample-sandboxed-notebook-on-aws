// Panel-wide action banner read from services/bannerStore.js (sandbox ready or
// failed, import finished). The main action activates the notebook the banner
// points at (payload.tabId), which shows its Notebook view (or its Connect panel
// after a failure). Dismiss, the action and the 10 s auto-dismiss remove the banner
// from every slot it was published to, so it never comes back in another panel.
import ActionBanner from '../ActionBanner'
import { SLOTS } from '../../services/actionBanner'
import { useSlotBanner, clear } from '../../services/bannerStore'

export default function SlotBanner({ slot, tabs = [], onSelectTab }) {
  const banner = useSlotBanner(slot)
  if (!banner) return null
  const clearEverywhere = () => {
    for (const s of Object.values(SLOTS)) clear(s, banner.id)
  }
  const onAction = () => {
    clearEverywhere()
    // payload.openPanel (e.g. 'packages' for a restore report with failed
    // packages): also open that sidebar panel (Sidebar 'open-sidebar-panel').
    const panel = banner.payload?.openPanel
    if (panel) {
      try { window.dispatchEvent(new CustomEvent('open-sidebar-panel', { detail: panel })) } catch {}
    }
    const tabId = banner.payload?.tabId
    if (tabId == null || !onSelectTab) return
    const tab = tabs.find(t => String(t.id) === String(tabId))
    if (tab) onSelectTab(tab.id)
  }
  return (
    <ActionBanner
      banner={banner}
      className="slot-banner"
      onAction={onAction}
      onDismiss={clearEverywhere}
      dismissLabel="Dismiss message"
    />
  )
}
