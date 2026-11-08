import {test} from 'node:test';
import assert from 'node:assert/strict';
import {registerLookups,LOOKUP_TOOLS} from './lookups.mjs';
test('lookup tools are reads over the model socket with closed schemas',async()=>{
 const tools=[],calls=[];registerLookups({registerTool:t=>tools.push(t)},async(a,args,priv)=>{calls.push([a,args,priv]);return {ok:1};});
 assert.deepEqual(tools.map(t=>t.name),['places_search','npi_lookup']);
 for(const t of tools)assert.equal(t.parameters.additionalProperties,false);
 await tools[0].execute('1',{query:'CVS',near:'Miami'});assert.deepEqual(calls[0],['places_search',{query:'CVS',near:'Miami'},undefined]);
 await tools[1].execute('1',{last_name:'Rivera',state:'FL'});assert.equal(calls[1][0],'npi_lookup');
 assert.equal(LOOKUP_TOOLS.length,2);
});
