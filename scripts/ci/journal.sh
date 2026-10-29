# Source from a gate script:   . "$(dirname "$0")/journal.sh"; ci_journal <gate-name>
# Mirrors everything the gate prints into a journal file. In CI the journal goes straight into the durable directory named by
# scripts/ci/log-archive.yml (written as it goes, so a killed step keeps what it had printed); anywhere else, or with no usable
# directory, it is a node-local temp file. The first line printed names the file, so a run is readable even when the agent drops the
# tail of the step log. Read the server's copy with scripts/ci/wp_log.py.
ci_journal() {
  local gate="$1" cfg dir="" candidate target
  cfg="$(dirname "${BASH_SOURCE[0]}")/log-archive.yml"
  if [ -n "${CI_PIPELINE_NUMBER:-}" ] && [ -r "$cfg" ]; then
    while IFS= read -r candidate; do
      candidate="${candidate#*- }"
      if mkdir -p "$candidate" 2>/dev/null && [ -w "$candidate" ]; then dir="$candidate"; break; fi
    done < <(grep -E '^[[:space:]]+- /' "$cfg")
  fi
  if [ -n "$dir" ]; then
    mkdir -p "$dir/${CI_REPO//\//_}" 2>/dev/null || true
    target="$dir/${CI_REPO//\//_}/${CI_PIPELINE_NUMBER}-${gate}.log"
  else
    target="${TMPDIR:-/tmp}/herdr-${gate}-$$.log"
  fi
  echo "journal: $target"
  exec > >(tee "$target") 2>&1
}
