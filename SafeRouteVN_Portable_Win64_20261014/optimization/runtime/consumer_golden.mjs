// Same neutral corpus as Python, no frontend ownership or SQLite dependency.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {view,jobView,strictJSON} from './reference_consumer.mjs';
const directory=path.dirname(fileURLToPath(import.meta.url));
const examples=process.argv[2]??'examples';const results=[];
try{
 const corpus=strictJSON(fs.readFileSync(path.join(directory,'consumer_golden_corpus.json'),'utf8'));
 for(const c of corpus.cases){
  const value=c.payload??strictJSON(fs.readFileSync(path.join(examples,c.base+'_execution_view.json'),'utf8'));
  if(c.path){let parent=value;for(const key of c.path.slice(0,-1))parent=parent[key];if(c.remove)delete parent[c.path.at(-1)];else parent[c.path.at(-1)]=c.value;}
  let accepted=true,diagnostic=null;try{(c.kind==='job'?jobView:view)(value);}catch(e){accepted=false;diagnostic={code:e.code??'CONTRACT_INVALID',path:e.path??'$',message:String(e)};}
  if(accepted!==c.valid||(c.expected_diagnostic&&Object.entries(c.expected_diagnostic).some(([k,v])=>diagnostic?.[k]!==v)))throw new Error('GOLDEN_PARITY '+c.id);results.push({id:c.id,accepted,diagnostic});
 }
 console.log(JSON.stringify({status:'GOLDEN_PASS',cases:results.length,results}));
}catch(e){console.log(JSON.stringify({status:'GOLDEN_FAIL',diagnostic:String(e)}));process.exitCode=2;}
