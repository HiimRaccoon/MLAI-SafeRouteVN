import { test } from 'node:test';
import assert from 'node:assert/strict';
import { assertBackendGraph, auditBackendBuild } from './audit-backend-build.mjs';
import { mkdtemp, writeFile, unlink, rmdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';

const graph = () => ({ schema: 'saferoute-build-graph/1', mode: 'backend', modules: [
  { id: 'src/app/DispatchContext.tsx', imports: ['src/services/api/createDispatchApi.ts'], dynamicImports: [] },
  { id: 'src/services/api/createDispatchApi.ts', imports: ['src/services/api/BackendDispatchApi.ts'], dynamicImports: ['src/config/mockUnavailable.ts'] },
  { id: 'src/services/api/BackendDispatchApi.ts', imports: [], dynamicImports: [] },
  { id: 'src/config/mockUnavailable.ts', imports: [], dynamicImports: [] }
], chunks: [{ file: 'assets/index.js', entry: true, modules: ['src/app/DispatchContext.tsx'], imports: [], dynamicImports: ['assets/mockUnavailable.js'] },
  { file: 'assets/mockUnavailable.js', entry: false, modules: ['src/config/mockUnavailable.ts'], imports: [], dynamicImports: [] }],
assets: [{ file: 'assets/index.js', sha256: 'a'.repeat(64) }, { file: 'assets/mockUnavailable.js', sha256: 'b'.repeat(64) }] });

test('accepts a recorded backend static and dynamic graph', () => assert.doesNotThrow(() => assertBackendGraph(graph())));
for (const id of ['src/mocks/engine/MockStateEngine.ts','src/services/api/MockDispatchApi.ts','src/mocks/fixtureCatalog.ts','src/mocks/engine/decisionPacks.ts','src/mocks/presentation.ts','src/mocks/data/member2-road-packs.json']) {
  test(`rejects dynamically reachable ${id}`, () => {
    const artifact = graph(); artifact.modules[1].dynamicImports.push(id); artifact.modules.push({ id, imports: [], dynamicImports: [] });
    assert.throws(() => assertBackendGraph(artifact), /mock|offline|pack/i);
  });
}
test('rejects missing graph, wrong build mode and undeclared dynamic chunks', () => {
  assert.throws(() => assertBackendGraph({ mode: 'backend' }));
  const mock = graph(); mock.mode = 'mock'; assert.throws(() => assertBackendGraph(mock));
  const missing = graph(); missing.chunks[0].dynamicImports.push('assets/unknown.js'); assert.throws(() => assertBackendGraph(missing));
});
test('checks actual emitted bytes and refuses extra unrecorded assets', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'saferoute-audit-test-'));
  const artifact = graph();
  artifact.chunks[0].file = 'index.js'; artifact.chunks[0].dynamicImports = ['mockUnavailable.js'];
  artifact.chunks[1].file = 'mockUnavailable.js';
  artifact.assets = artifact.chunks.map(c => ({ file: c.file, sha256: createHash('sha256').update('original').digest('hex') }));
  try {
    for (const a of artifact.assets) await writeFile(join(directory, a.file), 'original');
    await writeFile(join(directory, 'build-graph.json'), JSON.stringify(artifact));
    assert.equal((await auditBackendBuild(directory)).status, 'PASS');
    await writeFile(join(directory, 'index.js'), 'tampered');
    await assert.rejects(auditBackendBuild(directory), /hash mismatch/);
    await writeFile(join(directory, 'index.js'), 'original');
    await writeFile(join(directory, 'unexpected.json'), '{}');
    await assert.rejects(auditBackendBuild(directory), /inventory/);
  } finally {
    for (const file of ['index.js','mockUnavailable.js','build-graph.json','unexpected.json']) await unlink(join(directory, file)).catch(() => {});
    await rmdir(directory);
  }
});
