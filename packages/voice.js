// Voice input/output helper for the Personal Assistant chat surface.
//
// Uses the Web Speech API (webkitSpeechRecognition / SpeechSynthesis) when the
// runtime provides it, and degrades gracefully to text-only otherwise. The pure
// capability functions take an explicit `win` so they are unit-testable in Node
// without a real browser.

export function isSpeechRecognitionAvailable(win = globalThis) {
  return Boolean(win && (win.SpeechRecognition || win.webkitSpeechRecognition));
}

export function isSpeechSynthesisAvailable(win = globalThis) {
  return Boolean(win && win.speechSynthesis && win.speechSynthesis.speak);
}

export function voiceCapabilities(win = globalThis) {
  return {
    input: isSpeechRecognitionAvailable(win),
    output: isSpeechSynthesisAvailable(win),
  };
}

export function voiceLabel(caps = {}) {
  if (caps.input && caps.output) return 'voice';
  if (caps.input) return 'voice-input';
  if (caps.output) return 'voice-output';
  return 'text-only';
}

// Create a voice-input controller. Returns { available, start, stop }. When the
// Speech API is unavailable, start() is a no-op that calls onError.
export function createVoiceInput({ onResult, onError, win = globalThis } = {}) {
  const Ctor = win && (win.SpeechRecognition || win.webkitSpeechRecognition);
  if (!Ctor) {
    return {
      available: false,
      start: () => onError && onError({ code: 'unavailable', message: 'Speech recognition is not available in this browser.' }),
      stop: () => {},
    };
  }
  const rec = new Ctor();
  rec.continuous = false;
  rec.interimResults = false;
  rec.lang = 'en-US';
  rec.onresult = (event) => {
    const transcript = Array.from(event.results || [])
      .map((r) => r && r[0] && r[0].transcript)
      .join(' ');
    if (transcript && onResult) onResult(transcript.trim());
  };
  rec.onerror = (event) => onError && onError(event);
  return { available: true, start: () => rec.start(), stop: () => rec.stop() };
}

// Speak text via SpeechSynthesis. Returns { spoken, error }.
export function speak(text, { win = globalThis, rate = 1 } = {}) {
  if (!text || !isSpeechSynthesisAvailable(win)) {
    return { spoken: false, error: 'unavailable' };
  }
  try {
    const utter = new win.SpeechSynthesisUtterance(text);
    utter.rate = rate;
    win.speechSynthesis.cancel();
    win.speechSynthesis.speak(utter);
    return { spoken: true };
  } catch (err) {
    return { spoken: false, error: err && err.message ? err.message : 'synthesis-failed' };
  }
}
