import {test} from 'node:test';
import assert from 'node:assert/strict';
import {headlines,quotes,parseRss,newsUrl,resolveSymbol,registerNews,clearQuoteCache} from './news.mjs';

const NOW=Date.parse('2026-11-04T20:15:00Z');
const RSS=`<?xml version="1.0"?><rss><channel><title>Business - Latest - Google News</title>
<item><title>Stocks rally after midterms &amp; oil slides - Reuters</title><link>https://news.google.com/rss/articles/abc?oc=5</link><pubDate>Wed, 04 Nov 2026 19:45:00 GMT</pubDate><description>&lt;a href="x"&gt;ignore&lt;/a&gt;</description><source url="https://www.reuters.com">Reuters</source></item>
<item><title><![CDATA[Fed holds rates — "data dependent" - CNBC]]></title><link>https://news.google.com/rss/articles/def</link><pubDate>Wed, 04 Nov 2026 20:05:00 GMT</pubDate><source url="https://www.cnbc.com">CNBC</source></item>
<item><title>No date story</title><link>javascript:alert(1)</link></item></channel></rss>`;
const CHART=(symbol,price,prev,name)=>JSON.stringify({chart:{result:[{meta:{symbol,shortName:name,currency:'USD',regularMarketPrice:price,chartPreviousClose:prev,regularMarketTime:NOW/1000-300,regularMarketDayHigh:price+5,regularMarketDayLow:price-5,fullExchangeName:'SNP'}}],error:null}});
function fake(routes){const seen=[];const f=async(url,opts)=>{seen.push({url,ua:opts.headers['user-agent']});for(const [re,body,status=200] of routes)if(re.test(url))return {ok:status===200,status,text:async()=>body};return {ok:false,status:404,text:async()=>''};};f.seen=seen;return f;}

test('headlines: newest first, outlet split from title, entities decoded, unsafe links dropped',async()=>{
 const f=fake([[/topic\/BUSINESS/,RSS]]);
 const r=await headlines({topic:'business'},{fetch:f,now:()=>NOW});
 assert.equal(r.source,'Google News');assert.equal(r.count,3);
 assert.deepEqual(r.items[0],{title:'Fed holds rates — "data dependent"',source:'CNBC',published:'2026-11-04T20:05:00.000Z',age_minutes:10,link:'https://news.google.com/rss/articles/def'});
 assert.equal(r.items[1].title,'Stocks rally after midterms & oil slides');assert.equal(r.items[1].source,'Reuters');
 assert.equal(r.items[2].link,undefined);assert.match(f.seen[0].url,/hl=en-US&gl=US&ceid=US%3Aen/);
});
test('news URLs: search, top, Ecuador edition, bad topic rejected',()=>{
 assert.match(newsUrl({query:'NJ bar fee arbitration'}),/rss\/search\?q=NJ%20bar%20fee%20arbitration&/);
 assert.match(newsUrl({}),/^https:\/\/news\.google\.com\/rss\?hl=en-US/);
 assert.match(newsUrl({topic:'world',edition:'EC'}),/topic\/WORLD\?hl=es-419&gl=EC/);
 assert.throws(()=>newsUrl({topic:'gossip'}),/Topic must be/);assert.throws(()=>newsUrl({query:'x'.repeat(201)}));
 assert.equal(parseRss(RSS,{limit:1,now:NOW}).length,1);
});
test.beforeEach(()=>clearQuoteCache());
test('quotes: names map to symbols, change and percent computed, failures reported not estimated',async()=>{
 const f=fake([[/%5EGSPC/,CHART('^GSPC',7698.12,7569.4,'S&P 500')],[/AAPL/,CHART('AAPL',250,250,'Apple Inc.')],[/%5EDJI/,'',500]]);
 const r=await quotes({symbols:['S&P 500','aapl','Dow','sp500']},{fetch:f,now:()=>NOW});
 assert.equal(f.seen.filter(x=>/query1/.test(x.url)).length,3,'duplicates fetched once');
 const [spx,aapl,dow]=r.quotes;
 assert.equal(spx.symbol,'^GSPC');assert.equal(spx.change,128.72);assert.equal(spx.change_pct,1.7);assert.equal(spx.minutes_old,5);assert.equal(spx.as_of,'2026-11-04T20:10:00.000Z');
 assert.equal(aapl.change_pct,0);assert.deepEqual(dow,{symbol:'^DJI',error:'HTTP 500'});
});
test('symbols are validated; only symbols leave the server',async()=>{
 assert.equal(resolveSymbol('10-year'),'^TNX');assert.equal(resolveSymbol('btc-usd'),'BTC-USD');
 assert.throws(()=>resolveSymbol('drop table; --'),/not a ticker/);
 await assert.rejects(quotes({symbols:[]}),/1–12/);
});
test('tools register as optional and turn errors into a clear "do not estimate" message',async()=>{
 const tools=[];registerNews({registerTool:(t,o)=>tools.push([t,o])});
 assert.deepEqual(tools.map(([t,o])=>[t.name,o.optional]),[['news_headlines',true],['market_quotes',true],['youtube_search',true]]);
 const r=await tools[1][0].execute('1',{symbols:['$$$$']});assert.equal(r.isError,true);assert.match(r.content[0].text,/do not estimate/);
});

test('a 429 retries on the second Yahoo host, then serves a recent cached quote',async()=>{
 let n=0;const f=async url=>{n++;if(/query1/.test(url))return {ok:false,status:429,text:async()=>''};return {ok:true,status:200,text:async()=>CHART('^GSPC',7688.62,7569.4,'S&P 500')};};
 const r=await quotes({symbols:['S&P 500']},{fetch:f,now:()=>NOW,wait:async()=>{}});
 assert.equal(r.quotes[0].price,7688.62);assert.equal(n,2);
 const busy=async()=>({ok:false,status:429,text:async()=>''});
 const c=await quotes({symbols:['S&P 500']},{fetch:busy,now:()=>NOW+30000,wait:async()=>{}});assert.equal(c.quotes[0].cached,true);
 const s=await quotes({symbols:['S&P 500']},{fetch:busy,now:()=>NOW+120000,wait:async()=>{}});assert.equal(s.quotes[0].stale_seconds,120);
 clearQuoteCache();const e=await quotes({symbols:['S&P 500']},{fetch:busy,now:()=>NOW,wait:async()=>{}});assert.match(e.quotes[0].error,/rate-limited/);
});
import {apiKey,youtube,FINNHUB_PROXY} from './news.mjs';
test('quotes fall back to Finnhub with their key when Yahoo rate-limits, labelling ETF stand-ins',async()=>{
 const {quotes,clearQuoteCache}=await import('./news.mjs');clearQuoteCache();
 const seen=[];
 const fetchImpl=async(url,opts)=>{seen.push([url,opts?.headers?.['X-Finnhub-Token']]);if(url.includes('yahoo'))return {ok:false,status:429,text:async()=>''};
  return {ok:true,json:async()=>({c:571.2,d:3.1,dp:0.55,h:572,l:566,o:567,pc:568.1,t:1793900000})};};
 const out=await quotes({symbols:['S&P 500','AAPL']},{fetch:fetchImpl,now:()=>1793900000000,wait:async()=>{},key:'test-key-123'});
 const spx=out.quotes.find(q=>q.symbol==='^GSPC');
 assert.equal(spx.quoted_as,'SPY');assert.match(spx.note,/via SPY ETF/);assert.equal(spx.change_pct,0.55);assert.equal(spx.source,'Finnhub');
 assert.ok(seen.some(([u,k])=>u.includes('finnhub.io')&&k==='test-key-123'&&!u.includes('test-key-123')),'key goes in a header, not the URL');
 const none=await quotes({symbols:['AAPL']},{fetch:async()=>({ok:false,status:429,text:async()=>''}),now:()=>1793900000000,wait:async()=>{},key:null});
 assert.match(none.quotes[0].error,/set-api-key.sh finnhub/);
});
test('youtube search needs their key and returns links only',async()=>{
 await assert.rejects(youtube({query:'x'},{key:null}),/set-api-key.sh youtube/);
 const out=await youtube({query:'FEA tutorial',limit:2},{key:'k-12345678',fetch:async()=>({ok:true,json:async()=>({items:[{id:{videoId:'abc'},snippet:{title:'FEA &amp; you',channelTitle:'Prof',publishedAt:'2026-01-01'}}]})})});
 assert.equal(out.videos[0].url,'https://www.youtube.com/watch?v=abc');assert.equal(out.videos[0].title,'FEA & you');
 assert.equal(apiKey('missing','/nonexistent'),null);assert.ok(FINNHUB_PROXY['^GSPC']);
});
