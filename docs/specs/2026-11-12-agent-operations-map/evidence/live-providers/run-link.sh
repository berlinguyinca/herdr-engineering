#!/bin/sh
L=${SCRATCH:-/tmp/ao-live}
P=${REPO:-$HOME/IdeaProjects/herdr-engineering}/deploy/herdr-agent-ops-plugin
rm -rf $L/herdr-home && mkdir -p $L/herdr-home/h $L/herdr-home/cfg
for v in $(env | grep -o '^HERDR_[A-Z_]*'); do unset $v; done
export HOME=$L/herdr-home XDG_CONFIG_HOME=$L/herdr-home/cfg XDG_DATA_HOME=$L/herdr-home/data XDG_STATE_HOME=$L/herdr-home/state
export HERDR_HOME=$L/herdr-home/h HERDR_SOCKET_PATH=$L/herdr-home/h/herdr.sock HERDR_CONFIG_PATH=$L/herdr-home/h/config.toml
echo "--- link $P"; ~/.local/bin/herdr plugin link "$P"; echo "exit=$?"
echo "--- list"; ~/.local/bin/herdr plugin list; echo "exit=$?"
echo "--- action list"; ~/.local/bin/herdr plugin action list 2>&1 | head -20
echo "--- tree"; find $L/herdr-home | head -40
