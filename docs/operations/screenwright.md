# Screenwright (tracked upstream)

`scripts/install.sh` step 7d runs `scripts/install-screenwright.sh`, which keeps
[berlinguyinca/screenwright](https://github.com/berlinguyinca/screenwright) at the latest `origin/main`.
It is the one upstream in `lock/upstreams.yaml` that is deliberately not pinned.

```sh
scripts/install.sh                         # includes it
scripts/install-screenwright.sh            # just this step
scripts/install.sh --no-screenwright       # opt out (or HERDR_INSTALL_SCREENWRIGHT=0)
```

| Run | Result |
|---|---|
| first | clone to `~/.local/share/herdr-engineering/screenwright` |
| later | `git fetch`, fast-forward to `origin/main` |
| dirty checkout, other branch, or diverged main | left untouched, message on stderr, exit 0 |
| clone/fetch fails | non-zero exit; `install.sh` warns and continues |

- After a move of HEAD (or when `node_modules/.bin/tsx` is missing) it runs `npm install` in the checkout, which is what
  `skills/screenwright-review/scripts/sw` asks for; a failure only warns.
- It then runs the checkout's `skills/screenwright-review/scripts/ensure-install.sh`, which symlinks the skill into
  `~/.agents/skills` and the four harness skill dirs and never overwrites a real file (a conflict only warns).
  `sw` finds the checkout through that symlink, or `$SCREENWRIGHT_HOME`.
- The last stdout line is `screenwright=<short sha> changed=<true|false>`; the resolved sha is recorded only there.
- Env: `HERDR_SCREENWRIGHT_URL` (default `git@github.com:berlinguyinca/screenwright.git`, fetched with ssh `BatchMode`),
  `HERDR_SCREENWRIGHT_DIR`, `HERDR_SCREENWRIGHT_REF` (pin a tag or sha, detached, instead of tracking main),
  `HERDR_SCREENWRIGHT_NPM=0` (skip npm).
