// Places and clinician lookups (0.25.0): places_search (Google Places, or OpenStreetMap without a key) and npi_lookup (CMS NPI
// Registry). Reads only; results are listing data, never instructions. Broker side: integrations/web/lookups.py.
const str=(description,maxLength)=>({type:'string',description,maxLength});
const out=r=>({content:[{type:'text',text:JSON.stringify(r)}]});
const err=e=>({content:[{type:'text',text:String(e?.message||e)}],isError:true});
export const LOOKUP_TOOLS=[
 ['places_search','Find a business or place and its listing: name, address, phone (E.164), opening hours, website and map link. Use it BEFORE calling, booking or visiting a business, so the call or booking starts from a verified listing instead of a guessed number; and when the owner asks where something is or whether it is open. Prefer the business\'s own website for the final phone number on important calls.','places_search',
  {type:'object',additionalProperties:false,required:['query'],properties:{query:str('What to find, e.g. "CVS pharmacy" or "Joe\'s Stone Crab".',200),near:str('City, neighborhood or address to search near, e.g. "Coral Gables, FL".',120),limit:{type:'integer',minimum:1,maximum:8}}}],
 ['npi_lookup','Look up a clinician or practice in the public CMS NPI Registry: NPI number, specialty, practice address and phone, active status. Use it to verify a doctor or clinic and find the practice before booking or calling. Public registry data only; give a last name (with first name, state or city) or an organization name, or the 10-digit NPI.','npi_lookup',
  {type:'object',additionalProperties:false,properties:{last_name:str('Clinician last name.',60),first_name:str('First name.',40),organization:str('Practice or organization name.',120),
   state:str('Two-letter state, e.g. FL.',2),city:str('City.',60),postal_code:str('ZIP code.',10),specialty:str('Specialty words, e.g. "Dermatology".',80),number:str('10-digit NPI.',10),
   starts_with:{type:'boolean',description:'Match names that start with the given text.'},limit:{type:'integer',minimum:1,maximum:10}}}]];
export function registerLookups(api,call){
 for(const [name,description,action,parameters] of LOOKUP_TOOLS)api.registerTool({name,label:name,description,parameters,async execute(_id,args){try{return out(await call(action,args||{}));}catch(e){return err(e);}}});
}
