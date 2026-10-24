import test from 'node:test';
import assert from 'node:assert/strict';
import {createRefreshBus} from '../refresh-bus.js';

function harness(){
  let t=0;const timers=[];
  const h={
    advance(ms){
      const end=t+ms;
      for(;;){
        const due=timers.filter(x=>!x.dead&&x.at<=end).sort((a,b)=>a.at-b.at)[0];
        if(!due)break;
        t=due.at;due.dead=true;due.fn();
      }
      t=end;
    },
    bus:createRefreshBus({
      throttleMs:5000,
      now:()=>t,
      setTimer:(fn,ms)=>{const x={fn,at:t+ms,dead:false};timers.push(x);return x},
      clearTimer:x=>{x.dead=true},
    }),
  };
  return h;
}
const view=(interests)=>{const calls=[];return {calls,refresh:hint=>calls.push(hint),interests}};

test('burst of 50 events yields one refresh per view per window',()=>{
  const {bus,advance}=harness();const a=view(),b=view();
  bus.register(a);bus.register(b);
  for(let i=0;i<50;i++)bus.dispatch({event_type:'X',i});
  advance(0);
  assert.equal(a.calls.length,1);assert.equal(b.calls.length,1);
  for(let i=0;i<50;i++)bus.dispatch({event_type:'X',i});
  advance(4999);assert.equal(a.calls.length,1);
  advance(1);assert.equal(a.calls.length,2);
  advance(20000);assert.equal(a.calls.length,2);
});

test('hint is the latest matching event',()=>{
  const {bus,advance}=harness();const a=view();bus.register(a);
  bus.dispatch({event_type:'A'});bus.dispatch({event_type:'B'});advance(0);
  assert.equal(a.calls[0].event_type,'B');
});

test('events outside interests do not refresh',()=>{
  const {bus,advance}=harness();const a=view(['HostHeartbeat']),b=view();
  bus.register(a);bus.register(b);
  bus.dispatch({event_type:'MissionStageChanged'});advance(10000);
  assert.equal(a.calls.length,0);assert.equal(b.calls.length,1);
  bus.dispatch({event_type:'HostHeartbeat'});advance(10000);
  assert.equal(a.calls.length,1);
});

test('pause drops delivery and cancels pending; resume refreshes every view once',()=>{
  const {bus,advance}=harness();const a=view(['X']),b=view(['Y']);
  bus.register(a);bus.register(b);
  bus.dispatch({event_type:'X'});bus.pause();advance(10000);
  bus.dispatch({event_type:'X'});advance(10000);
  assert.equal(a.calls.length,0);
  bus.resume();advance(0);
  assert.equal(a.calls.length,1);assert.equal(b.calls.length,1);
  bus.resume();assert.equal(a.calls.length,1,'resume while running is a no-op');
});

test('register returns an unsubscribe that stops delivery and pending refresh',()=>{
  const {bus,advance}=harness();const a=view();
  const off=bus.register(a);
  bus.dispatch({event_type:'X'});off();advance(10000);
  bus.dispatch({event_type:'X'});advance(10000);
  assert.equal(a.calls.length,0);
});

test('a throwing view does not block others',()=>{
  const {bus,advance}=harness();const b=view();
  bus.register({refresh(){throw new Error('x')}});bus.register(b);
  bus.dispatch({event_type:'X'});advance(0);
  assert.equal(b.calls.length,1);
});
