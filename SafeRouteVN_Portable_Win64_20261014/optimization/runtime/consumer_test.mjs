import fs from 'node:fs';
import {strictJSON,exactInt,view} from './reference_consumer.mjs';
const vectors=JSON.parse(fs.readFileSync(new URL('./vectors.json',import.meta.url),'utf8'));
let passed=0;
for(const v of vectors.raw_vectors){let ok=true;try{strictJSON(v.raw);}catch(e){ok=false;}if(ok!==v.valid)throw new Error('raw vector drift');passed++;}
for(const v of vectors.exact_integer_vectors){let ok=true;try{exactInt(v.value);}catch(e){ok=false;}if(ok!==v.valid)throw new Error('int vector drift');passed++;}
console.log(JSON.stringify({status:'PORTABLE_JS_PASS',cases:passed,native_runtime_certified:false}));
