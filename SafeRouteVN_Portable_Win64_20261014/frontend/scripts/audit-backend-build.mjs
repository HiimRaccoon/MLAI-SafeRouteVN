import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const forbidden = /(?:^|\/)src\/mocks\/|(?:^|\/)(?:MockStateEngine|MockDispatchApi|offlineRoadPlans|offlineRoadPackCatalog|fixtureCatalog|decisionPacks)\.(?:ts|tsx|js)|(?:^|\/)src\/services\/api\/mockEntry\.ts|scenarios\/fixtures\/|src\/integrations\/member2\/fixtures\/|member2-(?:event-)?road-packs\.json/i;
function safeFile(file) {
  assert(typeof file === 'string' && file && !file.startsWith('/') && !file.includes('\\') && !file.includes(':') && !file.split('/').includes('..'), 'Invalid artifact path');
  assert(!forbidden.test(file), `Mock/offline pack asset forbidden: ${file}`);
}
export function assertBackendGraph(graph) {
  assert.equal(graph.schema, 'saferoute-build-graph/1');
  assert.equal(graph.mode, 'backend', 'Audit requires the backend build');
  assert(Array.isArray(graph.modules) && graph.modules.length > 0, 'Build module graph required');
  assert(Array.isArray(graph.chunks) && graph.chunks.some(c => c.entry), 'Actual entry chunk required');
  assert(Array.isArray(graph.assets) && graph.assets.length > 0, 'Actual asset inventory required');
  const modules = new Set(graph.modules.map(m => m.id));
  const chunks = new Set(graph.chunks.map(c => c.file));
  assert.equal(modules.size, graph.modules.length, 'Duplicate module IDs');
  for (const m of graph.modules) {
    assert(typeof m.id === 'string' && !forbidden.test(m.id), `Mock/offline pack module forbidden: ${m.id}`);
    assert(Array.isArray(m.imports) && Array.isArray(m.dynamicImports), 'Static and dynamic edges required');
    for (const dependency of [...m.imports, ...m.dynamicImports]) {
      assert(!forbidden.test(dependency), `Mock/offline pack dependency forbidden: ${dependency}`);
      assert(modules.has(dependency), `Undeclared module dependency: ${dependency}`);
    }
  }
  for (const c of graph.chunks) {
    safeFile(c.file);
    assert(Array.isArray(c.modules) && c.modules.every(id => modules.has(id)), 'Chunk module membership required');
    assert(Array.isArray(c.imports) && Array.isArray(c.dynamicImports), 'Chunk edges required');
    for (const dependency of [...c.imports, ...c.dynamicImports]) assert(chunks.has(dependency), `Undeclared static/dynamic chunk: ${dependency}`);
  }
  const assets = new Set();
  for (const a of graph.assets) { safeFile(a.file); assert(!assets.has(a.file)); assets.add(a.file); assert(/^[a-f0-9]{64}$/.test(a.sha256), 'Asset hash required'); }
  assert([...chunks].every(file => assets.has(file)), 'Every chunk must be an inventoried asset');
}
export async function listArtifactFiles(directory, prefix = '') {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const file = prefix + entry.name;
    if (entry.isDirectory()) result.push(...await listArtifactFiles(join(directory, entry.name), file + '/'));
    else { assert(entry.isFile(), 'Build artifacts must be ordinary files'); result.push(file); }
  }
  return result.sort();
}
export async function auditBackendBuild(directory) {
  const manifestBytes = await readFile(join(directory, 'build-graph.json'));
  const graph = JSON.parse(manifestBytes); assertBackendGraph(graph);
  const files = (await listArtifactFiles(directory)).filter(file => file !== 'build-graph.json');
  assert.deepEqual(files, graph.assets.map(a => a.file).sort(), 'Actual files must match the recorded inventory');
  let bytes = 0;
  for (const asset of graph.assets) {
    const contents = await readFile(join(directory, asset.file)); bytes += contents.length;
    assert.equal(createHash('sha256').update(contents).digest('hex'), asset.sha256, `Artifact hash mismatch: ${asset.file}`);
  }
  return { schema_version: 'saferoute-backend-build-audit/1', status: 'PASS', mode: graph.mode,
    graph_sha256: createHash('sha256').update(manifestBytes).digest('hex'), modules: graph.modules.length,
    chunks: graph.chunks.length, assets: graph.assets.length, bytes, static_dynamic_graph_checked: true };
}
if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  const directory = resolve(process.argv[2] ?? fileURLToPath(new URL('../dist', import.meta.url)));
  console.log(JSON.stringify(await auditBackendBuild(directory), null, 2));
}
