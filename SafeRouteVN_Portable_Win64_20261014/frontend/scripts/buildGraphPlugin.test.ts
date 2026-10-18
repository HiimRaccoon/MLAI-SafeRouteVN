import {expect,it} from 'vitest';
import {collectBuildModules} from './buildGraphPlugin';
it('records generated chunk runtime modules without weakening chunk membership checks',()=>{
 const reader={getModuleIds:()=>['src/main.ts'],getModuleInfo:(id:string)=>id==='src/main.ts'?{importedIds:[],dynamicallyImportedIds:[]}:null};
 const rows=collectBuildModules(reader,['src/main.ts','\0rolldown/runtime.js'],id=>id.replace('\0','virtual:'));
 expect(rows).toEqual([{id:'src/main.ts',origin:'MODULE_GRAPH',imports:[],dynamicImports:[]},{id:'virtual:rolldown/runtime.js',origin:'GENERATED_CHUNK',imports:[],dynamicImports:[]}]);
});
