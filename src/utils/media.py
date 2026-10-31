from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.config.models import VideoDecompositionConfig
from src.utils.i18n import InputError
from src.utils.logger import get_logger

logger = get_logger("media")

_PROBE_TIMEOUT_SECONDS = 30
_EXTRACT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class VideoDecomposition:
    """Sampled JPEG frames, their source timestamps and the optional WAV audio track."""

    frames: list[bytes]
    timestamps: list[float]
    duration: float
    sampled_seconds: float
    truncated: bool
    audio_wav: bytes | None


def decompose_video(data: bytes, name: str, config: VideoDecompositionConfig) -> VideoDecomposition:
    """Sample frames at a fixed interval and extract the audio track with ffmpeg.

    Decoding stops after the last sampled frame and the audio extraction skips video
    decoding, so long videos cost only as much CPU as the sampled window requires.
    """
    ffmpeg = _tool("ffmpeg")
    ffprobe = _tool("ffprobe")
    interval = config.frame_interval_seconds
    with tempfile.TemporaryDirectory(prefix="recall_video_") as tmp:
        tmp_dir = Path(tmp)
        source = tmp_dir / "source"
        source.write_bytes(data)

        duration, has_audio = _probe(ffprobe, source, name)
        candidates = math.ceil(duration / interval) if duration > 0 else 1
        count = max(1, min(config.max_frames, candidates))
        truncated = candidates > count

        frames_dir = tmp_dir / "frames"
        frames_dir.mkdir()
        _run(name, [
            ffmpeg, "-v", "error", "-threads", "2",
            "-t", f"{count * interval:.3f}", "-i", str(source),
            "-an", "-map", "0:v:0",
            "-vf", (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval:g})',"
                    f"scale='min({config.max_width},iw)':-2"),
            "-fps_mode", "passthrough", "-frames:v", str(count), "-q:v", "4",
            str(frames_dir / "frame_%04d.jpg"),
        ], timeout=_EXTRACT_TIMEOUT_SECONDS)
        frame_files = sorted(frames_dir.glob("frame_*.jpg"))
        if not frame_files:
            raise InputError("could not extract frames from video '{name}'", name=name)
        frames = [path.read_bytes() for path in frame_files]
        timestamps = [index * interval for index in range(len(frames))]

        audio_wav = None
        if has_audio:
            wav = tmp_dir / "audio.wav"
            _run(name, [
                ffmpeg, "-v", "error", "-i", str(source),
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                "-f", "wav", str(wav),
            ], timeout=_EXTRACT_TIMEOUT_SECONDS)
            audio_wav = wav.read_bytes()

    logger.debug(
        "decomposed video=%s frames=%d duration=%.2fs audio=%s truncated=%s",
        name, len(frames), duration, audio_wav is not None, truncated,
    )
    return VideoDecomposition(
        frames=frames,
        timestamps=timestamps,
        duration=duration,
        sampled_seconds=count * interval,
        truncated=truncated,
        audio_wav=audio_wav,
    )


def _tool(executable: str) -> str:
    path = shutil.which(executable)
    if path is None:
        raise InputError("video input for models without video support requires {tool} on the PATH", tool=executable)
    return path


def _probe(ffprobe: str, source: Path, name: str) -> tuple[float, bool]:
    result = _run(name, [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_entries", "format=duration:stream=codec_type", str(source),
    ], timeout=_PROBE_TIMEOUT_SECONDS)
    try:
        info = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise InputError("could not read video '{name}'", name=name) from exc
    try:
        duration = max(0.0, float(info.get("format", {}).get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0.0
    has_audio = any(stream.get("codec_type") == "audio" for stream in info.get("streams", []))
    return duration, has_audio


def _run(name: str, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise InputError("video '{name}' took too long to process", name=name) from exc
    if result.returncode != 0:
        logger.debug("%s failed for video=%s: %s", Path(command[0]).name, name, result.stderr.strip())
        raise InputError("could not process video '{name}'", name=name)
    return result
