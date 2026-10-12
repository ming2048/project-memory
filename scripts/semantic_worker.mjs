// Version-pinned QMD 2.8.3 SDK. One process per operation; no daemon/GPU.
import {createStore} from '../runtime/qmd/node_modules/@tobilu/qmd/dist/index.js';
import {appendFileSync} from 'node:fs';
let input=''; for await(const chunk of process.stdin) input+=chunk;
const request=JSON.parse(input);
console.log=(...args)=>console.error(...args);
process.env.QMD_FORCE_CPU='1';
process.env.QMD_EMBED_PARALLELISM=request.action==='build'?'4':'1';
process.env.QMD_EMBED_MODEL=request.model;
let store;
try {
  store=await createStore({dbPath:request.db,config:{models:{embed:request.model},collections:request.collections}});
  // The SDK exposes internal for advanced use. Cap only this instance;
  // do not modify the vendor runtime. Verified with the pinned version.
  store.internal.llm.threadsPerContext=async()=>4;
  const updated=await store.update({collections:request.scope});
  let embedded=null;
  // Search has a zero-document embedding budget. The SDK's batch size
  // limits memory, not total work; even one batch can exceed query latency.
  // Only build drains pending vectors, preserving completed work on reruns.
  if(request.action==='build') {
    embedded=await store.embed({maxDocsPerBatch:4,maxBatchBytes:1024*1024,chunkStrategy:'regex',onProgress:p=>{
      if(p.chunksEmbedded%20===0) {
        console.error('[semantic] embedding progress '+JSON.stringify(p));
        appendFileSync(request.db+'.progress.jsonl',JSON.stringify({at:new Date().toISOString(),...p})+'\n');
      }
    }});
  }
  let results=[];
  if(request.action==='search') {
    const hits=await store.searchVector(request.query,{limit:request.limit,collection:request.scope?.length===1?request.scope[0]:request.scope});
    results=hits.map(h=>({file:h.filepath,score:h.score,chunkPos:h.chunkPos,collection:h.collectionName}));
  }
  const status=await store.getStatus();
  await store.close(); store=null;
  process.stdout.write(JSON.stringify({ok:true,updated,embedded,status,pending_embedding:status.needsEmbedding,embedding_budget_docs:request.action==='search'?0:null,results,cpu_only:true,threads:4,parallel_contexts:request.action==='build'?4:1}));
} catch(error) {
  if(store) await store.close().catch(()=>{});
  console.error(String(error));
  process.stdout.write(JSON.stringify({ok:false,error:String(error)}));
  process.exitCode=1;
}
