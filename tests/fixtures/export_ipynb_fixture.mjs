// Prints the .ipynb export of the shared export fixture notebook (for
// tests/web/test_ipynb_nbformat.py). Run: node tests/fixtures/export_ipynb_fixture.mjs [multi]
// `multi` exports the multi-output fixture (several tables / charts / images in one cell).
import { buildIPYNB } from '../../web/src/services/notebookExport.js'
import { fixtureTab, fixtureCells, fixtureCellsMulti } from '../../web/src/services/__tests__/fixtures/exportFixtures.mjs'
const cells = process.argv[2] === 'multi' ? fixtureCellsMulti() : fixtureCells()
process.stdout.write(buildIPYNB(fixtureTab(), cells).content)
