import test from 'node:test';
import assert from 'node:assert/strict';
import {createRefreshBus, liveRefresher, pollWhileVisible} from '../refresh-bus.js';

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

test('register({fresh:true}) treats registration as a refresh: first event waits out the window',()=>{
  const {bus,advance}=harness();const a=view(),b=view();
  bus.register(a,{fresh:true});bus.register(b);
  bus.dispatch({event_type:'X'});advance(0);
  assert.equal(a.calls.length,0);assert.equal(b.calls.length,1);
  advance(4999);assert.equal(a.calls.length,0);
  advance(1);assert.equal(a.calls.length,1);
});

test('liveRefresher: a refresh requested mid-flight runs once more after it finishes, with the latest data',async()=>{
  const gates=[];let calls=0;const applied=[];
  const live=liveRefresher(()=>{calls++;return new Promise(r=>gates.push(r));},d=>applied.push(d));
  const first=live.refresh();
  const second=live.refresh();live.refresh();// both coalesce into one trailing run
  assert.equal(calls,1);
  gates[0]('old');await first;
  await new Promise(r=>setImmediate(r));
  assert.equal(calls,2,'the swallowed request runs once more');
  gates[1]('new');await second;
  assert.deepEqual(applied,['old','new']);
  await new Promise(r=>setImmediate(r));
  assert.equal(calls,2,'no extra runs');
});

test('liveRefresher: a disposed view applies nothing and does not rerun',async()=>{
  const gates=[];let calls=0;const applied=[];
  const live=liveRefresher(()=>{calls++;return new Promise(r=>gates.push(r));},d=>applied.push(d));
  const p=live.refresh();live.refresh();live.dispose();
  gates[0]('x');await p;await new Promise(r=>setImmediate(r));
  assert.deepEqual([calls,applied],[1,[]]);
});

test('pollWhileVisible fires on a quiet stream, skips hidden ticks, and stops',()=>{
  let tick=null,cleared=false,hidden=false,calls=0;
  const stop=pollWhileVisible(()=>calls++,15000,{setTimer:(fn,ms)=>{assert.equal(ms,15000);tick=fn;return 'h';},clearTimer:h=>{assert.equal(h,'h');cleared=true;},hidden:()=>hidden});
  tick();tick();
  assert.equal(calls,2,'no event needed: every visible tick refreshes');
  hidden=true;tick();
  assert.equal(calls,2,'a hidden tab does not poll');
  stop();
  assert.equal(cleared,true);
});
