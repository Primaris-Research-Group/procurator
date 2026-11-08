// Which browser task the assistant is working on in each conversation (web 0.17.0). Noted when a task tool runs with
// the conversation's runtime context; read by the usage hook so model cost is charged to that task (task_cost).
const TTL=45*60*1000;
const active=new Map();
export function noteTask(sessionKey,taskId,now=Date.now()){
 if(typeof sessionKey!=='string'||!/^[a-f0-9]{16}$/.test(String(taskId||'')))return;
 active.set(sessionKey,{id:taskId,at:now});
 if(active.size>50)for(const [k,v] of active)if(now-v.at>TTL)active.delete(k);
}
export function forgetTask(sessionKey){active.delete(sessionKey);}
export function taskFor(sessionKey,now=Date.now()){
 const v=active.get(String(sessionKey||''));
 if(!v)return null;
 if(now-v.at>TTL){active.delete(sessionKey);return null;}
 return v.id;
}
