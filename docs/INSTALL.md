# Installing hermes-s2s

Pick an install profile that matches your mode, install the right system deps
for your OS, then verify with `hermes s2s doctor`. The full happy path is
4 commands; the matrix below exists for users who want finer control.

## Pip install profiles

> **Note**: Until we publish to PyPI, each `pip install ...` below must include
> the suffix `@ git+https://github.com/baladithyab/hermes-s2s.git@v0.3.2` after
> the extras spec. Once we publish to PyPI, drop the `@ git+...@v0.3.2` suffix.

| Profile | What you get | Install command |
|---|---|---|
| `core` | Plugin scaffold + command-provider STT/TTS shims | `pip install 'hermes-s2s @ git+https://github.com/baladithyab/hermes-s2s.git@v0.3.2'` |
| `audio` | + `scipy` for non-16 kHz resampling | `pip install 'hermes-s2s[audio] @ git+https://github.com/baladithyab/hermes-s2s.git@v0.3.2'` |
| `moonshine` | + Moonshine STT (ONNX, local) | `pip install 'hermes-s2s[moonshine] @ git+https://github.com/baladithyab/hermes-s2s.git@v0.3.2'` |
| `kokoro` | + Kokoro TTS (local) | `pip install 'hermes-s2s[kokoro] @ git+https://github.com/baladithyab/hermes-s2s.git@v0.3.2'` |
| `local-all` | Moonshine + Kokoro + `scipy` | `pip install 'hermes-s2s[local-all] @ git+https://github.com/baladithyab/hermes-s2s.git@v0.3.2'` |
| `realtime` | + `websockets` for Gemini Live / OpenAI Realtime | `pip install 'hermes-s2s[realtime] @ git+https://github.com/baladithyab/hermes-s2s.git@v0.3.2'` |
| `server-client` | + `websockets` for the external s2s-server | `pip install 'hermes-s2s[server-client] @ git+https://github.com/baladithyab/hermes-s2s.git@v0.3.2'` |
| `all` | Everything above in one shot | `pip install 'hermes-s2s[all] @ git+https://github.com/baladithyab/hermes-s2s.git@v0.3.2'` |
| `dev` | + pytest + ruff (for contributors) | `pip install 'hermes-s2s[dev] @ git+https://github.com/baladithyab/hermes-s2s.git@v0.3.2'` |

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
2. **Python dependencies** — are `websockets`, `scipy`, `moonshine-onnx`,
   `kokoro` importable (as required by the configured mode)?
3. **System dependencies** — are `ffmpeg`, `libopus`, `espeak-ng` in `PATH`?
4. **API keys** — is the relevant API key set (`GEMINI_API_KEY`,
   `OPENAI_API_KEY`), and is it a plausible length?
5. **Hermes integration** — is `HERMES_S2S_MONKEYPATCH_DISCORD=1` exported
   (realtime mode only)? Is `DISCORD_BOT_TOKEN` + `DISCORD_ALLOWED_USERS`
   visible to Hermes?
6. **Backend connectivity** — opens a 5 s WS probe to the configured realtime
   backend. Skipped with `--no-probe`; emits a tiny charge (~$0.0001) otherwise.

Exit code is 0 on all-green, 1 on any `✗`. `hermes s2s doctor --json` emits
the same data machine-readable for CI.

## Common install errors and fixes

**`ModuleNotFoundError: No module named 'scipy'` from the bridge.**
You installed a profile without `scipy`. Either `pip install 'hermes-s2s[audio]'`
or switch to `hermes-s2s[all]`.

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

**`pip install hermes-s2s[all]` is slow on first run.**
That's `scipy` compiling or downloading a wheel (~20 MB). No workaround;
subsequent installs use the wheel cache.

For anything not on this list, open an issue at
<https://github.com/baladithyab/hermes-s2s/issues> and attach the full
`hermes s2s doctor --json` output.
