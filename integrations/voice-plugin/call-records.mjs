// Read-only compatibility reader for the pinned 2026.10.9 provider event store.
// Only a matching, already-receipted call is returned to report generation.
const MAX_BYTES=524288;
export function decodeCallEvent(key,meta,lookup){
 if(!Number.isInteger(meta?.chunkCount)||meta.chunkCount<1||meta.chunkCount>12||!Number.isInteger(meta.byteLength)||meta.byteLength<1||meta.byteLength>MAX_BYTES)return null;
 const chunks=[];
 for(let i=0;i<meta.chunkCount;i++){
  const raw=lookup(`${key}:chunk:${String(i).padStart(4,'0')}`);
  if(!raw)return null;
  let c;try{c=JSON.parse(raw);}catch{return null;}
  if(c.index!==i||typeof c.dataBase64!=='string'||c.dataBase64.length>65000||!/^[A-Za-z0-9+/]*={0,2}$/.test(c.dataBase64))return null;
  chunks.push(Buffer.from(c.dataBase64,'base64'));
 }
 const bytes=Buffer.concat(chunks);if(bytes.length!==meta.byteLength)return null;
 try{const call=JSON.parse(bytes.toString('utf8'));return call&&typeof call.callId==='string'&&Array.isArray(call.transcript)?call:null;}catch{return null;}
}
export function selectCallRecord(rows,lookup,callId){
 if(typeof callId!=='string'||!/^[a-f0-9-]{36}$/.test(callId))throw new Error('Invalid call receipt.');
 const entries=[];
 for(const row of rows){try{const meta=JSON.parse(row.value_json);entries.push({...row,meta});}catch{}}
 entries.sort((a,b)=>(b.meta.persistedAt??b.created_at)-(a.meta.persistedAt??a.created_at)||(b.meta.sequence??0)-(a.meta.sequence??0)||b.entry_key.localeCompare(a.entry_key));
 for(const r of entries){const call=decodeCallEvent(r.entry_key,r.meta,lookup);if(call?.callId===callId)return call;}
 return null;
}
export async function readCallRecord(filename,callId){
 const {DatabaseSync}=await import('node:sqlite');
 const db=new DatabaseSync(filename,{readOnly:true});
 try{
  db.exec('BEGIN');
  const rows=db.prepare('SELECT entry_key,value_json,created_at FROM plugin_state_entries WHERE plugin_id=? AND namespace=? ORDER BY created_at DESC LIMIT 1100').all('voice-call','call-record-events');
  const chunks=db.prepare('SELECT value_json FROM plugin_state_entries WHERE plugin_id=? AND namespace=? AND entry_key=?');
  return selectCallRecord(rows,key=>chunks.get('voice-call','call-record-event-chunks',key)?.value_json,callId);
 }finally{db.close();}
}
