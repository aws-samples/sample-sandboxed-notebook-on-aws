// Shared fixtures for the export tests (notebookExportHtml, chatExport,
// notebookRoundTrip, exportRender) and tests/web/test_ipynb_nbformat.py.
// The POISON values must never appear in any export.
import { buildOutputsEnvelope } from '../../cellOutputs.js'
const F = '```'

export const POISON = 'SECRET_VALUE_X'
export const FIXED_NOW = new Date('2026-11-13T12:00:00Z')
export const fmtTime = (ts) => (ts ? new Date(ts).toISOString().replace('T', ' ').slice(0, 16) + ' UTC' : '')

const DF_PAYLOAD = JSON.stringify({
  __df_table__: true,
  columns: ['region', 'orders'],
  cells: [['us-east-1', '120'], ['eu-west-1', '80']],
  total_rows: 2, shown_rows: 2, total_cols: 2, shown_cols: 2,
})

const PLOTLY_HTML = '<div class="plotly-chart" data-plotly="true"><div>'
  + '<script type="text/javascript">window.PlotlyConfig = {MathJaxConfig: \'local\'};</script>'
  + '<script charset="utf-8" src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'
  + '<div id="fig-1" class="plotly-graph-div" style="height:100%; width:100%;"></div>'
  + '<script type="text/javascript">window.PLOTLYENV=window.PLOTLYENV || {};'
  + 'if (document.getElementById("fig-1")) { Plotly.newPlot("fig-1", [{"type":"bar","x":["a","b"],"y":[1,2]}], {}, {"responsive": true}) };</script>'
  + '</div></div>'

// 1x1 transparent PNG.
const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='

const LONG_LINE = 'result = compute_something_with_a_really_long_name(' + Array.from({ length: 30 }, (_, i) => `arg_${i}=${i}`).join(', ') + ')'

export function fixtureTab() {
  return {
    id: 900101,
    name: 'Export Fixture',
    description: 'Fixture notebook for the export tests',
    tag: 'Drafts',
    microvmId: 'vm-fixture',
    sessionId: 'sess-fixture',
    // Poison: env values and secrets live on the tab/env store, never in exports.
    _env: { env_vars: { API_KEY: POISON }, secrets: [{ name: 'db', arn: 'arn:aws:secretsmanager:us-east-1:000000000000:secret:db', envVar: 'DB_PASS', value: POISON }] },
    secrets: { DB_PASS: POISON },
    _variables: [{ name: 'token', value: POISON }],
    _dag: {
      schema: 1,
      nodes: { 'cell-py-1': { reads: [], writes: ['region', 'limit'], defined_by: 'cell-py-1' } },
    },
  }
}

export function fixtureCells() {
  return [
    {
      id: 'cell-md-1', type: 'markdown',
      code: `# Sales overview\n\nThis notebook **summarises** orders.\n\n- [x] load data\n- [ ] review\n\n${F}mermaid\ngraph LR\n  A[S3 bucket] --> B[DuckDB]\n  B --> C[Report]\n${F}\n`,
      executionNumber: null, aiExplanation: null, aiExplanationEdited: false,
    },
    {
      id: 'cell-py-1', type: 'code',
      code: `import os\n# @param {"type": "dropdown", "options": ["us-east-1", "eu-west-1"], "label": "Region"}\nregion = "us-east-1"\n# @param {"type": "slider", "min": 1, "max": 100}\nlimit = 25\ntoken = os.environ["API_KEY"]  # env ref only\n${LONG_LINE}\nprint(f"<b>{region}</b> {limit}")`,
      output: '<b>us-east-1</b> 25\n',
      executionNumber: 3, executionTime: 1234, lastExecutedCode: 'import os\nprint(1)',
      peakMemMb: 182.4, peakCpuPct: 63.2, executedAt: '2026-11-13T11:58:00Z',
      aiExplanation: 'Sets the **region** and the row `limit`.', aiExplanationEdited: true, aiExplanationSource: 'user',
      generated: null,
      // Poison: transient web state that must never be exported.
      errorLocals: { token: POISON }, status: 'success', lintDiagnostics: [{ message: POISON }],
    },
    {
      id: 'cell-sql-1', type: 'sql',
      code: "SELECT region, COUNT(*) AS orders\nFROM sales\nWHERE region = 'us-east-1' -- filter\nGROUP BY region",
      html: DF_PAYLOAD, outputVariable: 'df_orders',
      executionNumber: 4, executionTime: 87, executedAt: '2026-11-13T11:59:00Z',
      generated: 'ai',
    },
    {
      id: 'cell-py-plot', type: 'code',
      code: 'import plotly.express as px\nfig = px.bar(x=["a", "b"], y=[1, 2])\nfig',
      html: PLOTLY_HTML, executionNumber: 5, executionTime: 410,
    },
    {
      id: 'cell-py-img', type: 'code',
      code: 'import matplotlib.pyplot as plt\nplt.plot([1, 2, 3])\nplt.show()',
      image: PNG, imageS3Key: 'notebook-outputs/900101/cell-py-img.png', executionNumber: 6,
    },
    {
      id: 'cell-py-err', type: 'code',
      code: 'raise ValueError("bad <input> value")',
      error: 'Traceback (most recent call last):\n  File "<cell>", line 1, in <module>\nValueError: bad <input> value',
      executionNumber: 7, executionTime: 5,
    },
    {
      // A cell id that isn't a valid nbformat id (has a space and a dot).
      id: 'legacy id.8', type: 'markdown',
      code: `## Notes\n\nSee the ${F}sql${F} below:\n\n${F}sql\nSELECT 1\n${F}\n`,
    },
  ]
}

// Trust levels as lineageTrustLevels would return them.
export const fixtureTrust = new Map([['cell-py-1', 'green'], ['cell-sql-1', 'amber'], ['cell-py-err', 'red']])

const user = (content, extra = {}) => ({ role: 'user', content, timestamp: '2026-11-13T10:00:00Z', ...extra })
const ai = (content, extra = {}) => ({ role: 'assistant', content, timestamp: '2026-11-13T10:01:00Z', ...extra })

export function fixtureChat() {
  return [
    user('How many orders per region?', { attachments: [{ name: 'regions.csv', mime: 'text/csv' }] }),
    ai(`_(Inspected variables)_\n\n## Orders per region\n\nRun this:\n\n${F}python\nimport pandas as pd\ndf = pd.read_csv("regions.csv")  # load\nprint(df.groupby("region").size())\n${F}\n\nOr in SQL:\n\n${F}sql\nSELECT region, COUNT(*) FROM sales GROUP BY region\n${F}\n`),
    user('Context (from Workbook Intel):\nThe notebook loads sales data.\n\nTask: Document the flow', { intelCard: { title: 'Document the data flow' } }),
    ai(`### Data flow\n\n${F}markdown\n# Flow\n\n- **Load** from S3\n- Query with DuckDB\n${F}\n\n${F}mermaid\ngraph LR\n  S3 --> DuckDB --> Report\n${F}\n\nInline \`code\` and a <button>Click</button> that must go.`),
    user('Plot it'),
    ai(`${F}\nimport plotly.express as px\npx.bar(x=[1, 2], y=[3, 4])\n${F}\n\nDone.`, { isError: false }),
  ]
}

// The panel's turn store entries (keyed by turn key) are supplied by the tests,
// which compute keys via groupTurns.
export const stubMermaid = async (src) => ({ svg: `<svg data-stub="1" xmlns="http://www.w3.org/2000/svg"><text>${src.length}</text></svg>` })
export const identitySanitize = (h) => h

// ── Multi-output cell (several tables / charts / images + text, see cellOutputs.js) ──
// Kept OUT of fixtureCells() so the existing export snapshots stay byte-identical.
const tbl = (cols, rows, vals) => ({
  __df_table__: true, columns: cols, dtypes: cols.map(() => 'object'),
  cells: vals, total_rows: rows, shown_rows: vals.length, total_cols: cols.length, shown_cols: cols.length,
})
const plotly = (id) => PLOTLY_HTML.split('fig-1').join(id)
const imgKey = (c) => `notebook-outputs/900101/cell-py-multi/${c.repeat(64)}.png`
const MULTI_ITEMS = [
  { t: 'text', text: 'loading orders…\n' },
  { t: 'table', label: 'direct', payload: tbl(['region', 'orders'], 2, [['us-east-1', '120'], ['eu-west-1', '80']]) },
  { t: 'text', text: 'and the upstream view\n' },
  { t: 'table', label: 'upstream', payload: tbl(['region'], 12, [['us-east-1'], ['eu-west-1']]) },
  { t: 'table', payload: tbl(['a', 'b', 'c'], 3, [['1', '2', '3']]) },
  { t: 'plotly', label: 'fig1', html: plotly('fig-m1') },
  { t: 'plotly', html: plotly('fig-m2') },
  { t: 'image', key: imgKey('a'), src: PNG },
  { t: 'image', key: imgKey('b'), src: PNG },
  { t: 'image', key: imgKey('c'), src: PNG },
  { t: 'text', text: 'done\n' },
]
/** The multi-output cell. withSrc: in-memory form (base64 next to the key); false: the stored form (keys only). */
export function multiOutputCell({ withSrc = true } = {}) {
  const items = MULTI_ITEMS.map(it => (it.t === 'image' && !withSrc ? { t: 'image', key: it.key } : it))
  return {
    id: 'cell-py-multi', type: 'code',
    code: 'print("loading orders…")\ndisplay(direct)\nprint("and the upstream view")\ndisplay(upstream)',
    output: 'loading orders…\nand the upstream view\ndone\n',
    html: buildOutputsEnvelope(items), image: null, imageS3Key: imgKey('c'),
    executionNumber: 8, executionTime: 640, executedAt: '2026-11-13T11:59:30Z',
  }
}
/** Multi-output cell followed by an error (what was displayed before it stays above the error). */
export function multiOutputErrorCell() {
  const items = MULTI_ITEMS.slice(0, 4)
  return {
    id: 'cell-py-multi-err', type: 'code', code: 'display(direct)\ndisplay(upstream)\nraise ValueError("boom")',
    output: 'loading orders…\nand the upstream view\n', html: buildOutputsEnvelope(items),
    error: 'ValueError: boom', executionNumber: 9,
  }
}
export function fixtureCellsMulti() {
  return [
    { id: 'cell-md-m', type: 'markdown', code: '# Multiple outputs\n\nOne cell, several outputs.\n' },
    multiOutputCell(),
    multiOutputErrorCell(),
  ]
}
