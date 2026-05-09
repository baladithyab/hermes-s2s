# ARIA Local Pipeline Research: Moonshine STT + Kokoro TTS + OpenRouter LLM

Target hardware: RTX 5090 (32GB VRAM). Python backend.

## 1. Moonshine STT (Useful Sensors)

- **Models**: `tiny` (27M params, ~27MB) / `base` (61M params, ~58MB). v2 adds "Ergodic Streaming Encoder".
- **License**: MIT (open-weights).
- **HF repo**: https://huggingface.co/UsefulSensors/moonshine
- **Install**: `pip install useful-moonshine` (Keras; set `KERAS_BACKEND=torch`). Newer edge package: `pip install moonshine-voice`. ONNX variant: `pip install moonshine-onnx`.
- **Streaming**: Yes — v2 (Oct 2025) has a streaming encoder designed for latency-critical apps; official demo at `useful-sensors/moonshine` repo includes live mic streaming.
- **Latency (consumer GPU)**: ~5× faster than Whisper-tiny/base on same hardware; on RTX-class GPU a 1–2s audio chunk transcribes in **~20–60ms** (RTF ≈ 0.02–0.05). On CPU/RPi5 it runs real-time.
- **WER vs Whisper-base**: Moonshine `tiny` ≈ Whisper `tiny`; Moonshine `base` (61M) **matches or beats Whisper `base` (74M) and Whisper `small` (244M)** on standard English benchmarks per paper + Gladia/Northflank roundups. Comparable to Whisper Large v3 on some clean benchmarks at 6× fewer params. Less hallucination during silences than Whisper.
- Sources: https://huggingface.co/UsefulSensors/moonshine , https://www.gladia.io/blog/best-open-source-speech-to-text-models , https://www.onresonant.com/resources/local-stt-models-2026

## 2. Kokoro TTS (hexgrad)

- **Model size**: 82M params, weights ~330MB fp32 / ~160MB fp16. Architecture: StyleTTS2 + ISTFTNet decoder-only.
- **License**: Apache 2.0 (weights + code).
- **HF repo**: https://huggingface.co/hexgrad/Kokoro-82M
- **Voices**: v1.0 ships 54 voices across 8 langs. English (US/UK), male+female. Notable: `af_heart`, `af_nicole`, `am_adam`, `bf_emma`. Languages: en, ja, zh, es, fr, hi, it, pt-br.
- **Install**: `pip install kokoro>=0.9.4 soundfile` + system `espeak-ng` (G2P fallback). Python ≥3.9. Uses `misaki` G2P under the hood.
- **Streaming**: Yes — `KPipeline(text, voice=...)` is a **generator** that yields audio chunks per phrase/graphemes tuple `(gs, ps, audio)` at 24kHz. True chunked streaming, not whole-utterance only.
- **Latency (short utterance)**: Sub-second first-chunk. Replicate cog benchmark: 352-char sentence → predict_time **1.4s total** including cold load; per-chunk first-audio on warm GPU typically **80–200ms** on RTX-class hw. <2GB VRAM, runs faster than realtime.
- **Quality vs ElevenLabs**: Informally — punches well above its weight; top-10 on TTS Arena at release, beating many much larger open models. Not as emotionally expressive or zero-shot-clonable as ElevenLabs Turbo/v3, but prosody on the curated voices (esp. `af_heart`, `af_bella`) is natural and conversational. "Good enough" for a voice assistant; not for audiobook narration demanding sub-ElevenLabs clone quality.
- Sources: https://github.com/hexgrad/kokoro , https://huggingface.co/hexgrad/Kokoro-82M , https://docs.clore.ai/guides/audio-and-voice/kokoro-tts

## 3. VAD Recommendation: **silero-vad**

- Pick silero-vad over webrtcvad.
- 1.8MB JIT/ONNX model, runs on CPU in ~1ms per 30ms frame; dramatically fewer false positives on music/noise/room tone than webrtcvad (which is a simple GMM from 2011).
- Native PyTorch + ONNX; trivial `pip install silero-vad`; returns probabilities (not just hard 0/1), enabling hysteresis thresholding for barge-in. webrtcvad only handles 8/16/32kHz PCM 10/20/30ms frames with binary output — too noisy for LLM-triggered turn-taking.

## 4. End-to-end Timing Budget (RTX 5090, streaming)

| Stage                    | Latency      | Notes |
|--------------------------|--------------|-------|
| VAD-trigger (end-of-utt) | 150–300ms    | silero-vad with ~200ms hangover; dominated by waiting for silence to confirm turn end |
| STT-chunk (Moonshine)    | 30–80ms      | base model on ~1–3s utterance, RTF ~0.03 on 5090 |
| LLM first-token (OpenRouter) | 250–600ms | Network RTT + TTFT; fastest routes (Groq/Cerebras via OR, or Llama-3 8B) can hit <200ms; GPT-4o-mini ~400ms |
| TTS first-audio (Kokoro)  | 80–200ms    | First chunk from first phrase; streams in parallel with subsequent LLM tokens |
| **Total (first audio out)** | **~550–1100ms** | Tight budget target <800ms; achievable if LLM TTFT <400ms |

Notes: STT and TTS overlap with LLM streaming after first token. VAD-trigger is the hardest floor to reduce (can go to ~100ms with aggressive endpointing but raises interruption rate).

## 5. GPU VRAM Footprint (all 3 loaded, fp16)

| Component        | VRAM   |
|------------------|--------|
| Moonshine base   | ~0.3 GB |
| Kokoro-82M       | ~0.6 GB (spec says <2GB ceiling incl. buffers) |
| silero-vad       | ~0.05 GB (often CPU) |
| CUDA/torch overhead + audio buffers | ~1.0 GB |
| **Total**        | **~2–3 GB** |

LLM is remote (OpenRouter) → zero local VRAM. Leaves 29GB free on RTX 5090 for a local LLM fallback (e.g., Llama-3.1-70B-Q4 or Qwen2.5-32B) if desired later.

## 6. Reference Implementation: **Pipecat**

Pick **Pipecat** (https://github.com/pipecat-ai/pipecat) over LiveKit Agents and RealtimeSTT.

1. Pipeline mental model matches our architecture exactly: Transport → VAD → STT → LLM → TTS → Transport. Pythonic, provider-agnostic.
2. Already has first-class `MoonshineSTTService` and `KokoroTTSService` processors shipped in `pipecat-ai/pipecat` plugins — zero glue code.
3. OpenRouter works out of the box via `OpenAILLMService(base_url="https://openrouter.ai/api/v1")` — any OpenAI-compatible endpoint.
4. Interruption handling ("Interruptible Frames") propagates cancel signals through the pipeline on barge-in — hard to replicate manually.
5. LiveKit Agents is overkill (WebRTC SFU + rooms) for a single-user desktop assistant; RealtimeSTT is STT-only and would need DIY TTS+LLM orchestration.

## 7. Python Skeleton (~50 lines)

```python
# aria/local_pipeline.py
import asyncio, os, queue, numpy as np, sounddevice as sd
from silero_vad import load_silero_vad, VADIterator
from moonshine_onnx import MoonshineOnnxModel, load_tokenizer
from kokoro import KPipeline
from openai import AsyncOpenAI

SR = 16000
vad = load_silero_vad(); vad_it = VADIterator(vad, sampling_rate=SR, threshold=0.5,
                                              min_silence_duration_ms=300)
stt = MoonshineOnnxModel(model_name="moonshine/base")
tok = load_tokenizer()
tts = KPipeline(lang_code='a')  # American English
llm = AsyncOpenAI(base_url="https://openrouter.ai/api/v1",
                  api_key=os.environ["OPENROUTER_API_KEY"])

audio_out_q: asyncio.Queue = asyncio.Queue()

async def tts_stream(text_iter):
    buf = ""
    async for tok_text in text_iter:
        buf += tok_text
        if any(p in buf for p in ".!?") or len(buf) > 80:
            for _, _, audio in tts(buf, voice="af_heart"):
                await audio_out_q.put(audio.astype(np.float32))
            buf = ""
    if buf.strip():
        for _, _, audio in tts(buf, voice="af_heart"):
            await audio_out_q.put(audio.astype(np.float32))

async def llm_stream(user_text):
    resp = await llm.chat.completions.create(
        model="openai/gpt-4o-mini", stream=True,
        messages=[{"role": "user", "content": user_text}])
    async for chunk in resp:
        delta = chunk.choices[0].delta.content
        if delta: yield delta

async def handle_utterance(pcm_f32):
    text = stt.generate(pcm_f32[None, :])[0]
    text = tok.decode_batch(text)[0]
    print(f"USER: {text}")
    await tts_stream(llm_stream(text))

async def mic_loop():
    speech_buf, in_speech = [], False
    def cb(indata, frames, t, status):
        asyncio.get_event_loop().call_soon_threadsafe(in_q.put_nowait, indata.copy())
    in_q: asyncio.Queue = asyncio.Queue()
    with sd.InputStream(channels=1, samplerate=SR, blocksize=512,
                        dtype='float32', callback=cb):
        while True:
            frame = (await in_q.get()).squeeze()
            ev = vad_it(frame, return_seconds=False)
            if ev and "start" in ev: in_speech, speech_buf = True, [frame]
            elif in_speech: speech_buf.append(frame)
            if ev and "end" in ev and in_speech:
                pcm = np.concatenate(speech_buf); speech_buf, in_speech = [], False
                asyncio.create_task(handle_utterance(pcm))

async def player():
    with sd.OutputStream(channels=1, samplerate=24000, dtype='float32') as out:
        while True:
            out.write(await audio_out_q.get())

async def main():
    await asyncio.gather(mic_loop(), player())

if __name__ == "__main__":
    asyncio.run(main())
```

Notes: production version should add barge-in (drain `audio_out_q` on new VAD start), STT warmup, Kokoro GPU placement (`torch.device('cuda')`), and OpenRouter model routing.

## References

- Moonshine HF: https://huggingface.co/UsefulSensors/moonshine
- Moonshine GitHub: https://github.com/usefulsensors/moonshine
- Kokoro GitHub: https://github.com/hexgrad/kokoro
- Kokoro HF: https://huggingface.co/hexgrad/Kokoro-82M
- Pipecat: https://github.com/pipecat-ai/pipecat
- silero-vad: https://github.com/snakers4/silero-vad
- Gladia STT roundup 2026: https://www.gladia.io/blog/best-open-source-speech-to-text-models
- Pipecat vs LiveKit: https://sellerity.co/blog/livekit-pipecat-web-voice-agents
- LiveKit pipeline vs realtime: https://livekit.com/blog/realtime-vs-cascade
