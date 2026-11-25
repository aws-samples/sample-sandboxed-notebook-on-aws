import { useEffect, useRef, useState } from 'react'
import { useDraggable } from '../hooks/useDraggable'
import { DEFAULT_MEMORY_MIB } from '../constants'
import './ExecutionHud.css'

/**
 * ExecutionHud — a transient, bottom-center floating pill that surfaces the
 * VM's live CPU / MEM / DISK while cells are executing, then fades out shortly
 * after the run finishes. This makes the three resource metrics visible even
 * when the VM side panel is collapsed.
 *
 * Data source is the SAME shared `vmMetrics[microvmId]` object the side panel
 * uses, so both stay in lockstep. Liveness during a run comes from the
 * isExecuting-gated poll in useNotebookCells — this component is purely a view.
 *
 * Props:
 *   isExecuting — true while a cell/Run-All is running
 *   metrics     — vmMetrics[microvmId]: { cpu:{percent}, memory:{percent,used_mb}, disk:{percent} }
 *   memoryMib   — the VM's baseline memory (for the MEM burst badge threshold)
 */
const LINGER_MS = 1200  // keep showing the final snapshot briefly after a run ends

export default function ExecutionHud({ isExecuting, metrics, memoryMib = DEFAULT_MEMORY_MIB }) {
  // Stay visible through the run AND for a short linger afterwards so the user
  // sees the final values settle before the pill disappears.
  const [visible, setVisible] = useState(false)
  const lingerTimer = useRef(null)

  // Draggable + position persisted across runs and notebooks (shared key).
  const { position, dragging, onPointerDown } = useDraggable('microvm.execHud.pos')

  useEffect(() => {
    if (isExecuting) {
      if (lingerTimer.current) { clearTimeout(lingerTimer.current); lingerTimer.current = null }
      setVisible(true)
    } else if (visible) {
      lingerTimer.current = setTimeout(() => setVisible(false), LINGER_MS)
    }
    return () => { if (lingerTimer.current) clearTimeout(lingerTimer.current) }
  }, [isExecuting, visible])

  if (!visible) return null

  const cpu = Math.max(0, metrics?.cpu?.percent || 0)
  const memPct = Math.max(0, metrics?.memory?.percent || 0)
  const memUsed = metrics?.memory?.used_mb || 0
  const disk = Math.max(0, metrics?.disk?.percent || 0)

  const cpuBurst = cpu > 30
  const memBurst = memUsed > (memoryMib || DEFAULT_MEMORY_MIB)

  // While executing we pulse; during the linger we hold the final frame steady.
  const stateClass = isExecuting ? 'exec-hud-running' : 'exec-hud-settling'

  // Once the user has dragged it, pin to an explicit top-left position and drop
  // the default bottom-center anchor (and its translate-based animations).
  const moved = !!position
  const style = moved
    ? { left: `${position.x}px`, top: `${position.y}px`, right: 'auto', bottom: 'auto', transform: 'none' }
    : undefined

  return (
    <div
      className={`exec-hud ${stateClass}${moved ? ' exec-hud-moved' : ''}${dragging ? ' exec-hud-dragging' : ''}`}
      style={style}
      role="status"
      aria-live="polite"
      onPointerDown={onPointerDown}
      title="Drag to reposition"
    >
      <span className="exec-hud-title-group">
        <span className="exec-hud-pulse" aria-hidden="true">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
          </svg>
        </span>
        <span className="exec-hud-title">MicroVM&nbsp;·&nbsp;Live</span>
      </span>

      <span className="exec-hud-divider" aria-hidden="true" />

      <ExecMetric label="CPU" pct={cpu} fillClass="vm-metric-cpu" burst={cpuBurst}
                  burstTitle="Using burst vCPU — beyond allocated baseline" />
      <ExecMetric label="MEM" pct={memPct} fillClass="vm-metric-mem" burst={memBurst}
                  burstTitle={`Using ${(memUsed - (memoryMib || DEFAULT_MEMORY_MIB)).toFixed(0)} MB above baseline — burst billing active`} />
      <ExecMetric label="DISK" pct={disk} fillClass="vm-metric-disk" burst={false} />
    </div>
  )
}

function ExecMetric({ label, pct, fillClass, burst, burstTitle }) {
  return (
    <div className="exec-hud-metric">
      <span className="exec-hud-label">{label}</span>
      <div className="exec-hud-bar">
        <div className={`exec-hud-fill ${fillClass}`} style={{ width: `${Math.min(pct, 100)}%` }} />
      </div>
      <span className="exec-hud-value">
        {pct.toFixed(0)}%
        {burst && <span className="exec-hud-burst" title={burstTitle}>🔥</span>}
      </span>
    </div>
  )
}
