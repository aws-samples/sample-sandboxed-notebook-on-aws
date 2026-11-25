import { Component } from 'react'
import { errorDetails, saveLastError } from '../services/errorDetails'

// Root boundary (no `scope`): full-screen error with Reload. Scoped boundary
// (`scope="sidebar"` / `scope="connect"`): an inline box with Try again, so a
// panel that throws does not take the whole app down. Both show the message and
// a "Copy details" button (message, stack, component stack).
export default class ErrorBoundary extends Component {
  state = { hasError: false, error: null, details: '', copied: false }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, info) {
    console.error(`[ErrorBoundary${this.props.scope ? `:${this.props.scope}` : ''}] Uncaught error:`, error, info)
    const details = errorDetails(error, info, { scope: this.props.scope || 'app', url: globalThis.location?.href || '' })
    saveLastError(details)
    this.setState({ details })
  }

  copyDetails = () => {
    const text = this.state.details || errorDetails(this.state.error, null, { scope: this.props.scope || 'app' })
    navigator.clipboard?.writeText(text).then(() => this.setState({ copied: true }), () => {})
  }

  render() {
    if (this.state.hasError) {
      const message = this.state.error?.message || 'An unexpected error occurred in the application.'
      const copyBtn = (
        <button className="eb-copy" onClick={this.copyDetails}
          style={{ marginTop: '0.75rem', padding: '6px 12px', background: 'transparent', color: '#9aa4b5', border: '1px solid #3a4150', borderRadius: '6px', cursor: 'pointer', fontSize: '12px' }}>
          {this.state.copied ? 'Copied' : 'Copy details'}
        </button>
      )
      if (this.props.scope) {
        return (
          <div className="eb-root eb-scoped" role="alert" style={{ padding: '1rem', margin: '0.75rem', border: '1px solid #ff5c5c55', borderRadius: '8px', color: '#ccc', fontFamily: 'system-ui', fontSize: '13px' }}>
            <div className="eb-title" style={{ color: '#ff5c5c', fontWeight: 600, marginBottom: '0.25rem' }}>This panel hit an error</div>
            <div className="eb-msg" style={{ color: '#9aa4b5', lineHeight: 1.5, wordBreak: 'break-word' }}>{message}</div>
            <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
              <button className="eb-btn" onClick={() => this.setState({ hasError: false, error: null, details: '', copied: false })}
                style={{ marginTop: '0.75rem', padding: '6px 12px', background: '#5b8cff', color: '#fff', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '12px', fontWeight: 600 }}>
                Try again
              </button>
              {copyBtn}
            </div>
          </div>
        )
      }
      return (
        <div className="eb-root" style={{ padding: '3rem', textAlign: 'center', color: '#ccc', fontFamily: 'system-ui', background: '#0b0e14', minHeight: '100vh', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
          <h2 className="eb-title" style={{ color: '#ff5c5c', marginBottom: '0.5rem' }}>Something went wrong</h2>
          <p className="eb-msg" style={{ color: '#9aa4b5', maxWidth: 500, lineHeight: 1.6 }}>{message}</p>
          <button
            className="eb-btn"
            onClick={() => window.location.reload()}
            style={{ marginTop: '1.5rem', padding: '10px 20px', background: '#5b8cff', color: '#fff', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '14px', fontWeight: 600 }}
          >
            Reload Application
          </button>
          {copyBtn}
        </div>
      )
    }
    return this.props.children
  }
}
