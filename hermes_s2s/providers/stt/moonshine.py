"""Moonshine STT provider (UsefulSensors, MIT, local).

Ported from work originally on the Hermes feat/aria-voice branch.
Tries `useful-moonshine-onnx` first (no torch dep), falls back to `transformers`
with `MoonshineForConditionalGeneration`. Caches the loaded model
module-globally per `model_name` so subsequent calls reuse weights.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Module-level cache: {model_name: (model, kind)}
_MOONSHINE_CACHE: Dict[str, Any] = {}


def _load_moonshine_model(model_name: str):
    """Load a moonshine model. Returns (model, kind) where kind is 'onnx' or 'transformers'."""
    try:
        from moonshine_onnx import MoonshineOnnxModel  # type: ignore
        return MoonshineOnnxModel(model_name=model_name), "onnx"
    except ImportError:
        pass

    try:
        from transformers import (  # type: ignore
            AutoProcessor,
            MoonshineForConditionalGeneration,
        )
        model = MoonshineForConditionalGeneration.from_pretrained(model_name)
        processor = AutoProcessor.from_pretrained(model_name)
        return (model, processor), "transformers"
    except ImportError as exc:
        raise ImportError(
            "Moonshine requires either useful-moonshine-onnx or transformers. "
            "Install via: pip install hermes-s2s[moonshine]"
        ) from exc


class MoonshineSTT:
    """STT provider that wraps Moonshine for one-shot file transcription."""

    def __init__(self, model_name: str = "tiny") -> None:
        self.model_name = model_name

    def transcribe(self, file_path: str | Path) -> Dict[str, Any]:
        """Transcribe a WAV/FLAC/OGG file. Returns {success, transcript, provider, error?}."""
        try:
            cached = _MOONSHINE_CACHE.get(self.model_name)
            if cached is None:
                logger.info(
                    "Loading moonshine model '%s' (first load may download weights)...",
                    self.model_name,
                )
                cached = _load_moonshine_model(self.model_name)
                _MOONSHINE_CACHE[self.model_name] = cached
            model, kind = cached
        except ImportError as exc:
            return {"success": False, "transcript": "", "error": str(exc)}
        except Exception as exc:
            logger.error("moonshine load failed: %s", exc, exc_info=True)
            return {
                "success": False,
                "transcript": "",
                "error": f"Failed to load moonshine model '{self.model_name}': {exc}",
            }

        try:
            import numpy as np
            import soundfile as sf
        except ImportError as exc:
            return {
                "success": False,
                "transcript": "",
                "error": f"hermes-s2s requires soundfile + numpy (missing: {exc})",
            }

        try:
            audio, sr = sf.read(str(file_path), dtype="float32", always_2d=False)
        except FileNotFoundError:
            return {"success": False, "transcript": "", "error": f"Audio file not found: {file_path}"}
        except Exception as exc:
            return {"success": False, "transcript": "", "error": f"soundfile.read failed: {exc}"}

        # Mono downmix
        if hasattr(audio, "ndim") and audio.ndim == 2:
            audio = audio.mean(axis=1)

        # Moonshine wants 16kHz mono float32
        if sr != 16000:
            try:
                from math import gcd
                from scipy.signal import resample_poly  # type: ignore
                g = gcd(int(sr), 16000)
                up = 16000 // g
                down = int(sr) // g
                audio = resample_poly(audio, up, down).astype("float32")
                sr = 16000
            except ImportError:
                return {
                    "success": False,
                    "transcript": "",
                    "error": (
                        f"Audio is {sr}Hz; moonshine requires 16kHz and scipy is not "
                        "installed. Install via: pip install hermes-s2s[audio]"
                    ),
                }

        audio = np.asarray(audio, dtype="float32")

        try:
            if kind == "onnx":
                result = model.generate(audio[None, :])
                if isinstance(result, (list, tuple)) and result:
                    transcript = result[0]
                    if isinstance(transcript, bytes):
                        transcript = transcript.decode("utf-8", errors="replace")
                else:
                    transcript = str(result)
            else:
                hf_model, processor = model
                inputs = processor(audio, sampling_rate=sr, return_tensors="pt")
                generated = hf_model.generate(**inputs)
                transcript = processor.batch_decode(generated, skip_special_tokens=True)[0]

            transcript = str(transcript).strip()
            logger.info(
                "Transcribed %s via moonshine (%s, backend=%s, %d chars)",
                Path(file_path).name, self.model_name, kind, len(transcript),
            )
            return {"success": True, "transcript": transcript, "provider": "moonshine"}
        except Exception as exc:
            logger.error("moonshine inference failed: %s", exc, exc_info=True)
            return {"success": False, "transcript": "", "error": f"Moonshine inference failed: {exc}"}


def make_moonshine(config: Dict[str, Any]) -> MoonshineSTT:
    """Factory used by the registry."""
    model_name = (config or {}).get("model", "tiny")
    return MoonshineSTT(model_name=model_name)
