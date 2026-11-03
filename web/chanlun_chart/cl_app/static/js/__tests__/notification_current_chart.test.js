'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {annotationPoints}=require('../notification_current_chart.js');
const coordinate=(time,resolution,label)=>label==='end'?time-Number(resolution)*60:time;
const base={notification_at:1900,anchor_at:1800,anchor_price:12.34,resolution:'5'};

test('delivery time and the saved reference point stay distinct on current candles',()=>{
  const marks=annotationPoints(base,[1200,1500,1800,2100],'5','end',coordinate);
  assert.deepEqual(marks,[{kind:'notice',time:1800,at:1900},{kind:'point',time:1500,price:12.34,at:1800}]);
});
test('switching to a larger period preserves the original reference time and price within the latest bar',()=>{
  const marks=annotationPoints(base,[0,1800],'30','end',coordinate);
  assert.deepEqual(marks,[{kind:'notice',time:1800,at:1900},{kind:'point',time:0,price:12.34,at:1800}]);
  const later=annotationPoints({...base,anchor_at:2400},[0,1800],'30','end',coordinate);
  assert.deepEqual(later[1],{kind:'point',time:1800,price:12.34,at:2400});
});
test('old records outside loaded history are not falsely moved onto the first candle',()=>{
  assert.deepEqual(annotationPoints(base,[3000,3300],'5','end',coordinate),[]);
  const loaded=annotationPoints(base,[1200,1500,1800,2100,3000,3300],'5','end',coordinate);
  assert.equal(loaded.length,2);
});
test('missing reference metadata still marks the notice without inventing its price',()=>{
  const marks=annotationPoints({...base,anchor_price:null},[1500,1800,2100],'5','end',coordinate);
  assert.deepEqual(marks,[{kind:'notice',time:1800,at:1900}]);
  assert.deepEqual(annotationPoints({...base,notification_at:null,anchor_at:null},[1500,1800],'5','end',coordinate),[]);
});
