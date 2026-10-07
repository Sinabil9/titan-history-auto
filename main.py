from __future__ import annotations

import html
import json
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
CAT = json.loads((ROOT / "config" / "topics_catalog.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "state" / "history.json"
WORK = ROOT / "work"
OUT = ROOT / "output"
WORK.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)

UA = "TitanHistoryAuto/3.0 (educational YouTube pipeline; GitHub Actions)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})


def log(*args):
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)


def run(cmd, check=True, capture=False, input_text=None, cwd=None):
    cmd = [str(x) for x in cmd]
    log("RUN:", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        input=input_text,
        capture_output=capture,
        cwd=str(cwd) if cwd else None,
    )


def ffprobe_duration(path: Path) -> float:
    r = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture=True,
    )
    return float(r.stdout.strip())


def load_state():
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        data.setdefault("used_topics", [])
        data.setdefault("uploads", [])
        return data
    except Exception:
        return {"used_topics": [], "uploads": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_PATH)


def get_json(url, params=None, tries=5, timeout=30):
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            log(f"HTTP retry {attempt}/{tries}:", exc)
            if attempt < tries:
                time.sleep(min(12, 2 * attempt))
    raise RuntimeError(f"HTTP request failed after {tries} attempts: {last}")


def get_wikipedia(title: str):
    api = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "format": "json",
        "redirects": "1",
        "prop": "extracts|info",
        "inprop": "url",
        "exintro": "1",
        "explaintext": "1",
        "titles": title,
    }
    try:
        data = get_json(api, params=params)
        page = next(iter(data["query"]["pages"].values()))
        extract = (page.get("extract") or "").strip()
        if page.get("missing") is not None or len(extract) < 420:
            return None
        return {
            "title": page.get("title", title),
            "extract": extract,
            "url": page.get("fullurl")
            or f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
        }
    except Exception as exc:
        log("Wikipedia failed:", exc)
        return None


def split_sentences(text: str):
    text = re.sub(r"\[[^\]]+\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        sentence = sentence.strip()
        words = sentence.split()
        if 6 <= len(words) <= 46:
            out.append(sentence)
    return out


def build_script(source):
    """Evidence-only script: no cloud LLM and no invented facts."""
    min_words = int(CFG.get("target_words_min", 105))
    max_words = int(CFG.get("target_words_max", 135))
    sentences = split_sentences(source["extract"])
    if not sentences:
        return None

    engineering_terms = {
        "engineer", "engineering", "built", "construction", "structure", "stone",
        "water", "aqueduct", "bridge", "road", "harbor", "harbour", "canal",
        "tunnel", "wall", "dam", "drain", "drainage", "arch", "vault", "column",
        "foundation", "masonry", "hydraulic", "irrigation", "reservoir", "system",
        "channel", "technology", "designed", "constructed", "building", "works",
    }

    scored = []
    for idx, sentence in enumerate(sentences):
        low = sentence.lower()
        score = sum(1 for term in engineering_terms if term in low)
        # Strong preference for the opening context while still selecting engineering detail.
        if idx == 0:
            score += 4
        elif idx <= 2:
            score += 2
        scored.append((score, idx, sentence))

    selected = []
    total = 0
    # Take useful sentences by relevance, then restore source order for coherence.
    for _, idx, sentence in sorted(scored, key=lambda x: (-x[0], x[1])):
        wc = len(sentence.split())
        if total + wc > max_words:
            continue
        selected.append((idx, sentence))
        total += wc
        if total >= min_words:
            break

    if total < min_words:
        # Conservative fallback: source-order sentences only.
        selected = []
        total = 0
        for idx, sentence in enumerate(sentences):
            wc = len(sentence.split())
            if total + wc > max_words:
                continue
            selected.append((idx, sentence))
            total += wc
            if total >= min_words:
                break

    if total < min_words:
        return None

    selected.sort(key=lambda x: x[0])
    script = " ".join(sentence for _, sentence in selected)
    log("Script: evidence-only local builder accepted.", len(script.split()), "words")
    return script


def commons_search(query: str, limit=24):
    api = "https://commons.wikimedia.org/w/api.php"
    try:
        data = get_json(
            api,
            params={
                "action": "query",
                "format": "json",
                "list": "search",
                "srnamespace": 6,
                "srlimit": limit,
                "srsearch": query,
            },
        )
        results = data.get("query", {}).get("search", [])
    except Exception as exc:
        log("Commons search failed:", exc)
        return []

    badwords = (
        "map", "diagram", "plan", "drawing", "illustration", "reconstruction",
        "logo", "icon", "seal", "coat of arms", "flag", "locator",
    )
    out = []
    for item in results:
        title = item.get("title", "")
        if not title or any(word in title.lower() for word in badwords):
            continue
        try:
            data = get_json(
                api,
                params={
                    "action": "query",
                    "format": "json",
                    "prop": "imageinfo",
                    "pageids": item["pageid"],
                    "iiprop": "url|mime|extmetadata",
                    "iiurlwidth": 1280,
                },
                tries=3,
            )
            page = next(iter(data["query"]["pages"].values()))
            ii = (page.get("imageinfo") or [{}])[0]
            if ii.get("mime", "") not in ("image/jpeg", "image/png", "image/webp"):
                continue
            meta = ii.get("extmetadata") or {}
            lic = (meta.get("LicenseShortName") or {}).get("value", "").strip()
            url = ii.get("thumburl") or ii.get("url")
            if not lic or not url:
                continue
            artist = html.unescape(
                re.sub("<[^>]+>", "", (meta.get("Artist") or {}).get("value", ""))
            ).strip()
            out.append(
                {
                    "title": title,
                    "url": url,
                    "page_url": ii.get("descriptionurl", ""),
                    "license": html.unescape(re.sub("<[^>]+>", "", lic)),
                    "artist": artist[:120],
                }
            )
        except Exception as exc:
            log("Commons metadata skip:", exc)
            continue
        if len(out) >= 10:
            break
    return out


def download_images(items):
    imgdir = WORK / "images"
    shutil.rmtree(imgdir, ignore_errors=True)
    imgdir.mkdir(parents=True, exist_ok=True)
    good = []
    for idx, item in enumerate(items):
        try:
            response = None
            for attempt in range(1, 4):
                try:
                    response = SESSION.get(item["url"], timeout=40)
                    response.raise_for_status()
                    break
                except Exception as exc:
                    if attempt == 3:
                        raise
                    log("Image retry", attempt, exc)
                    time.sleep(2 * attempt)
            if response is None or len(response.content) > 12_000_000:
                continue
            path = imgdir / f"{idx:02d}.jpg"
            path.write_bytes(response.content)
            with Image.open(path) as im:
                if im.width < 500 or im.height < 400:
                    path.unlink(missing_ok=True)
                    continue
                im.convert("RGB").save(path, "JPEG", quality=90, optimize=True)
            good.append((path, item))
        except Exception as exc:
            log("Image skip:", exc)
    return good


def _atempo_chain(factor: float):
    parts = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.6f}")
    return ",".join(parts)


def normalize_audio_duration(wav: Path, target=52.0):
    duration = ffprobe_duration(wav)
    if 45.0 <= duration <= 60.0:
        return duration
    factor = duration / target
    fixed = wav.with_name("narration_fixed.wav")
    run([
        "ffmpeg", "-y", "-i", wav,
        "-filter:a", _atempo_chain(factor),
        "-ac", "1", "-ar", "22050", fixed,
    ])
    fixed.replace(wav)
    duration = ffprobe_duration(wav)
    if not 44.0 <= duration <= 61.0:
        raise RuntimeError(f"Could not normalize narration duration: {duration:.1f}s")
    log("Narration normalized to", f"{duration:.1f}s")
    return duration


def tts(text: str, wav: Path):
    model = ROOT / "models" / "piper" / "en_US-lessac-medium.onnx"
    if shutil.which("piper") and model.exists():
        try:
            run(["piper", "--model", model, "--output_file", wav], input_text=text)
            if wav.exists() and wav.stat().st_size > 10_000:
                normalize_audio_duration(wav)
                return "piper"
        except Exception as exc:
            log("Piper failed, using espeak-ng fallback:", exc)

    if shutil.which("espeak-ng"):
        # A slower deterministic rate keeps Shorts in the target duration.
        run(["espeak-ng", "-s", "140", "-w", wav, text])
        if wav.exists() and wav.stat().st_size > 10_000:
            normalize_audio_duration(wav)
            return "espeak-ng"

    raise RuntimeError("No working local TTS engine.")


def make_srt(script: str, duration: float, path: Path):
    words = script.split()
    if not words:
        raise RuntimeError("Cannot subtitle an empty script")

    # Short caption chunks are easier to read on vertical video.
    chunks = []
    size = 7
    for i in range(0, len(words), size):
        chunks.append(" ".join(words[i:i + size]))

    total_words = sum(len(chunk.split()) for chunk in chunks)
    t = 0.0
    blocks = []

    def fmt(seconds):
        ms = int(round(seconds * 1000))
        h = ms // 3_600_000
        ms %= 3_600_000
        m = ms // 60_000
        ms %= 60_000
        s = ms // 1000
        ms %= 1000
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    for idx, chunk in enumerate(chunks, 1):
        weight = len(chunk.split())
        end = duration if idx == len(chunks) else t + duration * weight / total_words
        blocks.append(f"{idx}\n{fmt(t)} --> {fmt(end)}\n{chunk}\n")
        t = end

    path.write_text("\n".join(blocks), encoding="utf-8")


def make_video(images, wav: Path, srt: Path, out: Path):
    duration = ffprobe_duration(wav)
    if not 44 <= duration <= 61:
        raise RuntimeError(f"Narration duration out of target range: {duration:.1f}s")
    if len(images) < 3:
        raise RuntimeError("Need at least 3 images")

    segdur = duration / len(images)
    listfile = WORK / "concat.txt"
    segs = []
    width = int(CFG.get("width", 720))
    height = int(CFG.get("height", 1280))
    fps = int(CFG.get("fps", 30))

    for idx, (image, _) in enumerate(images):
        seg = WORK / f"seg_{idx:02d}.mp4"
        frames = max(1, int(round(segdur * fps)))
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,"
            f"zoompan=z='min(zoom+0.00030,1.055)':d={frames}:s={width}x{height}:fps={fps},"
            "format=yuv420p"
        )
        run([
            "ffmpeg", "-y", "-loop", "1", "-i", image,
            "-vf", vf, "-t", f"{segdur:.3f}", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-pix_fmt", "yuv420p", seg,
        ])
        segs.append(seg)

    listfile.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in segs),
        encoding="utf-8",
    )
    silent = WORK / "silent.mp4"

    concat = run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", silent],
        check=False,
    )
    if concat.returncode != 0 or not silent.exists() or silent.stat().st_size < 10_000:
        log("Concat-copy failed; retrying with re-encode.")
        run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-pix_fmt", "yuv420p", silent,
        ])

    # The SRT is already work/captions.srt. Never copy a file onto itself.
    local_srt = WORK / "captions.srt"
    if Path(srt).resolve() != local_srt.resolve():
        shutil.copy2(srt, local_srt)

    subtitle_filter = (
        "subtitles=work/captions.srt:"
        "force_style='FontName=DejaVu Sans,FontSize=18,Outline=2,Shadow=1,Alignment=2,MarginV=85'"
    )
    cmd = [
        "ffmpeg", "-y", "-i", silent, "-i", wav,
        "-vf", subtitle_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", out,
    ]
    burned = run(cmd, check=False, cwd=ROOT)

    if burned.returncode != 0 or not out.exists() or out.stat().st_size < 100_000:
        # Never lose a finished episode just because libass/font support changed on a runner.
        log("WARNING: subtitle burn failed; creating safe final video without burned subtitles.")
        run([
            "ffmpeg", "-y", "-i", silent, "-i", wav,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-shortest", "-movflags", "+faststart", out,
        ])

    final_duration = ffprobe_duration(out)
    if not 43 <= final_duration <= 62:
        raise RuntimeError(f"Final video duration invalid: {final_duration:.1f}s")
    if out.stat().st_size < 250_000:
        raise RuntimeError("Final video file is unexpectedly small")
    log("Final video QC PASS:", f"{final_duration:.1f}s", f"{out.stat().st_size / 1_000_000:.1f} MB")
    return final_duration


def yt_upload(video: Path, title: str, description: str):
    token = ROOT / "config" / "youtube_token.json"
    if not token.exists():
        raise RuntimeError("Missing config/youtube_token.json generated from GitHub Secret.")

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds = Credentials.from_authorized_user_file(str(token), scopes)
    try:
        if not creds.valid:
            if creds.refresh_token:
                creds.refresh(GoogleRequest())
                token.write_text(creds.to_json(), encoding="utf-8")
            else:
                raise RuntimeError("OAuth token has no refresh token")
    except RefreshError as exc:
        raise RuntimeError(
            "YOUTUBE OAUTH EXPIRED/REVOKED. Create a fresh YouTube token and replace GitHub Secret YOUTUBE_TOKEN_JSON."
        ) from exc

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "categoryId": "27",
            "tags": ["history", "ancient history", "engineering", "shorts"],
        },
        "status": {
            "privacyStatus": CFG.get("youtube_privacy", "public"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(
        str(video), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4"
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=5)
        if status:
            log(f"Upload {int(status.progress() * 100)}%")
    if not response or "id" not in response:
        raise RuntimeError("YouTube upload returned no video ID")
    return response["id"]


def description(source, images):
    lines = [
        f"Factual narration source: {source['url']}",
        "",
        "Visuals: Wikimedia Commons files used under their listed licenses:",
    ]
    for _, item in images:
        label = item["title"].replace("File:", "")
        artist = f" | {item['artist']}" if item.get("artist") else ""
        lines.append(f"- {label} | {item['license']}{artist} | {item['page_url']}")
    lines += [
        "",
        "Narration is restricted to information in the cited source extract.",
        "",
        "#Shorts #History #AncientEngineering",
    ]
    return "\n".join(lines)


def preflight():
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            raise RuntimeError(f"Missing required system binary: {binary}")
    if not CAT or not isinstance(CAT, list):
        raise RuntimeError("Topic catalog is empty or invalid")
    token = ROOT / "config" / "youtube_token.json"
    if not token.exists() or token.stat().st_size < 100:
        raise RuntimeError("GitHub YouTube token secret was not written correctly")
    log("PREFLIGHT PASS")


def main():
    preflight()
    state = load_state()
    used = set(state.get("used_topics", []))
    pool = [topic for topic in CAT if topic.get("title") not in used]
    if not pool:
        log("All catalog topics used. Starting a new cycle.")
        state["used_topics"] = []
        used.clear()
        pool = CAT[:]

    # Stable per-run shuffle avoids always trying the same weak first entries.
    random.SystemRandom().shuffle(pool)
    attempts = min(len(pool), max(8, int(CFG.get("max_topic_attempts", 8))))

    for topic in pool[:attempts]:
        title = topic.get("title", "").strip()
        commons_query = topic.get("commons_query", title).strip()
        if not title:
            continue
        log("\n=== TOPIC:", title, "===")
        shutil.rmtree(WORK, ignore_errors=True)
        WORK.mkdir(parents=True, exist_ok=True)

        # All pre-upload topic failures are isolated. The next candidate is tried automatically.
        try:
            source = get_wikipedia(title)
            if not source:
                raise RuntimeError("weak/missing factual source")

            script = build_script(source)
            if not script:
                raise RuntimeError("script evidence/length gate")

            media = commons_search(commons_query)
            minimum = max(3, int(CFG.get("min_commons_images", 5)))
            if len(media) < minimum:
                raise RuntimeError(f"only {len(media)} Commons candidates")

            images = download_images(media)
            if len(images) < minimum:
                raise RuntimeError(f"only {len(images)} usable Commons images")

            # Use at most 8 images to keep encoding predictable on free runners.
            images = images[:8]
            wav = WORK / "narration.wav"
            engine = tts(script, wav)
            duration = ffprobe_duration(wav)
            log("TTS:", engine, f"{duration:.1f}s")

            srt = WORK / "captions.srt"
            make_srt(script, duration, srt)
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", source["title"])[:60]
            video = OUT / f"{safe}.mp4"
            video.unlink(missing_ok=True)
            make_video(images, wav, srt, video)

            yt_title = f"{source['title']}: Ancient Engineering in 60 Seconds #Shorts"
            desc = description(source, images)

        except Exception as exc:
            log("SKIP TOPIC:", title, "|", repr(exc))
            continue

        # Once upload starts, do NOT move to another topic on an uncertain upload failure.
        # This prevents accidental multiple uploads in one click.
        try:
            video_id = yt_upload(video, yt_title, desc)
        except Exception as exc:
            log("UPLOAD FAILED SAFELY:", repr(exc))
            return 4

        log("UPLOAD SUCCESS:", f"https://youtu.be/{video_id}")
        state.setdefault("used_topics", []).append(title)
        state.setdefault("uploads", []).append(
            {
                "topic": title,
                "youtube_id": video_id,
                "title": yt_title,
                "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        try:
            save_state(state)
        except Exception as exc:
            # Upload is already confirmed. Never upload a second video because state persistence failed.
            log("WARNING: upload succeeded but local state save failed:", repr(exc))

        shutil.rmtree(WORK, ignore_errors=True)
        video.unlink(missing_ok=True)
        return 0

    log("NO VIDEO UPLOADED: every attempted topic failed a conservative pre-upload gate.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
