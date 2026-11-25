# Notebook web UI

React 19 + Vite single-page app for the sandboxed notebook. It talks to the proxy
(`proxy/`) over HTTP and to the MicroVM runtime through the proxy.

```bash
cd web
npm install
npm run dev      # Vite dev server, hot reload
npm run build    # production build into dist/
npx oxlint src   # lint (exit 0 expected)
node --test src/services/__tests__/*.test.mjs   # service unit tests
```

Layout: `src/components` (UI), `src/hooks` (stateful logic), `src/services` (pure
helpers and API clients, covered by the node tests), `src/constants.js` and
`src/config.js` (shared constants and proxy URL). Sidebar panels live in
`src/components/panels`. The Samples panel reads `public/samples/index.json`; the
`showcasedFeatures` pills in that file use the vocabulary in
`src/services/sampleFeatures.js`.

`./local_install.sh` starts the dev server together with the rest of the stack and
passes it `VITE_PROXY_PORT` and `VITE_PROXY_TOKEN`. When you run `npm run dev` yourself,
set the same variables (the proxy port defaults to 8081; the token is sent in the
`X-Proxy-Auth` header).
