import test from 'node:test';
import assert from 'node:assert/strict';
import {statusOutcome, recordOutcome} from '../record-state.js';

test('terminal lifecycle aliases are shared across mission and session records',()=>{
 for(const status of ['FAILED','error','crashed']) assert.equal(statusOutcome(status),'failed');
 for(const status of ['finished','complete','completed','closed','stopped','canceled','cancelled','succeeded','done','archived','merged']) assert.equal(statusOutcome(status),'finished');
 assert.equal(recordOutcome({status:' CLOSED ',stage:'running'}),'finished');
 assert.equal(recordOutcome({status:'failed',stage:'completed'}),'failed');
});
test('generic active records honor terminal stage while explicit resumed state remains current',()=>{
 assert.equal(recordOutcome({status:'active',stage:'failed'}),'failed');
 assert.equal(recordOutcome({status:'active',stage:'completed'}),'finished');
 assert.equal(recordOutcome({status:'running',stage:'failed'}),'current');
 assert.equal(recordOutcome({stage:'closed'}),'finished');
});
test('missing and unfamiliar states never silently become finished',()=>{
 for(const record of [{},null,{status:'unrecognized'},{status:'stale'},{status:'disconnected'}]) assert.equal(recordOutcome(record),'unknown');
 for(const status of ['running','active','paused_attention','waiting','idle','blocked']) assert.equal(statusOutcome(status),'current');
});
