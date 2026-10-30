import test from 'node:test';
import assert from 'node:assert/strict';
import {explorerTests,filterExplorerTests,renderTestExplorer,renderTestInspector,ROW_LIMIT} from '../ci-test-explorer.js';

const logTest=(id,name,status,extra={})=>({id,name,status,suite:'pkg',duration_ms:0,timed:false,step_id:'31',step:'unit',line_start:10,line_end:14,...extra});
const logReport=(tests,extra={})=>({source:'log_parse',runners:['go'],run_number:7,branch:'main',provider_url:'https://ci.example.org',provider_repo_id:5,repository:'org/app',summary:{total:tests.length},tests,...extra});
const junitReport=(tests,extra={})=>({run_number:6,branch:'main',provider_url:'https://ci.example.org',provider_repo_id:5,repository:'org/app',summary:{total:tests.length},tests,...extra});
const ex=(reports,extra={})=>({repo:'k',label:'org/app',reports,search:'',status:'all',sort:'failures',selected:'',slice:null,sliceError:'',history:null,historyError:'',...extra});

test('tests carry where they came from and filter by text, status and sort',()=>{
 const tests=explorerTests([{label:'main',report:logReport([logTest('a','TestAlpha','passed',{duration_ms:5,timed:true}),logTest('b','TestBeta','failed'),logTest('c','TestGamma','skipped')])}]);
 assert.deepEqual(tests.map(t=>[t.key,t.source]),[['7:a','log'],['7:b','log'],['7:c','log']]);
 assert.deepEqual(filterExplorerTests(tests,{sort:'failures'}).map(t=>t.name),['TestBeta','TestAlpha','TestGamma']);
 assert.deepEqual(filterExplorerTests(tests,{status:'failing'}).map(t=>t.name),['TestBeta']);
 assert.deepEqual(filterExplorerTests(tests,{search:'gam unit'}).map(t=>t.name),['TestGamma']);
 assert.deepEqual(filterExplorerTests(tests,{sort:'duration'}).map(t=>t.name)[0],'TestAlpha');
 assert.equal(explorerTests([{label:'x',report:null}]).length,0);
});

test('the list labels each row and the card says the counts come from logs, and which steps are missing',()=>{
 const html=renderTestExplorer(ex([{label:'main',report:logReport([logTest('a','TestAlpha','passed'),logTest('b','TestBeta','failed')],{summary:{total:4},skipped_steps:[{step:'wcmc',reason:'log_too_large'}]})}]));
 assert.match(html,/data-cx-test="7:b"[^>]*data-test-status="failed"/);
 assert.match(html,/data-cx-source="log">CI log</);
 assert.match(html,/All counts are parsed from CI logs \(go\)/);
 assert.match(html,/The list names 2 of 4 counted tests/);
 assert.match(html,/Step wcmc is not counted: its log is too large/);
 assert.match(html,/lines 10–14/);
 assert.match(html,/<td>—<\/td>|class="cx-dur">—</);
});

test('a runner that prints only totals shows its counts and says there is no list',()=>{
 const html=renderTestExplorer(ex([{label:'main',report:logReport([],{summary:{total:30},runners:['karma']})}]));
 assert.match(html,/These runners print only totals, so 30 tests are counted but no list is available. Counts only./);
 assert.doesNotMatch(html,/data-cx-search|cx-list/);
});

test('a JUnit test shows its stored failure output; a log test shows the live excerpt, or says why there is none',()=>{
 const junit=explorerTests([{label:'m',report:junitReport([{id:'j',name:'adds',status:'failed',suite:'math',duration_ms:2000,message:'boom',trace:'at line 3'}])}])[0];
 const j=renderTestInspector(ex(null),junit);
 assert.match(j,/boom/);assert.match(j,/at line 3/);assert.match(j,/from the uploaded JUnit report/);assert.doesNotMatch(j,/data-cx-open-step/);
 const t=explorerTests([{label:'m',report:logReport([logTest('b','TestBeta','failed')])}])[0];
 assert.match(renderTestInspector(ex(null,{slice:null}),t),/Reading the live step log/);
 const slice={slice:{first:9,last:16,start:10,end:14,lines:['a','b','c','d','e','f','g','h']},total_lines:99,truncated:false};
 const ok=renderTestInspector(ex(null,{slice}),t);
 assert.equal((ok.match(/cx-hit/g)||[]).length,5);assert.match(ok,/Lines 9–16 of 99/);assert.match(ok,/data-cx-open-step/);assert.match(ok,/data-cx-history/);
 assert.match(renderTestInspector(ex(null,{slice:{slice:null,reason:'The log no longer matches the stored line range, so no excerpt is shown.'}}),t),/data-cx-slice-gone[^>]*>The log no longer matches/);
 assert.match(renderTestInspector(ex(null,{sliceError:'The log excerpt could not be read from CI.'}),t),/could not be read/);
 assert.match(renderTestInspector(ex(null,{slice:{...slice,truncated:true}}),t),/larger than HerdR reads/);
});

test('history lists the outcome of the same test in earlier runs, including runs without it',()=>{
 const t=explorerTests([{label:'m',report:logReport([logTest('b','TestBeta','failed')])}])[0];
 const html=renderTestInspector(ex(null,{history:[{run_number:7,status:'failed',source:'log'},{run_number:5,status:'passed',source:'log'},{run_number:4,status:'',source:'junit'},{run_number:3,status:'',source:'log',unindexed:true}]}),t);
 assert.match(html,/data-cx-open-run="5"/);assert.match(html,/No such test/);assert.match(html,/Test list no longer kept for this older run/);
 assert.match(renderTestInspector(ex(null,{history:'loading'}),t),/Looking this test up/);
});

test('long lists are capped and say so; loading and error states render',()=>{
 const many=Array.from({length:ROW_LIMIT+30},(_,i)=>logTest(`t${i}`,`Test${i}`,'passed'));
 const html=renderTestExplorer(ex([{label:'m',report:logReport(many)}]));
 assert.equal((html.match(/data-cx-test=/g)||[]).length,ROW_LIMIT);assert.match(html,new RegExp(`showing the first ${ROW_LIMIT}`));
 assert.match(renderTestExplorer(ex(null)),/Loading the tests/);
 assert.match(renderTestExplorer(ex(null,{error:'nope'})),/nope/);
 assert.equal(renderTestExplorer(null),'');
});

test('names are escaped',()=>{
 const html=renderTestExplorer(ex([{label:'m',report:logReport([logTest('x','<img src=x onerror=alert(1)>','failed')])}]));
 assert.doesNotMatch(html,/<img src=x/);
});

test('a run whose list was dropped says so instead of claiming the runner prints only totals',()=>{
 const html=renderTestExplorer(ex([{label:'main',report:logReport([],{summary:{total:30},runners:['cargo'],index_dropped:true})}]));
 assert.match(html,/The test list is no longer kept for 1 older run/);
 assert.doesNotMatch(html,/print only totals/);
});

test('a step that could not be read is named as retried, and a junit marker adds no gap',()=>{
 const html=renderTestExplorer(ex([{label:'main',report:logReport([logTest('a','TestAlpha','passed')],{summary:{total:1},skipped_steps:[{step:'lint',reason:'read_failed'},{step:'*',reason:'junit_report_exists'}]})}]));
 assert.match(html,/Step lint is not counted yet: its log could not be read, and it will be tried again/);
 assert.doesNotMatch(html,/Step \* /);
});

test('excerpt numbers from the server are coerced, never written as markup',()=>{
 const t=explorerTests([{label:'m',report:logReport([logTest('b','TestBeta','failed')])}])[0];
 const hostile={slice:{first:'1"><img src=x onerror=alert(1)>',last:'<b>',start:1,end:1,lines:['a']},total_lines:'<script>'};
 const html=renderTestInspector(ex(null,{slice:hostile}),t);
 assert.doesNotMatch(html,/<img|<script|<b>/);
});

test('the selected test unfolds under its own row, not below the list',()=>{
 const html=renderTestExplorer(ex([{label:'main',report:logReport([logTest('a','TestAlpha','failed'),logTest('b','TestBeta','passed')])}],{selected:'7:a',slice:{slice:{first:9,last:12,start:10,end:11,lines:['a','b','c','d']},total_lines:40}}));
 const row=html.match(/<li>(?:(?!<\/li>)[\s\S])*TestAlpha[\s\S]*?<\/li>/)[0];
 assert.match(row,/data-cx-inspector/);
 assert.match(row,/cx-log/);
 assert.equal(html.split('data-cx-inspector').length-1,1);
 assert.doesNotMatch(html.match(/<li>(?:(?!<\/li>)[\s\S])*TestBeta[\s\S]*?<\/li>/)[0],/data-cx-inspector/);
});

test('a failing log test offers to send itself to a Pi agent; a passing one does not',()=>{
 const report=logReport([logTest('a','TestAlpha','failed'),logTest('b','TestBeta','passed')]);
 const failing=renderTestExplorer(ex([{label:'main',report}],{selected:'7:a'}));
 assert.match(failing,/data-cx-agents/);
 assert.doesNotMatch(renderTestExplorer(ex([{label:'main',report}],{selected:'7:b'})),/data-cx-agents|data-cx-send/);
 const picked=renderTestExplorer(ex([{label:'main',report}],{selected:'7:a',sessions:[{session_id:'s1',display_name:'Pi <one>',branch:'main'}],sessionId:'s1',sendMessage:'Delivered.'}));
 assert.match(picked,/data-cx-session/);
 assert.match(picked,/<option value="s1" selected>Pi &lt;one&gt; · on this branch/);
 assert.doesNotMatch(picked,/data-cx-send disabled/);
 assert.match(picked,/data-cx-send-status[^>]*>Delivered\./);
 assert.match(renderTestExplorer(ex([{label:'main',report}],{selected:'7:a',sessions:[{session_id:'s1'}],sessionId:''})),/data-cx-send disabled/);
 assert.match(renderTestExplorer(ex([{label:'main',report}],{selected:'7:a',sessions:[]})),/No active Pi agents/);
});

test('a JUnit failure cannot be sent because the server has no log excerpt to read',()=>{
 const html=renderTestExplorer(ex([{label:'main',report:junitReport([{id:'j',name:'TestJ',status:'failed',suite:'s',trace:'boom'}])}],{selected:'6:j'}));
 assert.doesNotMatch(html,/data-cx-agents/);
 assert.match(html,/needs the log excerpt/);
});

test('a test already sent to the chosen agent offers "Send again", not a silent resend',()=>{
 const report=logReport([logTest('a','TestAlpha','failed')]);
 const base={selected:'7:a',sessions:[{session_id:'s1'},{session_id:'s2'}],sent:{'7:a':{sessionId:'s1',message:'Delivered to the selected Pi agent.'}},sendMessage:'Delivered to the selected Pi agent.'};
 assert.match(renderTestExplorer(ex([{label:'main',report}],{...base,sessionId:'s1'})),/data-cx-send>Send again/);
 assert.match(renderTestExplorer(ex([{label:'main',report}],{...base,sessionId:'s2'})),/data-cx-send>Send this failure/);
});
