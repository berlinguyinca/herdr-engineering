import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  isSpeechRecognitionAvailable,
  isSpeechSynthesisAvailable,
  voiceCapabilities,
  voiceLabel,
  createVoiceInput,
  speak,
} from '../voice.js';

function fakeWin({ rec = false, synth = false } = {}) {
  const w = {};
  if (rec) w.webkitSpeechRecognition = class {};
  if (synth) {
    w.speechSynthesis = { speak: () => {}, cancel: () => {} };
    w.SpeechSynthesisUtterance = class { set rate(v) {} };
  }
  return w;
}

test('speech recognition unavailable by default', () => {
  assert.equal(isSpeechRecognitionAvailable({}), false);
  assert.equal(isSpeechRecognitionAvailable(fakeWin({ rec: true })), true);
});

test('speech synthesis availability', () => {
  assert.equal(isSpeechSynthesisAvailable({}), false);
  assert.equal(isSpeechSynthesisAvailable(fakeWin({ synth: true })), true);
});

test('voiceCapabilities reports both channels', () => {
  const caps = voiceCapabilities(fakeWin({ rec: true, synth: true }));
  assert.deepEqual(caps, { input: true, output: true });
});

test('voiceLabel maps capability sets', () => {
  assert.equal(voiceLabel({ input: true, output: true }), 'voice');
  assert.equal(voiceLabel({ input: true, output: false }), 'voice-input');
  assert.equal(voiceLabel({ input: false, output: true }), 'voice-output');
  assert.equal(voiceLabel({}), 'text-only');
});

test('createVoiceInput is safe when unavailable and calls onError', () => {
  let err = null;
  const v = createVoiceInput({ win: {}, onError: (e) => (err = e) });
  assert.equal(v.available, false);
  v.start();
  assert.equal(err.code, 'unavailable');
  assert.doesNotThrow(() => v.stop());
});

test('speak returns unavailable when no synth', () => {
  const res = speak('hello', { win: {} });
  assert.equal(res.spoken, false);
  assert.equal(res.error, 'unavailable');
});

test('speak returns spoken when synth exists', () => {
  const res = speak('hello', { win: fakeWin({ synth: true }) });
  assert.equal(res.spoken, true);
});
