import { IconX, ArchBadge } from '../Icons'
import PanelExpandButton from './PanelExpandButton'

export default function AboutPanel({ onClose, expanded, onToggleExpand }) {
  return (
    <div className="sidebar-panel-content">
      <div className="sidebar-panel-header">
        <span className="sidebar-panel-title">About</span>
        <PanelExpandButton expanded={expanded} onToggleExpand={onToggleExpand} />
        <button className="sidebar-panel-close" onClick={onClose} title="Close panel"><IconX width={12} height={12} /></button>
      </div>
      <div className="about-panel">
        <div className="about-logo">
          <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
            <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" fill="rgba(137,180,250,0.25)" stroke="#89b4fa"/>
          </svg>
        </div>
        <h2 className="about-title">Sandboxed Notebook on AWS</h2>
        <p className="about-subtitle">AI-Powered Python &amp; SQL Notebooks on Serverless Firecracker Sandboxes</p>

        <div className="about-badges">
          <span className="about-badge">🐍 Python</span>
          <span className="about-badge">🗄️ SQL</span>
          <span className="about-badge">✨ AI</span>
          <span className="about-badge">🔥 Firecracker</span>
        </div>

        <div className="about-info">
          <div className="about-row"><span className="about-row-key">Version</span><span className="about-row-val">1.0.0</span></div>
          <div className="about-row"><span className="about-row-key">Platform</span><span className="about-row-val">AWS Lambda MicroVMs</span></div>
          <div className="about-row"><span className="about-row-key">AI Engine</span><span className="about-row-val">Strands SDK + Bedrock</span></div>
          <div className="about-row"><span className="about-row-key">Runtime</span><span className="about-row-val">Python 3.11 · <ArchBadge arch="arm64" /> / <ArchBadge arch="x86_64" /></span></div>
        </div>

        <div className="about-team">
          <span className="about-team-label">Developed by</span>
          <span className="about-team-name">AWS Startup SA Team</span>
          <span className="about-team-love">Built with <span className="about-heart">❤</span></span>
        </div>

        <div className="about-footer">
          <a className="about-github" href="https://github.com/aws-samples/sample-sandboxed-notebook-on-aws" target="_blank" rel="noopener noreferrer">
            <svg width={14} height={14} viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/></svg>
            View on GitHub
          </a>
          <div className="about-legal">
            <span>© Amazon.com, Inc. or its affiliates. All Rights Reserved.</span>
            <span>MIT-0 License</span>
          </div>
        </div>
      </div>
    </div>
  )
}
