# Installing hermes-s2s

Hermes plugins live in `~/.hermes/plugins/`, so the install is a clone-then-pip
flow rather than a plain pip install. The full happy path is 5 commands; the
matrix below exists for users who want finer control.

## 30-second install

```bash
hermes plugins install baladithyab/hermes-s2s
cd ~/.hermes/plugins/hermes-s2s && pip install -e '.[all]'
hermes plugins enable hermes-s2s
hermes s2s setup --profile realtime-gemini
hermes s2s doctor
```

`hermes plugins install <owner>/<repo>` clones the GitHub repo into
`~/.hermes/plugins/hermes-s2s/` (where Hermes' plugin loader finds it). The
follow-up `pip install -e '.[all]'` installs the Python dependencies into
your active environment in editable mode so `pip install` upgrades after
`git pull` are free.

> **Why two commands?** `hermes plugins install` only handles the clone +
> manifest registration. It deliberately doesn't run `pip` — that would let
> a third-party plugin pin or remove arbitrary packages in your env without
> consent. You opt in explicitly with the `pip install -e` step.

## Pip extras matrix

After cloning into `~/.hermes/plugins/hermes-s2s/`, you can pick a smaller
extras set if you don't want the full `[all]`:

| Profile | What you get | Install |
|---|---|---|
| `core` (no extras) | Plugin scaffold + command-provider STT/TTS shims | `pip install -e .` |
| `audio` | + `scipy` for non-16 kHz resampling | `pip install -e '.[audio]'` |
| `moonshine` | + Moonshine STT (ONNX, local) | `pip install -e '.[moonshine]'` |
| `kokoro` | + Kokoro TTS (local) | `pip install -e '.[kokoro]'` |
| `local-all` | Moonshine + Kokoro + `scipy` | `pip install -e '.[local-all]'` |
| `realtime` | + `websockets` for Gemini Live / OpenAI Realtime | `pip install -e '.[realtime]'` |
| `server-client` | + `websockets` for the external s2s-server | `pip install -e '.[server-client]'` |
| `all` | Everything above in one shot | `pip install -e '.[all]'` |
| `dev` | + pytest + ruff (for contributors) | `pip install -e '.[dev]'` |

If you're new, pick `all` — it's the only profile that lets every `hermes s2s
setup --profile <x>` work without a follow-up `pip install`.

If you're CPU-constrained or on a tiny container: `realtime` alone is enough
to run Gemini Live or OpenAI Realtime in a Discord VC; Moonshine/Kokoro are
only needed for cascaded mode.

## System dependencies

Three system libraries matter:

- **`ffmpeg`** — decodes Discord's incoming Opus frames in cascaded mode, and
  is used by `hermes-s2s-tts` / `hermes-s2s-stt` for any non-WAV I/O.
- **`libopus`** — required by `discord.py` / `py-cord` for real-time Opus
  encode/decode in voice channels.
- **`espeak-ng`** — Kokoro's default grapheme-to-phoneme backend. Only needed
  if you're using Kokoro TTS (cascaded mode with the `kokoro` provider).

### Ubuntu / Debian / WSL

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg libopus0 libopus-dev espeak-ng
```

### macOS (Homebrew)

```bash
brew install ffmpeg opus espeak-ng
```

### Windows (Chocolatey)

```powershell
choco install ffmpeg opus-tools espeak-ng -y
```

Native Windows is **untested** for the realtime bridge path — we strongly
recommend WSL2 for anything beyond the CLI shims.

### Other

- **Alpine**: `apk add ffmpeg opus-dev espeak-ng`
- **Arch**: `sudo pacman -S ffmpeg opus espeak-ng`
- **Fedora**: `sudo dnf install ffmpeg opus-devel espeak-ng`

## Verifying the install

After installing the Python profile and system deps, run:

```bash
hermes s2s doctor
```

You should see a checklist with one of: `✓` (pass), `⚠` (warning), `✗` (fail)
for each of six categories:

1. **Configuration** — is `s2s.mode` + provider block present in
   `~/.hermes/config.yaml`?
2. **Python dependencies** — are `websockets`, `scipy`, `moonshine_onnx`
   (the import name; pip name is `useful-moonshine-onnx`), `kokoro`
   importable (as required by the configured mode)?
3. **System dependencies** — are `ffmpeg`, `libopus`, `espeak-ng` in `PATH`?
4. **API keys** — is the relevant API key set (`GEMINI_API_KEY`,
   `OPENAI_API_KEY`), and is it a plausible length?
5. **Hermes integration** — is `HERMES_S2S_MONKEYPATCH_DISCORD=1` exported
   (realtime mode only)? Is `DISCORD_BOT_TOKEN` + `DISCORD_ALLOWED_USERS`
   visible to Hermes?
6. **Backend connectivity** — opens a 5 s WS probe to the configured realtime
   backend. Skipped with `--no-probe`; emits a tiny charge (~$0.0001) otherwise.

Exit code is 0 on all-green or warnings-only, 1 on any `✗`.
`hermes s2s doctor --json` emits the same data machine-readable for CI.

## Common install errors and fixes

**`Plugin 'hermes-s2s' is not installed or bundled.`**
You ran `hermes plugins enable hermes-s2s` but skipped the
`hermes plugins install baladithyab/hermes-s2s` step that puts the manifest
under `~/.hermes/plugins/`. Hermes only discovers plugins that have a
`plugin.yaml` in that directory tree — pip-installing into site-packages
isn't enough.

**`hermes: error: argument command: invalid choice: 's2s'`**
The plugin isn't loaded in this Hermes process. Either `hermes plugins enable
hermes-s2s` hasn't run, or the plugin failed to import (check the activation
log: `hermes plugins list -v`). Common cause: `pip install -e '.[all]'`
hasn't run, so `hermes_s2s` itself isn't on `sys.path`.

**`ModuleNotFoundError: No module named 'scipy'` from the bridge.**
You installed a profile without `scipy`. Either `pip install -e '.[audio]'`
or switch to `pip install -e '.[all]'`.

**`OpusNotLoaded` / `discord.opus.OpusNotLoaded` when joining a VC.**
`libopus` isn't on your system PATH. Install it per your OS above. On macOS
with Apple Silicon, you may also need to point `discord` at the Homebrew-
installed dylib — see the discord.py docs for `discord.opus.load_opus()`.

**`espeak-ng not found` when running Kokoro TTS.**
`sudo apt install espeak-ng` (or OS equivalent). Kokoro's default phonemizer
shells out to it; there's no pure-Python fallback in the current release.

**`websockets.exceptions.InvalidStatusCode: 401` from the doctor WS probe.**
Your API key is wrong or expired. Re-check `GEMINI_API_KEY` /
`OPENAI_API_KEY` in `~/.hermes/.env` — the key must be copied whole, with
no trailing newline or quote characters.

**`hermes s2s setup --profile realtime-gemini` writes the config but voice
mode is still silent in Discord.**
Almost always the monkey-patch flag: check `~/.hermes/.env` contains
`HERMES_S2S_MONKEYPATCH_DISCORD=1` and that you've restarted
`hermes gateway` (env is only re-read on process start). `hermes s2s doctor`
detects and flags this exact case.

**`No matching distribution found for moonshine-onnx`.**
The PyPI package is `useful-moonshine-onnx`, not `moonshine-onnx`. Fixed in
v0.3.3 — `git pull` from `~/.hermes/plugins/hermes-s2s/` and re-run
`pip install -e '.[moonshine]'`.

**`pip install -e '.[all]'` is slow on first run.**
That's `scipy` + `kokoro` + `useful-moonshine-onnx` downloading wheels
(~50 MB total). No workaround; subsequent installs use the wheel cache.

For anything not on this list, open an issue at
<https://github.com/baladithyab/hermes-s2s/issues> and attach the full
`hermes s2s doctor --json` output.
