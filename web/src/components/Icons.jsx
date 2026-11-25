/* ============================================================
   SVG Icon Components (Lucide-inspired, 18x18, stroke-based)
   All icons use currentColor for easy theming
   ============================================================ */
import { archLabel } from '../services/costRates'
import { archKey } from '../services/vmSort'

const iconProps = {
  width: 16,
  height: 16,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
}

export function IconEraser(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M7 21h10" />
      <path d="M5.5 13.5L12 7l5 5-6.5 6.5a2.12 2.12 0 0 1-3 0L5.5 16.5a2.12 2.12 0 0 1 0-3z" />
    </svg>
  )
}

export function IconPencil(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
      <path d="m15 5 4 4" />
    </svg>
  )
}

export function IconBraces(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1" />
      <path d="M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1" />
    </svg>
  )
}

export function IconPlay(props) {
  return (
    <svg {...iconProps} {...props}>
      <polygon points="6 3 20 12 6 21 6 3" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function IconPlayAll(props) {
  return (
    <svg {...iconProps} {...props}>
      <polygon points="5 3 15 12 5 21 5 3" fill="currentColor" stroke="none" />
      <polygon points="13 3 23 12 13 21 13 3" fill="currentColor" stroke="none" />
    </svg>
  )
}

// Maximize / expand-to-fullscreen — four corner arrows pointing outward.
export function IconExpand(props) {
  return (
    <svg {...iconProps} {...props}>
      <polyline points="15 3 21 3 21 9" />
      <polyline points="9 21 3 21 3 15" />
      <line x1="21" y1="3" x2="14" y2="10" />
      <line x1="3" y1="21" x2="10" y2="14" />
    </svg>
  )
}

// Minimize / collapse-back — four corner arrows pointing inward.
export function IconCollapse(props) {
  return (
    <svg {...iconProps} {...props}>
      <polyline points="4 14 10 14 10 20" />
      <polyline points="20 10 14 10 14 4" />
      <line x1="14" y1="10" x2="21" y2="3" />
      <line x1="3" y1="21" x2="10" y2="14" />
    </svg>
  )
}

/* Lineage: two upstream nodes feeding one result node. Shared by the Outline row
   Lineage button and the "How was this computed?" trigger so both read as the
   same feature. */
export function IconLineage(props) {
  return (
    <svg {...iconProps} {...props}>
      <circle cx="6" cy="6" r="2.4" />
      <circle cx="18" cy="6" r="2.4" />
      <circle cx="12" cy="18" r="2.4" />
      <path d="M7.7 7.7 10.6 15.8M16.3 7.7 13.4 15.8" />
    </svg>
  )
}

export function IconPlus(props) {
  return (
    <svg {...iconProps} {...props}>
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  )
}

export function IconPackage(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M16.5 9.4 7.55 4.24" />
      <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      <polyline points="3.29 7 12 12 20.71 7" />
      <line x1="12" y1="22" x2="12" y2="12" />
    </svg>
  )
}

export function IconSave(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
      <polyline points="17 21 17 13 7 13 7 21" />
      <polyline points="7 3 7 8 15 8" />
    </svg>
  )
}

export function IconFolderOpen(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2" />
    </svg>
  )
}

// Archive box — signals a notebook that's been put away (closed/DB-only), as
// opposed to a live/open one. Shape-based cue so it reads as "closed" even at a
// glance, independent of the muted color also used for a disconnected VM dot.
export function IconArchive(props) {
  return (
    <svg {...iconProps} {...props}>
      <rect x="3" y="4" width="18" height="4" rx="1" />
      <path d="M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8" />
      <path d="M10 12h4" />
    </svg>
  )
}

export function IconTrash(props) {
  return (
    <svg {...iconProps} {...props}>
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      <line x1="10" y1="11" x2="10" y2="17" />
      <line x1="14" y1="11" x2="14" y2="17" />
    </svg>
  )
}

export function IconSparkles(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" />
      <path d="M20 3v4" />
      <path d="M22 5h-4" />
    </svg>
  )
}

// Circled "?" (thin stroke, same grid as the other toolbar icons). Used for
// "Explain this code" in the cell toolbar and the focus-mode toolbar.
export function IconExplain(props) {
  return (
    <svg {...iconProps} {...props}>
      <circle cx="12" cy="12" r="10" />
      <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
      <path d="M12 17h.01" />
    </svg>
  )
}

export function IconCheck(props) {
  return (
    <svg {...iconProps} {...props}>
      <polyline points="20 6 9 17 4 12" />
    </svg>
  )
}

export function IconX(props) {
  return (
    <svg {...iconProps} {...props}>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}

export function IconSun(props) {
  return (
    <svg {...iconProps} {...props}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
    </svg>
  )
}

export function IconMoon(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" />
    </svg>
  )
}

export function IconFlame(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z" />
    </svg>
  )
}

export function IconCode(props) {
  return (
    <svg {...iconProps} {...props}>
      <polyline points="16 18 22 12 16 6" />
      <polyline points="8 6 2 12 8 18" />
    </svg>
  )
}

export function IconTerminal(props) {
  return (
    <svg {...iconProps} {...props}>
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  )
}

export function IconUpload(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="17 8 12 3 7 8" />
      <line x1="12" y1="3" x2="12" y2="15" />
    </svg>
  )
}

export function IconDownload(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  )
}

export function IconRefresh(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />
      <path d="M21 3v5h-5" />
      <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />
      <path d="M3 21v-5h5" />
    </svg>
  )
}

export function IconServer(props) {
  return (
    <svg {...iconProps} {...props}>
      <rect x="2" y="2" width="20" height="8" rx="2" ry="2" />
      <rect x="2" y="14" width="20" height="8" rx="2" ry="2" />
      <line x1="6" y1="6" x2="6.01" y2="6" />
      <line x1="6" y1="18" x2="6.01" y2="18" />
    </svg>
  )
}

export function IconZap(props) {
  return (
    <svg {...iconProps} {...props}>
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function IconFile(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  )
}

export function IconChevronDown(props) {
  return (
    <svg {...iconProps} width={12} height={12} {...props}>
      <polyline points="6 9 12 15 18 9" />
    </svg>
  )
}

export function IconChevronRight(props) {
  return (
    <svg {...iconProps} width={12} height={12} {...props}>
      <polyline points="9 6 15 12 9 18" />
    </svg>
  )
}

export function IconLoader(props) {
  return (
    <svg {...iconProps} {...props} className={`icon-spin ${props.className || ''}`}>
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
    </svg>
  )
}

export function IconNotebook(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M2 6h4M2 10h4M2 14h4M2 18h4" />
      <rect x="4" y="2" width="16" height="20" rx="2" />
      <line x1="12" y1="2" x2="12" y2="22" />
    </svg>
  )
}

export function IconDatabase(props) {
  return (
    <svg {...iconProps} {...props}>
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
      <path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3" />
    </svg>
  )
}

export function IconBucket(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M4 7h16" />
      <path d="M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-12" />
      <path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3" />
      <path d="M10 12h4" />
    </svg>
  )
}

export function IconCloud(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z" />
    </svg>
  )
}

export function IconTable(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M9 3H5a2 2 0 0 0-2 2v4m6-6h10a2 2 0 0 1 2 2v4M9 3v18m0 0h10a2 2 0 0 0 2-2V9M9 21H5a2 2 0 0 1-2-2V9m0 0h18" />
    </svg>
  )
}

export function IconStop(props) {
  return (
    <svg {...iconProps} {...props}>
      <rect x="6" y="6" width="12" height="12" rx="1" />
    </svg>
  )
}

export function IconGripVertical(props) {
  return (
    <svg {...iconProps} {...props}>
      <circle cx="9" cy="5" r="1" fill="currentColor" stroke="none" />
      <circle cx="15" cy="5" r="1" fill="currentColor" stroke="none" />
      <circle cx="9" cy="12" r="1" fill="currentColor" stroke="none" />
      <circle cx="15" cy="12" r="1" fill="currentColor" stroke="none" />
      <circle cx="9" cy="19" r="1" fill="currentColor" stroke="none" />
      <circle cx="15" cy="19" r="1" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function IconSearch(props) {
  return (
    <svg {...iconProps} {...props}>
      <circle cx="11" cy="11" r="8" />
      <path d="M21 21l-4.35-4.35" />
    </svg>
  )
}

export function IconChevronUp(props) {
  return (
    <svg {...iconProps} {...props}>
      <polyline points="18 15 12 9 6 15" />
    </svg>
  )
}

export function IconLogs(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M4 6h16" /><path d="M4 10h16" /><path d="M4 14h10" /><path d="M4 18h8" /><circle cx="18" cy="16" r="3" /><path d="M18 14v2l1 1" />
    </svg>
  )
}

export function IconIntel(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M12 2a7 7 0 0 0-7 7c0 2.38 1.19 4.47 3 5.74V17a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2.26c1.81-1.27 3-3.36 3-5.74a7 7 0 0 0-7-7z" /><path d="M9 21h6" /><path d="M9 18h6" /><line x1="12" y1="2" x2="12" y2="6" /><line x1="12" y1="9" x2="12" y2="13" />
    </svg>
  )
}

export function IconBarChart(props) {
  return (
    <svg {...iconProps} {...props}>
      <line x1="6" y1="20" x2="6" y2="16" />
      <line x1="12" y1="20" x2="12" y2="10" />
      <line x1="18" y1="20" x2="18" y2="4" />
    </svg>
  )
}

export function IconChartLine(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M3 3v18h18" />
      <path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3" />
    </svg>
  )
}

export function IconAlertTriangle(props) {
  return (
    <svg {...iconProps} {...props}>
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  )
}

/* Connect dialog actions: Launch (rocket, the old dialog's 🚀) and Restore
   (one counter-clockwise arrow, the old dialog's ♻). Decorative: the button
   label stays the accessible name. */
export function IconRocket(props) {
  return (
    <svg {...iconProps} strokeWidth={1.75} aria-hidden="true" focusable="false" {...props}>
      <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" />
      <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" />
      <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0" />
      <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
    </svg>
  )
}
export function IconRestore(props) {
  return (
    <svg {...iconProps} strokeWidth={1.75} aria-hidden="true" focusable="false" {...props}>
      <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
      <path d="M3 3v5h5" />
    </svg>
  )
}

/* CPU architecture glyphs. Different silhouettes so they tell apart without
   colour: ARM64 = square chip with pins on all 4 sides and an "A" chevron;
   x86_64 = wider DIP-style chip with pins on the left/right only and an "×". */
export function IconArchArm(props) {
  return (
    <svg {...iconProps} {...props}>
      <rect x="6" y="6" width="12" height="12" rx="1.5" />
      <path d="M10 2v4M14 2v4M10 18v4M14 18v4M2 10h4M2 14h4M18 10h4M18 14h4" />
      <path d="M9.5 15 12 9.5l2.5 5.5" />
    </svg>
  )
}
export function IconArchX86(props) {
  return (
    <svg {...iconProps} {...props}>
      <rect x="5" y="4" width="14" height="16" rx="1.5" />
      <path d="M1.5 8H5M1.5 12H5M1.5 16H5M19 8h3.5M19 12h3.5M19 16h3.5" />
      <path d="m9.5 9.5 5 5M14.5 9.5l-5 5" />
    </svg>
  )
}

/* The one place an architecture is drawn: icon + label (default archLabel(arch)).
   The icon is aria-hidden with no <title>, and the badge adds no text nodes of
   its own (spacing is a CSS margin on the svg, see .arch-badge in index.css),
   so innerText is exactly the label. iconOnly
   renders just the decorative icon, for use next to a control that already
   names the arch in text. */
export function ArchBadge({ arch, label, size = 12, iconOnly = false, className }) {
  // Unknown arch: no icon (never the ARM glyph by default), text "unknown".
  if (!arch && label == null) {
    return iconOnly ? null : <span className={`arch-badge arch-badge-unknown${className ? ` ${className}` : ''}`}>{archLabel(arch)}</span>
  }
  const key = archKey(arch)
  const Icon = key === 'x86_64' ? IconArchX86 : IconArchArm
  const icon = <Icon width={size} height={size} aria-hidden="true" focusable="false" />
  const cls = `arch-badge arch-badge-${key}${className ? ` ${className}` : ''}`
  if (iconOnly) return <span className={cls} aria-hidden="true">{icon}</span>
  return <span className={cls}>{icon}{label ?? archLabel(arch)}</span>
}
