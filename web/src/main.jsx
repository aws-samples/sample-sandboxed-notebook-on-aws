import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
// Shared light + dark component overrides (Apple-style); imported after App so it is
// emitted after all component CSS. Every rule is scoped to
// :root:is([data-theme="light"],[data-theme="dark"]) — ember is never matched.
import './theme-light.css'
import ErrorBoundary from './components/ErrorBoundary.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
