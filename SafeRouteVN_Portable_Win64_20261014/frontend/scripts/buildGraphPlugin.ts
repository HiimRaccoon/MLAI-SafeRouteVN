import type { Plugin, ResolvedConfig } from "vite";
import { createHash } from "node:crypto";
import { readFile, readdir, writeFile } from "node:fs/promises";
import { resolve, relative, join, isAbsolute } from "node:path";

interface ModuleGraphReader {
  getModuleIds(): Iterable<string>;
  getModuleInfo(id:string): { importedIds: readonly string[]; dynamicallyImportedIds: readonly string[] } | null;
}
export function collectBuildModules(reader:ModuleGraphReader,chunkIds:Iterable<string>,normalize:(id:string)=>string){
  return [...new Set([...reader.getModuleIds(),...chunkIds])].map(id=>{
    const info=reader.getModuleInfo(id);
    return{id:normalize(id),origin:info?'MODULE_GRAPH':'GENERATED_CHUNK',imports:(info?.importedIds??[]).map(normalize),dynamicImports:(info?.dynamicallyImportedIds??[]).map(normalize)};
  });
}

export function buildGraphPlugin(mode: "backend" | "mock"): Plugin {
  let config: ResolvedConfig;
  let graph: Record<string, unknown>;
  const normalize = (id: string) => {
    if (id.startsWith("\0")) return `virtual:${id.slice(1)}`;
    if (!isAbsolute(id)) return id;
    const path = id.replaceAll("\\", "/");
    const dependency = path.indexOf("/node_modules/");
    return dependency >= 0 ? path.slice(dependency + 1) : relative(config.root, id).replaceAll("\\", "/");
  };
  async function files(directory: string, prefix = ""): Promise<string[]> {
    const result: string[] = [];
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      if (entry.isDirectory()) result.push(...await files(join(directory, entry.name), `${prefix}${entry.name}/`));
      else if (entry.isFile()) result.push(prefix + entry.name);
      else throw new Error("Build inventory requires ordinary files.");
    }
    return result.sort();
  }
  return {
    name: "saferoute-build-graph", apply: "build", enforce: "post",
    configResolved(resolved) { config = resolved; },
    generateBundle(_options, bundle) {
      const chunks=Object.values(bundle).filter(item=>item.type==='chunk');
      graph = { schema: "saferoute-build-graph/1", mode,
        modules: collectBuildModules(this,chunks.flatMap(c=>c.moduleIds),normalize),
        chunks: chunks.map(chunk => ({
          file: chunk.fileName, entry: chunk.isEntry, modules: chunk.moduleIds.map(normalize), imports: chunk.imports, dynamicImports: chunk.dynamicImports
        })), assets: []
      };
      this.emitFile({ type: "asset", fileName: "build-graph.json", source: JSON.stringify(graph) });
    },
    async writeBundle() {
      const output = resolve(config.root, config.build.outDir);
      graph.assets = await Promise.all((await files(output)).filter(file => file !== "build-graph.json").map(async file => ({
        file, sha256: createHash("sha256").update(await readFile(join(output, file))).digest("hex")
      })));
      await writeFile(join(output, "build-graph.json"), JSON.stringify(graph, null, 2));
    }
  };
}
