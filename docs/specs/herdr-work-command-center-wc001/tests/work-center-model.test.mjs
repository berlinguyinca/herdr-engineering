import test from 'node:test';
import assert from 'node:assert/strict';
import {fromMission, projectWork, weeklySummary, measuredProgress, normalizeState} from '../implementation/work-center-model.mjs';
test('mission and linked session count as one work item', () => {
 const items = projectWork({missions:[{mission_key:'pi:MSN-1',title:'Feature',sessions:[{session_id:'s1'}]}],sessions:[{session_id:'s1',mission_key:'pi:MSN-1'},{session_id:'s2',status:'running'}]});
 assert.equal(items.length,2); assert.deepEqual(items[0].sessionIds,['s1']);
 assert.equal(items[0].href,'#/missions/pi%3AMSN-1');
});
test('unverified progress remains unknown',()=>assert.equal(measuredProgress({}),null));
test('acceptance evidence wins over weighted dag',()=>assert.deepEqual(measuredProgress({acceptanceCoverage:{completed:3,total:4},workflowProgress:{basis:'weighted_dag',completed:2,total:5}}),{percent:75,basis:'acceptance_coverage'}));
test('bogus progress is rejected',()=>assert.equal(measuredProgress({acceptanceCoverage:{completed:8,total:2}}),null));
test('quota paused is not completed',()=>assert.equal(normalizeState('quota exceeded'),'quota_paused'));
test('weekly count needs timestamps for completion, not just status',()=>{const items=[{state:'finished',startedAt:'2026-11-02T09:00:00Z',completedAt:'2026-11-03T09:00:00Z'},{state:'quota_paused',startedAt:'2026-11-04T09:00:00Z'}];assert.deepEqual(weeklySummary(items,'2026-11-02T00:00:00Z','2026-11-09T00:00:00Z'),{started:2,finished:1,open:1,paused:1,unconfirmed:0});});
