import test from 'node:test';
import assert from 'node:assert/strict';
import {ciHealthPreferences,ciHealthSelection,ciHealthUnavailable} from '../ci-health-view.js';

test('health preferences validate enums and bound persisted filters and disclosures',()=>{
  assert.deepEqual(ciHealthPreferences({mode:'bogus',scope:'all',sort:'failures',search:'x',branch:'main',failuresOnly:true,expanded:['repo',42,null]}),{mode:'hours',scope:'all',sort:'failures',search:'x',branch:'main',failuresOnly:true,expanded:['repo']});
  const value=ciHealthPreferences({mode:'30',scope:'pr',sort:'name',search:'x'.repeat(10000),expanded:Array.from({length:900},(_,i)=>String(i)),failuresOnly:'true'});
  assert.equal(value.mode,'30');assert.equal(value.scope,'pr');assert.equal(value.search.length,500);assert.equal(value.expanded.length,500);assert.equal(value.failuresOnly,false);
});

test('health preferences tolerate invalid and partial storage without carrying route context',()=>{
  for(const value of [null,[],true,'bad']) assert.deepEqual(ciHealthPreferences(value),ciHealthPreferences({}));
  const prefs=ciHealthPreferences({repository:'private/repo',mission:'secret',session:'agent',selection:{projectKey:'x'},layout:'pipeline'});
  assert.equal('repository' in prefs,false);assert.equal('mission' in prefs,false);assert.equal('session' in prefs,false);assert.equal('selection' in prefs,false);assert.equal('layout' in prefs,false);
});


test('persisted cell selection retains compound run identity beyond short ordinal keys',()=>{
  const projectKey=JSON.stringify(['https://ci.example/'+ 'instance/'.repeat(18),'123']);
  const bucketKey='run:'+JSON.stringify(['https://ci.example/'+ 'instance/'.repeat(18),'123',42]);
  assert.ok(bucketKey.length>100);
  assert.deepEqual(ciHealthSelection({projectKey,bucketKey}),{projectKey,bucketKey});
  for(const value of [null,[],{projectKey:12,bucketKey:'run:1'},{projectKey,bucketKey:'x'.repeat(5000)}])assert.equal(ciHealthSelection(value),null);
});

test('unavailable history keeps backend cause distinct from a missing history client',()=>{
  assert.equal(ciHealthUnavailable({available:false,status:'disabled',error:'CI provider is disabled'}),'CI history unavailable: CI provider is disabled.');
  assert.equal(ciHealthUnavailable({available:false,status:'not_configured'}),'CI history unavailable: not_configured.');
  assert.equal(ciHealthUnavailable({},true),'CI history unavailable. The provider history endpoint is required.');
});

test('cold background discovery is loading rather than an unavailable provider',()=>{
 assert.equal(ciHealthUnavailable({available:false,status:'syncing',refreshing:true}), 'Loading CI projects and history…');
});
