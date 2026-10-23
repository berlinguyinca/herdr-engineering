//! Benchmark driver. Usage:
//!   pi-runtime-bench <scenario> [--reps N] [--warmups N] [--concurrency N] [--runtimes FILE]
//!                    [--models-json FILE --model provider/id] [--out FILE.jsonl]
//! scenarios: version | help | noop | tools
//! Arms are interleaved per repetition so host noise hits every arm alike. A credential file
//! (models.json) is copied into a private 0600 agent dir that is removed on exit and never logged.

use pi_runtime_bench::{clearly_lower, phases, summarize, Marks, PhaseTracker, Phases, Summary};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Instant;

#[derive(Deserialize)]
struct Arm {
    argv: Vec<String>,
}

#[derive(Serialize, Clone)]
struct Row {
    arm: String,
    scenario: String,
    rep: usize,
    measured: bool,
    concurrency: usize,
    wall_ms: f64,
    cpu_s: f64,
    maxrss_mib: f64,
    exit: i32,
    phases: Option<Phases>,
    tool_ms: f64,
    tool_calls: u32,
    quality_ok: Option<bool>,
    loadavg1: f64,
    err_head: String,
}

struct AgentDir(PathBuf);
impl AgentDir {
    fn new(models_json: Option<&Path>) -> std::io::Result<Self> {
        let p = std::env::temp_dir().join(format!("pirb-{}-{}", std::process::id(), Instant::now().elapsed().subsec_nanos()));
        fs::create_dir(&p)?;
        fs::set_permissions(&p, fs::Permissions::from_mode(0o700))?;
        fs::write(p.join("settings.json"), "{}")?;
        if let Some(m) = models_json {
            let dst = p.join("models.json");
            fs::copy(m, &dst)?;
            fs::set_permissions(&dst, fs::Permissions::from_mode(0o600))?;
        }
        Ok(AgentDir(p))
    }
}
impl Drop for AgentDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn loadavg1() -> f64 {
    fs::read_to_string("/proc/loadavg").ok().and_then(|s| s.split_whitespace().next().and_then(|x| x.parse().ok())).unwrap_or(f64::NAN)
}

struct Outcome {
    wall_ms: f64,
    cpu_s: f64,
    maxrss_mib: f64,
    exit: i32,
    marks: Marks,
    exit_at_ms: f64,
    err_head: String,
}

fn spawn_one(argv: &[String], agent: &Path, cwd: &Path, extra_env: &[(String, String)], timeout_s: u64) -> Outcome {
    let t0 = Instant::now();
    let mut cmd = Command::new(&argv[0]);
    cmd.args(&argv[1..])
        .current_dir(cwd)
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", agent)
        .env("PI_CODING_AGENT_DIR", agent)
        .env("PI_SKIP_VERSION_CHECK", "1")
        .envs(extra_env.iter().map(|(k, v)| (k.as_str(), v.as_str())))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            return Outcome { wall_ms: 0.0, cpu_s: 0.0, maxrss_mib: 0.0, exit: -1, marks: Marks::default(), exit_at_ms: 0.0, err_head: format!("spawn failed: {e}") }
        }
    };
    let pid = child.id() as i32;
    let done = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let watch = done.clone();
    std::thread::spawn(move || {
        for _ in 0..timeout_s * 10 {
            std::thread::sleep(std::time::Duration::from_millis(100));
            if watch.load(std::sync::atomic::Ordering::Relaxed) {
                return;
            }
        }
        unsafe { libc::kill(pid, libc::SIGKILL) };
    });
    let mut stderr = child.stderr.take().unwrap();
    let err_thread = std::thread::spawn(move || {
        let mut s = String::new();
        let _ = stderr.read_to_string(&mut s);
        s
    });
    let mut tracker = PhaseTracker::default();
    for line in BufReader::new(child.stdout.take().unwrap()).lines().map_while(Result::ok) {
        tracker.feed(&line, t0.elapsed().as_secs_f64() * 1000.0);
    }
    // Reap with wait4 for this child's own rusage (std's wait would discard it).
    let mut status = 0;
    let mut ru: libc::rusage = unsafe { std::mem::zeroed() };
    unsafe { libc::wait4(pid, &mut status, 0, &mut ru) };
    done.store(true, std::sync::atomic::Ordering::Relaxed);
    let exit_at = t0.elapsed().as_secs_f64() * 1000.0;
    let tv = |t: libc::timeval| t.tv_sec as f64 + t.tv_usec as f64 / 1e6;
    let err = err_thread.join().unwrap_or_default();
    let exit = if libc::WIFEXITED(status) { libc::WEXITSTATUS(status) } else { 128 + libc::WTERMSIG(status) };
    Outcome {
        wall_ms: exit_at,
        cpu_s: tv(ru.ru_utime) + tv(ru.ru_stime),
        maxrss_mib: ru.ru_maxrss as f64 / 1024.0,
        exit,
        marks: tracker.marks,
        exit_at_ms: exit_at,
        err_head: if exit != 0 { err.lines().next().unwrap_or("").chars().take(160).collect() } else { String::new() },
    }
}

const TOOLS_PROMPT: &str = "Read every file f01.txt .. f10.txt in the current directory (each holds one integer), then create total.txt containing only the sum of those integers. Use tools; reply DONE when finished.";

fn scenario_argv(scenario: &str, model: &str) -> Vec<String> {
    let s = |x: &str| x.to_string();
    match scenario {
        "version" => vec![s("--version")],
        "help" => vec![s("--help")],
        "noop" | "tools" => {
            let prompt = if scenario == "noop" { "Reply with exactly: OK" } else { TOOLS_PROMPT };
            let mut v = vec![s("-p"), s(prompt), s("--mode"), s("json"), s("--no-session"), s("--thinking"), s("off"), s("--model"), s(model)];
            if scenario == "noop" {
                v.push(s("--no-tools"));
            }
            v
        }
        other => {
            eprintln!("unknown scenario {other}");
            std::process::exit(2)
        }
    }
}

fn expected_total() -> i64 {
    (1..=10).map(|n| n * 7).sum()
}

fn run_arm(name: &str, arm: &Arm, scenario: &str, model: &str, agent: &Path, work_root: &Path, rep: usize, measured: bool, conc: usize) -> Vec<Row> {
    let handles: Vec<_> = (0..conc)
        .map(|k| {
            let mut argv = arm.argv.clone();
            argv.extend(scenario_argv(scenario, model));
            let work = work_root.join(format!("{name}-{rep}-{k}"));
            fs::create_dir_all(&work).unwrap();
            for n in 1..=10 {
                fs::write(work.join(format!("f{n:02}.txt")), format!("{}\n", n * 7)).unwrap();
            }
            let (agent, name, scenario) = (agent.to_path_buf(), name.to_string(), scenario.to_string());
            std::thread::spawn(move || {
                let o = spawn_one(&argv, &agent, &work, &[], 900);
                let quality = (scenario == "tools").then(|| fs::read_to_string(work.join("total.txt")).map(|t| t.trim() == expected_total().to_string()).unwrap_or(false));
                let live = scenario == "noop" || scenario == "tools";
                Row {
                    arm: name,
                    scenario,
                    rep,
                    measured,
                    concurrency: conc,
                    wall_ms: o.wall_ms,
                    cpu_s: o.cpu_s,
                    maxrss_mib: o.maxrss_mib,
                    exit: o.exit,
                    phases: live.then(|| phases(&o.marks, o.exit_at_ms)),
                    tool_ms: o.marks.tool_ms,
                    tool_calls: o.marks.tool_calls,
                    quality_ok: quality,
                    loadavg1: loadavg1(),
                    err_head: o.err_head,
                }
            })
        })
        .collect();
    handles.into_iter().map(|h| h.join().unwrap()).collect()
}

fn arg(args: &[String], flag: &str) -> Option<String> {
    args.iter().position(|a| a == flag).and_then(|i| args.get(i + 1)).cloned()
}

fn repeated(args: &[String], flag: &str) -> Vec<String> {
    args.windows(2).filter(|w| w[0] == flag).map(|w| w[1].clone()).collect()
}

#[derive(Deserialize, Serialize, Default, Clone)]
struct ProbeOut {
    #[serde(default)] bun: bool,
    #[serde(default)] node: String,
    #[serde(default)] fallback: Option<String>,
    #[serde(default)] commands: Vec<String>,
    #[serde(default)] tools: Vec<String>,
    #[serde(default)] err: String,
}

/// Load the same packages under every arm (isolated dirs, no credentials) and diff what registered.
fn compat(args: &[String], arms: &BTreeMap<String, Arm>) {
    let packages = repeated(args, "--package");
    let label = arg(args, "--label").unwrap_or_else(|| "compat".into());
    let probe = fs::canonicalize(arg(args, "--probe").unwrap_or_else(|| "probe".into())).expect("probe dir");
    let mut results: BTreeMap<String, Option<ProbeOut>> = BTreeMap::new();
    let mut costs: BTreeMap<String, (f64, f64, f64)> = BTreeMap::new();
    for (name, arm) in arms {
        let agent = AgentDir::new(None).expect("agent dir");
        let mut pk: Vec<String> = packages.clone();
        pk.push(probe.display().to_string());
        fs::write(agent.0.join("settings.json"), serde_json::json!({ "packages": pk }).to_string()).unwrap();
        let out = agent.0.join("probe.json");
        let env = vec![
            ("PROBE_OUT".to_string(), out.display().to_string()),
            ("PROBE_EXIT".to_string(), "1".to_string()),
            ("PI_OFFLINE".to_string(), "1".to_string()),
            ("PI_ENGINEERING_STATE_DIR".to_string(), agent.0.join("state").display().to_string()),
        ];
        let mut argv = arm.argv.clone();
        argv.extend(["-p".into(), "hi".into(), "--no-session".into()]);
        let o = spawn_one(&argv, &agent.0, &agent.0, &env, 120);
        let parsed = fs::read_to_string(&out).ok().and_then(|t| serde_json::from_str::<ProbeOut>(&t).ok());
        println!("{name:12} exit={} wall_ms={:.0} cpu_s={:.1} maxrss_MiB={:.0} loaded={}", o.exit, o.wall_ms, o.cpu_s, o.maxrss_mib, parsed.is_some());
        costs.insert(name.clone(), (o.wall_ms, o.cpu_s, o.maxrss_mib));
        results.insert(name.clone(), parsed);
    }
    let base = results.get("standard-pi").cloned().flatten();
    let mut rows = vec![];
    for (name, r) in &results {
        let (Some(r), Some(b)) = (r, &base) else {
            println!("{name}: no probe output (extension set failed to load or hung)");
            rows.push(serde_json::json!({"label": label, "arm": name, "loaded": false}));
            continue;
        };
        let diff = |a: &[String], b: &[String]| -> Vec<String> { a.iter().filter(|x| !b.contains(x)).cloned().collect() };
        let (add_c, miss_c) = (diff(&r.commands, &b.commands), diff(&b.commands, &r.commands));
        let (add_t, miss_t) = (diff(&r.tools, &b.tools), diff(&b.tools, &r.tools));
        println!("{name}: commands={} tools={} fallback={:?} runtime={} extra_commands={add_c:?} missing_commands={miss_c:?} extra_tools={add_t:?} missing_tools={miss_t:?} err={:?}",
            r.commands.len(), r.tools.len(), r.fallback, if r.bun { "bun" } else { "node" }, r.err);
        let (wall_ms, cpu_s, maxrss_mib) = costs.get(name).copied().unwrap_or_default();
        rows.push(serde_json::json!({"label": label, "arm": name, "loaded": true, "wall_ms": wall_ms, "cpu_s": cpu_s, "maxrss_mib": maxrss_mib, "commands": r.commands.len(), "tools": r.tools.len(), "fallback": r.fallback,
            "extra_commands": add_c, "missing_commands": miss_c, "extra_tools": add_t, "missing_tools": miss_t, "err": r.err, "packages": packages}));
    }
    if let Some(o) = arg(args, "--out") {
        let mut f = fs::OpenOptions::new().create(true).append(true).open(o).unwrap();
        for r in rows {
            writeln!(f, "{r}").unwrap();
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(scenario) = args.first().cloned() else {
        eprintln!("usage: pi-runtime-bench <version|help|noop|tools> [--reps N] [--warmups N] [--concurrency N] [--runtimes FILE] [--models-json FILE --model P/ID] [--out FILE]");
        std::process::exit(2)
    };
    let reps: usize = arg(&args, "--reps").and_then(|v| v.parse().ok()).unwrap_or(10);
    let warmups: usize = arg(&args, "--warmups").and_then(|v| v.parse().ok()).unwrap_or(1);
    let conc: usize = arg(&args, "--concurrency").and_then(|v| v.parse().ok()).unwrap_or(1);
    let model = arg(&args, "--model").unwrap_or_default();
    let runtimes = arg(&args, "--runtimes").unwrap_or_else(|| "runtimes.json".into());
    let out = arg(&args, "--out");
    let arms: BTreeMap<String, Arm> = serde_json::from_str(&fs::read_to_string(&runtimes).expect("runtimes file")).expect("runtimes json");
    if scenario == "compat" {
        compat(&args, &arms);
        return;
    }
    let live = scenario == "noop" || scenario == "tools";
    if live && (model.is_empty() || arg(&args, "--models-json").is_none()) {
        eprintln!("{scenario} needs --model and --models-json");
        std::process::exit(2)
    }
    let agent = AgentDir::new(arg(&args, "--models-json").map(PathBuf::from).as_deref()).expect("agent dir");
    let work_root = agent.0.join("work");
    fs::create_dir(&work_root).unwrap();
    let mut rows: Vec<Row> = vec![];
    for rep in 0..warmups + reps {
        for (name, arm) in &arms {
            rows.extend(run_arm(name, arm, &scenario, &model, &agent.0, &work_root, rep, rep >= warmups, conc));
            let _ = fs::remove_dir_all(&work_root);
            fs::create_dir(&work_root).unwrap();
        }
    }
    if let Some(o) = &out {
        let mut f = fs::File::create(o).expect("out file");
        for r in &rows {
            writeln!(f, "{}", serde_json::to_string(r).unwrap()).unwrap();
        }
    }
    let mut baseline: Option<(String, Summary)> = None;
    let mut order: Vec<&String> = arms.keys().collect();
    order.sort_by_key(|n| (n.as_str() != "standard-pi", n.to_string())); // baseline first
    for name in order {
        let m: Vec<&Row> = rows.iter().filter(|r| &r.arm == name && r.measured).collect();
        let ok: Vec<&&Row> = m.iter().filter(|r| r.exit == 0).collect();
        let wall = summarize(&ok.iter().map(|r| r.wall_ms).collect::<Vec<_>>());
        let cpu = summarize(&ok.iter().map(|r| r.cpu_s).collect::<Vec<_>>());
        let rss = summarize(&ok.iter().map(|r| r.maxrss_mib).collect::<Vec<_>>());
        print!("{name:12} ok={}/{} wall_ms med={:.0} p95={:.0} ci95=[{:.0},{:.0}] cpu_s med={:.3} rss_MiB med={:.0}", ok.len(), m.len(), wall.median, wall.p95, wall.ci95.0, wall.ci95.1, cpu.median, rss.median);
        if live {
            let col = |f: fn(&Phases) -> Option<f64>| ok.iter().filter_map(|r| r.phases.as_ref().and_then(f)).collect::<Vec<_>>();
            for (label, v) in [("start", col(|p| p.runtime_start_ms)), ("prep", col(|p| p.prep_ms)), ("ttft", col(|p| p.model_ttft_ms)), ("exit", col(|p| p.exit_ms))] {
                print!(" {label}={:.0}/{:.0}", summarize(&v).median, summarize(&v).p95);
            }
            if scenario == "tools" {
                print!(" quality={}/{} tool_ms={:.0}", ok.iter().filter(|r| r.quality_ok == Some(true)).count(), ok.len(), summarize(&ok.iter().map(|r| r.tool_ms).collect::<Vec<_>>()).median);
            }
        }
        println!();
        match &baseline {
            None => baseline = Some((name.clone(), wall)),
            Some((b, bs)) => println!("  wall vs {b}: {}", if clearly_lower(&wall, bs) { "clearly lower (disjoint 95% CIs)" } else { "not clearly different" }),
        }
    }
    let failed: Vec<&Row> = rows.iter().filter(|r| r.measured && r.exit != 0).collect();
    for r in failed.iter().take(3) {
        println!("failure {} exit={} {}", r.arm, r.exit, r.err_head);
    }
}
