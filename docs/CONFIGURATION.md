# Configuration

## Config file

`~/.nff/config.json`, written by `nff init` and editable by hand:

```json
{
  "version": "1",
  "default_device": {
    "port": "COM10",
    "board": "ESP32 (CP210x)",
    "fqbn": "esp32:esp32:esp32",
    "baud": 115200
  },
  "build": {
    "backend": "platformio",
    "board": "esp32dev"
  }
}
```

`build.backend` selects the toolchain (`platformio` default, or `arduino`) and `build.board` holds the PlatformIO board id; the arduino backend uses `default_device.fqbn` instead. The `NFF_BUILD_BACKEND` env var overrides `build.backend` per-run. See [BOARDS.md](BOARDS.md) for what each backend covers.

## Self-update

nff keeps itself current the way Claude Code does. After a command finishes, a throttled
(default: once per 24 h) **detached background check** downloads the latest GitHub Release
binary, verifies it against the release's `SHA256SUMS`, sanity-runs it, and atomically swaps
it into place — the *next* invocation runs the new version, and a `✓ nff updated itself to
vX.Y.Z` notice appears on stderr. Nothing is added to the latency of the command you ran.

- **Only standalone installs auto-update** (the `install.sh` / `install.ps1` one-liners).
  pip/pipx/uv wheel installs (being deprecated) just get a "new version available —
  reinstall standalone" notice; dev checkouts are left alone.
- `nff update` runs the same flow in the foreground; `nff update --check` only reports
  (exit 2 when a newer release exists — scriptable).
- **If an update fails, nff calls the doctor**: `nff update` runs `nff doctor`
  automatically for diagnostics, and a failed *background* attempt is surfaced on your
  next command with a pointer to `nff update`. `nff doctor` also shows update health
  (channel, freshness, last error).
- Opt out with `NFF_NO_AUTO_UPDATE=1` (per-run) or `"update": {"auto": false}` in
  `~/.nff/config.json` (persistent); `nff update` keeps working either way.
  `NFF_UPDATE_EVERY_HOURS` tunes the cadence.
- A running `nff mcp` server keeps its old image through a swap — restart it
  (`nff mcp restart`) to pick up the new version.

Update state lives in `~/.nff/update.json`; the background job logs to `~/.nff/update.log`.

## Claude Code skills

nff ships Claude Code skills bundled inside the package:

| Skill | When to use |
|---|---|
| `/nff` | Full pipeline reference — hardware workflows, sketch-first rules, debugging checklist |

```
/nff
```

Skill files live at `nff/skills/` (the source of truth — edit them there) so they ship with every `pip install nff`, and are also mirrored in `.claude/commands/` for project-level use. Copy them into `~/.claude/commands/` to make the slash commands available globally.

> The `/wokwi-diagram` simulation skill moved to the **[nff-sim](../../nff-sim)** package.
