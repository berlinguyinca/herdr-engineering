//! Pure core of the benchmark driver: event-stream phase tracking and statistics.
//! No process or clock access here; `main.rs` supplies timestamps.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;

/// Milestones stamped from Pi's `--mode json` stream, in milliseconds since spawn.
#[derive(Debug, Default, Clone, PartialEq, Serialize, Deserialize)]
pub struct Marks {
    pub session: Option<f64>,
    pub assistant_start: Option<f64>,
    pub first_delta: Option<f64>,
    pub agent_end: Option<f64>,
    pub tool_ms: f64,
    pub tool_calls: u32,
}

#[derive(Default)]
pub struct PhaseTracker {
    pub marks: Marks,
    open_tools: HashMap<String, f64>,
}

impl PhaseTracker {
    /// Feed one stdout line and the time it arrived. Non-JSON lines are ignored.
    pub fn feed(&mut self, line: &str, at_ms: f64) {
        let Ok(v) = serde_json::from_str::<Value>(line) else { return };
        let t = v.get("type").and_then(Value::as_str).unwrap_or("");
        let m = &mut self.marks;
        match t {
            "session" => {
                m.session.get_or_insert(at_ms);
            }
            "message_start" => {
                if v.pointer("/message/role").and_then(Value::as_str) == Some("assistant") {
                    m.assistant_start.get_or_insert(at_ms);
                }
            }
            "message_update" => {
                let kind = v.pointer("/assistantMessageEvent/type").and_then(Value::as_str).unwrap_or("");
                if kind.ends_with("_delta") {
                    m.first_delta.get_or_insert(at_ms);
                }
            }
            "agent_end" => m.agent_end = Some(at_ms),
            "tool_execution_start" => {
                if let Some(id) = v.get("toolCallId").and_then(Value::as_str) {
                    self.open_tools.insert(id.to_string(), at_ms);
                }
            }
            "tool_execution_end" => {
                if let Some(id) = v.get("toolCallId").and_then(Value::as_str) {
                    if let Some(s) = self.open_tools.remove(id) {
                        m.tool_ms += at_ms - s;
                        m.tool_calls += 1;
                    }
                }
            }
            _ => {}
        }
    }
}

/// Runtime-attributable vs model-attributable split of one run (all ms).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Phases {
    pub runtime_start_ms: Option<f64>,
    pub prep_ms: Option<f64>,
    pub model_ttft_ms: Option<f64>,
    pub model_total_ms: Option<f64>,
    pub exit_ms: Option<f64>,
}

pub fn phases(m: &Marks, exit_at_ms: f64) -> Phases {
    let d = |a: Option<f64>, b: Option<f64>| match (a, b) {
        (Some(a), Some(b)) => Some(b - a),
        _ => None,
    };
    Phases {
        runtime_start_ms: m.session,
        prep_ms: d(m.session, m.assistant_start),
        model_ttft_ms: d(m.assistant_start, m.first_delta),
        model_total_ms: d(m.assistant_start, m.agent_end),
        exit_ms: m.agent_end.map(|e| exit_at_ms - e),
    }
}

/// Linear-interpolated percentile, q in 0..=1. Empty input gives NaN.
pub fn percentile(xs: &[f64], q: f64) -> f64 {
    if xs.is_empty() {
        return f64::NAN;
    }
    let mut v = xs.to_vec();
    v.sort_by(|a, b| a.total_cmp(b));
    let k = (v.len() - 1) as f64 * q.clamp(0.0, 1.0);
    let (f, c) = (k.floor() as usize, k.ceil() as usize);
    v[f] + (v[c] - v[f]) * (k - f as f64)
}

pub fn median(xs: &[f64]) -> f64 {
    percentile(xs, 0.5)
}

/// Deterministic xorshift64* so a reported interval can be reproduced from the seed.
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
}

/// Percentile-bootstrap 95% interval for the median. With fewer than 3 samples the
/// interval is the sample range (no pretence of precision).
pub fn median_ci95(xs: &[f64], resamples: usize, seed: u64) -> (f64, f64) {
    if xs.len() < 3 {
        let lo = xs.iter().cloned().fold(f64::INFINITY, f64::min);
        let hi = xs.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        return (lo, hi);
    }
    let mut rng = Rng(seed | 1);
    let mut meds: Vec<f64> = (0..resamples)
        .map(|_| {
            let s: Vec<f64> = (0..xs.len()).map(|_| xs[(rng.next() % xs.len() as u64) as usize]).collect();
            median(&s)
        })
        .collect();
    meds.sort_by(|a, b| a.total_cmp(b));
    (percentile(&meds, 0.025), percentile(&meds, 0.975))
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Summary {
    pub n: usize,
    pub median: f64,
    pub p95: f64,
    pub ci95: (f64, f64),
}

pub fn summarize(xs: &[f64]) -> Summary {
    Summary { n: xs.len(), median: median(xs), p95: percentile(xs, 0.95), ci95: median_ci95(xs, 2000, 0x5eed) }
}

/// `true` only when the intervals of `a` (candidate) and `b` (baseline) do not overlap and a < b.
pub fn clearly_lower(a: &Summary, b: &Summary) -> bool {
    a.ci95.1 < b.ci95.0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(lines: &[(&str, f64)]) -> PhaseTracker {
        let mut t = PhaseTracker::default();
        for (l, at) in lines {
            t.feed(l, *at);
        }
        t
    }

    #[test]
    fn phases_split_runtime_from_model() {
        let t = run(&[
            (r#"{"type":"session"}"#, 50.0),
            ("not json", 55.0),
            (r#"{"type":"message_start","message":{"role":"system"}}"#, 60.0),
            (r#"{"type":"message_start","message":{"role":"assistant"}}"#, 70.0),
            (r#"{"type":"message_update","assistantMessageEvent":{"type":"thinking_start"}}"#, 80.0),
            (r#"{"type":"message_update","assistantMessageEvent":{"type":"thinking_delta"}}"#, 900.0),
            (r#"{"type":"message_update","assistantMessageEvent":{"type":"text_delta"}}"#, 950.0),
            (r#"{"type":"agent_end"}"#, 1000.0),
        ]);
        let p = phases(&t.marks, 1030.0);
        assert_eq!(p.runtime_start_ms, Some(50.0));
        assert_eq!(p.prep_ms, Some(20.0));
        assert_eq!(p.model_ttft_ms, Some(830.0), "first *_delta, not *_start");
        assert_eq!(p.model_total_ms, Some(930.0));
        assert_eq!(p.exit_ms, Some(30.0));
    }

    #[test]
    fn missing_milestones_stay_none() {
        let t = run(&[(r#"{"type":"session"}"#, 10.0)]);
        let p = phases(&t.marks, 99.0);
        assert_eq!(p.runtime_start_ms, Some(10.0));
        assert_eq!((p.prep_ms, p.model_ttft_ms, p.exit_ms), (None, None, None));
    }

    #[test]
    fn tool_time_sums_matched_pairs_only() {
        let t = run(&[
            (r#"{"type":"tool_execution_start","toolCallId":"a"}"#, 100.0),
            (r#"{"type":"tool_execution_start","toolCallId":"b"}"#, 110.0),
            (r#"{"type":"tool_execution_end","toolCallId":"a"}"#, 130.0),
            (r#"{"type":"tool_execution_end","toolCallId":"zzz"}"#, 140.0),
            (r#"{"type":"tool_execution_end","toolCallId":"b"}"#, 160.0),
        ]);
        assert_eq!(t.marks.tool_calls, 2);
        assert_eq!(t.marks.tool_ms, 80.0);
    }

    #[test]
    fn first_marks_are_not_overwritten() {
        let t = run(&[(r#"{"type":"session"}"#, 5.0), (r#"{"type":"session"}"#, 9.0)]);
        assert_eq!(t.marks.session, Some(5.0));
    }

    #[test]
    fn percentiles() {
        let xs = [1.0, 2.0, 3.0, 4.0, 5.0];
        assert_eq!(median(&xs), 3.0);
        assert_eq!(percentile(&xs, 0.0), 1.0);
        assert_eq!(percentile(&xs, 1.0), 5.0);
        assert!((percentile(&xs, 0.95) - 4.8).abs() < 1e-9);
        assert_eq!(median(&[4.0, 1.0]), 2.5);
        assert!(median(&[]).is_nan());
    }

    #[test]
    fn bootstrap_is_deterministic_and_brackets_median() {
        let xs: Vec<f64> = (1..=20).map(|x| x as f64).collect();
        let a = median_ci95(&xs, 1000, 7);
        assert_eq!(a, median_ci95(&xs, 1000, 7));
        assert!(a.0 <= median(&xs) && median(&xs) <= a.1);
        assert!(a.0 > 1.0 && a.1 < 20.0);
        assert_eq!(median_ci95(&[2.0, 9.0], 1000, 7), (2.0, 9.0));
    }

    #[test]
    fn clearly_lower_requires_disjoint_intervals() {
        let fast = summarize(&(0..12).map(|i| 50.0 + i as f64).collect::<Vec<_>>());
        let slow = summarize(&(0..12).map(|i| 1500.0 + i as f64).collect::<Vec<_>>());
        assert!(clearly_lower(&fast, &slow));
        assert!(!clearly_lower(&slow, &fast));
        assert!(!clearly_lower(&fast, &fast));
    }
}
