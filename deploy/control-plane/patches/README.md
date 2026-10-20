# Legacy frontend compatibility

`mission-attention-legacy.patch` applies the mission status labels and local
report-age clock to the deployed `1e3e8ab` frontend without importing newer
inspector or timeline modules. Current source already includes this change;
apply this patch only when rebuilding or maintaining that legacy frontend.

From the legacy source root (or a copied `/app` asset tree):

```sh
git apply --check /path/to/mission-attention-legacy.patch
git apply /path/to/mission-attention-legacy.patch
```

A failed check means the baseline differs; do not force it. The four changed
files are build-free browser modules. For a running deployment, back up these
files and record their SHA-256 hashes before replacing them. Replace
`components.js` and `status.js` first, then `index.js` and `app.js`. No daemon
restart is required. Restore the backups to roll back. Container recreation
requires rebuilding the legacy image with the patch, or building current source.

The clock only displays the age of a reported change. Pi emits finished,
crashed, or stalled attention transitions; an old timestamp is not evidence of
a stalled worker and does not trigger any network request or notification.
