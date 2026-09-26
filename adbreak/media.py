"""ffmpeg-based media helpers: probing, audio extraction, shot-cut detection."""
import json
import re
import subprocess
from pathlib import Path

import numpy as np


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)


def src(video) -> str:
    """ffmpeg input for a local file (absolute, so it survives cwd changes) or a remote URL."""
    return str(video.resolve()) if isinstance(video, Path) else str(video)


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
               "-of", "json", str(path)]).stdout
    info = json.loads(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    num, den = video["r_frame_rate"].split("/")
    return {
        "duration": float(info["format"]["duration"]),
        "width": video["width"],
        "height": video["height"],
        "fps": float(num) / float(den),
        "video_codec": video["codec_name"],
    }


def make_proxy(path: Path, out: Path) -> Path:
    """Small upload copy for Gemini: it samples 1 fps at low resolution anyway, so 320px @ 2 fps with
    speech-quality mono audio loses nothing it uses, at ~1/7 the size. Timestamps are preserved."""
    run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vf", "scale=320:-2,fps=2",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
         "-c:a", "aac", "-ac", "1", "-b:a", "48k", "-movflags", "+faststart", str(out)])
    return out


def extract_audio(path: Path, wav_out: Path) -> Path:
    run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_out)])
    return wav_out


def scene_scores(path: Path, workdir: Path, black_min_dur: float = 0.2, black_pix_th: float = 0.10):
    """One decode pass on a downscaled stream: per-frame scene-change score (0-1) and black-frame runs."""
    report = "scores_raw.txt"
    proc = run(["ffmpeg", "-y", "-v", "info", "-i", src(path), "-an",
                "-vf", f"scale=160:-2,blackdetect=d={black_min_dur}:pix_th={black_pix_th},"
                       f"select='gte(scene,0)',metadata=print:file={report}",
                "-f", "null", "-"], cwd=workdir)
    times, scores, current = [], [], None
    for line in (workdir / report).read_text(encoding="utf-8").splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            current = float(m.group(1))
            continue
        m = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if m and current is not None:
            times.append(current)
            scores.append(float(m.group(1)))
            current = None
    (workdir / report).unlink(missing_ok=True)
    blacks = [[round(float(m.group(1)), 3), round(float(m.group(2)), 3)]
              for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", proc.stderr)]
    return np.array(times), np.array(scores), blacks


def detect_shots_and_black(path: Path, workdir: Path, min_score: float = 0.08, ratio: float = 4.0,
                           window: int = 12) -> tuple[list[dict], list[list[float]]]:
    """Adaptive hard-cut detection plus fade-to-black runs (the strongest 'act break' signal in episodic drama).
    A frame is a cut when its change score is a local peak that stands well above the surrounding motion
    level; a fixed global threshold misses soft cuts in dimly graded dramas."""
    times, scores, blacks = scene_scores(path, workdir)
    shots = []
    for i in range(len(scores)):
        s = scores[i]
        if s < min_score:
            continue
        lo, hi = max(0, i - window), min(len(scores), i + window + 1)
        neighbours = np.concatenate([scores[lo:i], scores[i + 1:hi]])
        if s < scores[max(0, i - 3):i + 4].max():
            continue
        if neighbours.size and s >= ratio * (np.median(neighbours) + 0.005):
            shots.append({"t": round(float(times[i]), 3), "score": round(float(s), 3)})
    return shots, blacks
