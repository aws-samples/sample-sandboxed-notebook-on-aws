import { useSyncExternalStore } from 'react'
import { onMermaidThemeChange } from '../services/markdown'

// The current app theme ('dark' | 'light' | 'ember'). Reuses onMermaidThemeChange because it is
// the app's only data-theme change event (a shared MutationObserver with a subscribe/unsubscribe contract).
const read = () => document.documentElement.getAttribute('data-theme') || 'dark'

export function useDataTheme() {
  return useSyncExternalStore(onMermaidThemeChange, read, () => 'dark')
}
