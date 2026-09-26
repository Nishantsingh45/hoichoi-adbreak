"""Demo server: player UI, outputs, and a small job API so new videos and brands can be processed live."""
import json
import re
import threading
import time
import traceback
import uuid
from pathlib import Path

import os
import shutil

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from adbreak import brands as brand_lib  # noqa: E402  (config reads env at import)
from adbreak import budget, media, pipeline  # noqa: E402

# Sample episodes are not stored on the server: they are streamed from object storage (Cloudflare R2).
# Uploaded episodes live on local disk, one at a time.
SAMPLES = set(json.loads((ROOT / "samples.json").read_text(encoding="utf-8"))["videos"])
VIDEO_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/")
OUT = ROOT / "out"
VIDEOS = ROOT / "data" / "videos"
BRANDS = ROOT / "brands" / "brands.json"
CUSTOM_BRANDS = ROOT / "brands" / "custom.json"
for d in (ROOT / "out" / "ads", VIDEOS):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="hoichoi ad-break planner")
jobs: dict[str, dict] = {}
worker = threading.Lock()  # one pipeline run at a time keeps CPU and spend predictable


def brand_files() -> list[Path]:
    return [BRANDS, CUSTOM_BRANDS] if CUSTOM_BRANDS.exists() else [BRANDS]


def _run_job(job: dict, fetch=None):
    def log(line: str):
        job["log"].append(line)

    with worker:
        job["status"] = "running"
        try:
            if fetch:
                fetch(log)
            pipeline.process(job["video_path"], brand_files(), set(job.get("redo", [])), log=log)
            job["status"] = "done"
        except budget.BudgetExceeded as e:
            job["status"], job["error"] = "error", str(e)
        except Exception as e:
            traceback.print_exc()
            job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"


def _start(video_path: Path, fetch=None, redo=()) -> dict:
    job = {"id": uuid.uuid4().hex[:8], "video_id": video_path.stem, "video_path": video_path,
           "status": "queued", "log": [], "error": None, "redo": list(redo), "created": time.time()}
    jobs[job["id"]] = job
    threading.Thread(target=_run_job, args=(job, fetch), daemon=True).start()
    return _public(job)


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "video_path"}


def _drive_direct(url: str) -> str:
    m = re.search(r"/d/([\w-]{20,})", url) or re.search(r"[?&]id=([\w-]{20,})", url)
    return f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download&confirm=t" if m else url


def _source(video_id: str):
    """Local file if present, else the sample's copy in object storage."""
    vid = brand_lib.safe_id(video_id)
    local = VIDEOS / f"{vid}.mp4"
    if local.exists():
        return local
    if vid in SAMPLES and VIDEO_BASE_URL:
        return f"{VIDEO_BASE_URL}/{vid}.mp4"
    raise HTTPException(404, "video not found")


def _busy() -> bool:
    return any(j["status"] in ("queued", "running") for j in jobs.values())


def _replace_previous_upload():
    """Only one uploaded episode is kept, so the server's small disk never fills up."""
    for f in VIDEOS.glob("*.mp4"):
        vid = f.stem
        if vid in SAMPLES:
            continue
        f.unlink(missing_ok=True)
        shutil.rmtree(OUT / vid, ignore_errors=True)
        index_path = OUT / "index.json"
        if index_path.exists():
            index = [v for v in json.loads(index_path.read_text(encoding="utf-8")) if v["video_id"] != vid]
            index_path.write_text(json.dumps(index, indent=1), encoding="utf-8")


def _new_upload_slot(name: str) -> Path:
    if _busy():
        raise HTTPException(409, "Another episode is being processed; please wait for it to finish.")
    _replace_previous_upload()
    return VIDEOS / f"{brand_lib.safe_id(name or 'video')}_{uuid.uuid4().hex[:4]}.mp4"


def _check_length(source) -> None:
    limit = budget.max_video_minutes()
    minutes = media.probe(media.src(source))["duration"] / 60
    if minutes > limit:
        raise HTTPException(422, f"This video is {minutes:.0f} min; the limit is {limit:.0f} min.")


class UrlJob(BaseModel):
    url: str
    name: str | None = None


@app.post("/api/jobs/url")
def job_from_url(req: UrlJob):
    direct = _drive_direct(req.url)
    try:
        _check_length(direct)  # reads only the header, before downloading anything
    except HTTPException:
        raise
    except Exception:
        pass  # not probeable remotely; the pipeline checks again after download
    path = _new_upload_slot(req.name or Path(req.url.split("?")[0]).stem)

    def fetch(log):
        log(f"[download] {req.url}")
        with httpx.stream("GET", direct, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        log(f"[download] {path.stat().st_size / 1e6:.1f} MB")

    return _start(path, fetch)


@app.post("/api/jobs/upload")
async def job_from_upload(file: UploadFile = File(...)):
    path = _new_upload_slot(Path(file.filename or "video").stem)
    with open(path, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    try:
        _check_length(path)
    except HTTPException:
        path.unlink(missing_ok=True)
        raise
    return _start(path)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "unknown job")
    return _public(jobs[job_id])


@app.get("/api/brands")
def list_brands():
    official = {b["id"] for b in brand_lib.load([BRANDS])}
    return [{**b, "custom": b["id"] not in official} for b in brand_lib.load(brand_files())]


class BrandIn(BaseModel):
    brand: dict


@app.post("/api/brands")
def add_brand(req: BrandIn):
    """Accepts a brand object exactly as it appears in brands.json; nothing else changes."""
    try:
        normalised = brand_lib.normalise(req.brand)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(422, f"invalid brand: {e}")
    custom = json.loads(CUSTOM_BRANDS.read_text(encoding="utf-8")) if CUSTOM_BRANDS.exists() else []
    custom = [x for x in custom if brand_lib.safe_id(x.get("brand_id") or x.get("id", "")) != normalised["id"]]
    custom.append(req.brand)
    CUSTOM_BRANDS.write_text(json.dumps(custom, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "brand": normalised, "brands": len(list_brands())}


@app.delete("/api/brands/custom")
def reset_custom_brands():
    CUSTOM_BRANDS.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/rematch/{video_id}")
def rematch(video_id: str):
    """Re-run pacing + brand matching only (scene analysis and cut judgments are cached)."""
    if _busy():
        raise HTTPException(409, "Another job is running; please wait for it to finish.")
    return _start(_source(video_id))


@app.post("/api/reanalyse/{video_id}")
def reanalyse(video_id: str):
    """Throw away the cached Gemini scene notes and Jev judgments and redo them live (nothing is pre-baked)."""
    if _busy():
        raise HTTPException(409, "Another job is running; please wait for it to finish.")
    return _start(_source(video_id), redo=["understand"])


@app.get("/videos/{name}")
def video(name: str):
    """Uploaded episodes are served from local disk; sample episodes redirect to object storage."""
    vid = brand_lib.safe_id(Path(name).stem)
    local = VIDEOS / f"{vid}.mp4"
    if local.exists():
        return FileResponse(local, media_type="video/mp4")
    if vid in SAMPLES and VIDEO_BASE_URL:
        return RedirectResponse(f"{VIDEO_BASE_URL}/{vid}.mp4", status_code=307)
    raise HTTPException(404, "video not found")


@app.get("/api/budget")
def budget_status():
    return {"spent_usd": round(budget.spent(), 4), "budget_usd": budget.budget_usd()}


app.mount("/ads", StaticFiles(directory=ROOT / "out" / "ads"), name="ads")
app.mount("/out", StaticFiles(directory=ROOT / "out"), name="out")
app.mount("/web", StaticFiles(directory=ROOT / "web", html=True), name="web")


@app.get("/")
def home():
    return RedirectResponse("/web/")
