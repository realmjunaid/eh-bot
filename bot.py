"""EH Bot — E-Hentai -> Zip -> GoFile -> Discord (single file).

Run:  pip install -r requirements.txt
      copy .env.example .env   (fill DISCORD_TOKEN)
      python bot.py

Commands: /ehd <url> | /ping
"""
from __future__ import annotations

import asyncio
import logging
import math
import mimetypes
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ehbot")

# ============================== CONFIG ==============================

def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (ValueError, AttributeError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except (ValueError, AttributeError):
        return default


def _get_id_list(name: str) -> list[int]:
    """Parse comma-separated Discord channel IDs, e.g. '123, 456'."""
    ids: list[int] = []
    for part in os.getenv(name, "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


@dataclass
class Settings:
    discord_token: str = field(default_factory=lambda: os.getenv("DISCORD_TOKEN", ""))
    command_prefix: str = field(default_factory=lambda: os.getenv("COMMAND_PREFIX", "!"))
    allowed_channel_ids: list[int] = field(default_factory=lambda: _get_id_list("ALLOWED_CHANNEL_IDS"))
    gofile_token: str = field(default_factory=lambda: os.getenv("GOFILE_TOKEN", "").strip())
    gofile_folder_id: str = field(default_factory=lambda: os.getenv("GOFILE_FOLDER_ID", "").strip())
    cleanup_after_upload: bool = _get_bool("CLEANUP_AFTER_UPLOAD", True)
    eh_ipb_member_id: str = field(default_factory=lambda: os.getenv("EH_IPB_MEMBER_ID", "").strip())
    eh_ipb_pass_hash: str = field(default_factory=lambda: os.getenv("EH_IPB_PASS_HASH", "").strip())
    eh_igneous: str = field(default_factory=lambda: os.getenv("EH_IGNEOUS", "").strip())
    eh_enable_exhentai: bool = _get_bool("EH_ENABLE_EXHENTAI", False)
    eh_fetch_delay: float = _get_float("EH_FETCH_DELAY", 1.0)
    eh_user_agent: str = field(
        default_factory=lambda: os.getenv(
            "EH_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        )
    )
    download_dir: Path = field(default_factory=lambda: Path(os.getenv("DOWNLOAD_DIR", "./downloads")))
    max_concurrent_galleries: int = _get_int("MAX_CONCURRENT_GALLERIES", 2)
    max_concurrent_images: int = _get_int("MAX_CONCURRENT_IMAGES", 5)
    max_images_per_gallery: int = _get_int("MAX_IMAGES_PER_GALLERY", 0)
    max_zip_mb: int = _get_int("MAX_ZIP_MB", 0)
    require_nsfw_channel: bool = _get_bool("REQUIRE_NSFW_CHANNEL", True)
    progress_edit_interval: float = _get_float("PROGRESS_EDIT_INTERVAL", 5.0)
    http_timeout: int = _get_int("HTTP_TIMEOUT", 30)
    http_max_retries: int = _get_int("HTTP_MAX_RETRIES", 3)

    def validate(self) -> None:
        if not self.discord_token or self.discord_token == "put-your-bot-token-here":
            raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.")
        if self.eh_fetch_delay < 0.2:
            raise RuntimeError("EH_FETCH_DELAY must be >= 0.2 seconds.")
        self.download_dir.mkdir(parents=True, exist_ok=True)

    @property
    def eh_cookies(self) -> dict[str, str]:
        cookies: dict[str, str] = {}
        if self.eh_ipb_member_id:
            cookies["ipb_member_id"] = self.eh_ipb_member_id
        if self.eh_ipb_pass_hash:
            cookies["ipb_pass_hash"] = self.eh_ipb_pass_hash
        if self.eh_igneous:
            cookies["igneous"] = self.eh_igneous
        return cookies


settings = Settings()

# ============================== E-HENTAI ==============================

GALLERY_RE = re.compile(
    r"https?://(?:e-hentai\.org|exhentai\.org)/g/(?P<gid>\d+)/(?P<token>[0-9a-f]+)/?",
    re.IGNORECASE,
)
FILECOUNT_RE = re.compile(r"(\d+)\s+pages?", re.IGNORECASE)


class EHentaiError(Exception):
    pass


class GalleryNotFoundError(EHentaiError):
    pass


class RateLimitedError(EHentaiError):
    pass


class SadPandaError(EHentaiError):
    pass


@dataclass
class GalleryInfo:
    gid: str
    token: str
    title: str
    title_jpn: str = ""
    file_count: int = 0
    gallery_url: str = ""
    image_page_urls: list[str] = field(default_factory=list)

    @property
    def display_title(self) -> str:
        return self.title_jpn or self.title or f"gallery-{self.gid}"


def normalize_gallery_url(url: str, allow_exhentai: bool = False) -> tuple[str, str, str]:
    url = url.strip().split("?")[0].split("#")[0]
    m = GALLERY_RE.search(url)
    if not m:
        raise EHentaiError(
            "Invalid gallery URL. Expected format: `https://e-hentai.org/g/<gid>/<token>/`"
        )
    host = urlparse(url).netloc.lower()
    if "exhentai" in host and not allow_exhentai:
        raise EHentaiError("ExHentai URLs are disabled. Set `EH_ENABLE_EXHENTAI=true` with login cookies.")
    gid, token = m.group("gid"), m.group("token").lower()
    base = "https://exhentai.org" if "exhentai" in host else "https://e-hentai.org"
    return gid, token, f"{base}/g/{gid}/{token}/"


def parse_gallery_page(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    def _text(selector: str) -> str:
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else ""

    title = _text("h1#gn")
    title_jpn = _text("h1#gj")
    file_count = 0
    gdd = soup.select_one("div#gdd table")
    if gdd:
        m = FILECOUNT_RE.search(gdd.get_text(" ", strip=True))
        if m:
            file_count = int(m.group(1))
    thumbs: list[str] = []
    for a in soup.select("div#gdt a[href]"):
        href = a["href"].split("?")[0]
        if "/s/" in href and href not in thumbs:
            thumbs.append(href)
    total_pages = 1
    for a in soup.select("table.ptt a[href]"):
        href = a.get("href", "")
        pm = re.search(r"[?&]p=(\d+)", href)
        if pm:
            total_pages = max(total_pages, int(pm.group(1)) + 1)
        elif a.get_text(strip=True).isdigit():
            total_pages = max(total_pages, int(a.get_text(strip=True)))
    return {"title": title, "title_jpn": title_jpn, "file_count": file_count,
            "thumbs": thumbs, "total_pages": total_pages}


def parse_image_page(html: str, page_url: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True).lower()
    if "sad panda" in text and "exhentai" in page_url:
        raise SadPandaError("ExHentai login required (sad panda). Check EH_* cookies.")
    if "image limit" in text or "509" in text or "bandwidth" in text:
        raise RateLimitedError("E-Hentai image limit exceeded (509). Try again later or add EH_* login cookies.")
    img = soup.select_one("img#img")
    if not img or not img.get("src"):
        i3 = soup.select_one("div#i3 img[src]")
        if i3:
            img = i3
        else:
            raise EHentaiError(f"Could not find image on page: {page_url}")
    image_url = img["src"]
    nxt = soup.select_one("a#next[href]")
    next_url = nxt["href"] if nxt else ""
    if next_url and "/s/" not in next_url:
        next_url = ""
    return image_url, next_url


class EHentaiClient:
    def __init__(self, user_agent: str, cookies: dict[str, str] | None = None,
                 timeout: int = 30, max_retries: int = 3, fetch_delay: float = 1.0) -> None:
        self.user_agent = user_agent
        self.cookies = cookies or {}
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.fetch_delay = max(0.0, fetch_delay)

    def _headers(self, referer: str = "https://e-hentai.org/") -> dict[str, str]:
        return {"User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9", "Referer": referer}

    async def _fetch_text(self, session: aiohttp.ClientSession, url: str) -> str:
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                async with session.get(url, headers=self._headers()) as resp:
                    if resp.status == 404:
                        raise GalleryNotFoundError(f"Gallery/page not found (404): {url}")
                    if resp.status == 509:
                        raise RateLimitedError("E-Hentai returned 509 (bandwidth/quota exceeded).")
                    if resp.status == 403:
                        body = (await resp.text())[:500].lower()
                        if "sad panda" in body or "exhentai" in url:
                            raise SadPandaError("ExHentai login required.")
                        raise EHentaiError(f"Access denied ({resp.status}) for {url}")
                    resp.raise_for_status()
                    return await resp.text()
            except (GalleryNotFoundError, RateLimitedError, SadPandaError):
                raise
            except Exception as exc:
                last_err = exc
                log.warning("Fetch failed (attempt %d/%d) %s: %s", attempt, self.max_retries, url, exc)
                await asyncio.sleep(min(2 ** attempt, 8))
        raise EHentaiError(f"Failed to fetch {url} after {self.max_retries} retries: {last_err}")

    async def collect_gallery(self, gallery_url: str, allow_exhentai: bool = False) -> GalleryInfo:
        gid, token, canonical = normalize_gallery_url(gallery_url, allow_exhentai)
        connector = aiohttp.TCPConnector(limit=10)
        async with aiohttp.ClientSession(cookies=self.cookies, timeout=self.timeout, connector=connector) as session:
            first_html = await self._fetch_text(session, canonical)
            parsed = parse_gallery_page(first_html)
            if not parsed["title"] and not parsed["thumbs"]:
                lowered = first_html.lower()
                if "sad panda" in lowered:
                    raise SadPandaError("Gallery requires ExHentai login.")
                if "gallery not found" in lowered or "no hits" in lowered:
                    raise GalleryNotFoundError(f"Gallery not found: {canonical}")
                raise EHentaiError("Could not parse gallery page (layout changed or blocked).")
            all_thumbs: list[str] = list(parsed["thumbs"])
            for p in range(1, parsed["total_pages"]):
                html = await self._fetch_text(session, f"{canonical}?p={p}")
                for t in parse_gallery_page(html)["thumbs"]:
                    if t not in all_thumbs:
                        all_thumbs.append(t)
                await asyncio.sleep(0.4)

            def _page_num(u: str) -> int:
                m = re.search(r"-(\d+)(?:/?)$", u)
                return int(m.group(1)) if m else 0

            all_thumbs.sort(key=_page_num)
            expected = parsed["file_count"] or len(all_thumbs)
            log.info("Gallery %s: %d thumbs collected (advertised %d)", gid, len(all_thumbs), expected)
            return GalleryInfo(gid=gid, token=token, title=parsed["title"] or f"gallery-{gid}",
                               title_jpn=parsed["title_jpn"], file_count=expected,
                               gallery_url=canonical, image_page_urls=all_thumbs)

    async def resolve_image_urls(self, session: aiohttp.ClientSession,
                                 image_page_urls: list[str], progress_cb=None) -> list[tuple[str, str]]:
        resolved: list[tuple[str, str]] = []
        for i, page_url in enumerate(image_page_urls, 1):
            html = await self._fetch_text(session, page_url)
            img_url, _ = parse_image_page(html, page_url)
            resolved.append((page_url, img_url))
            if progress_cb:
                try:
                    progress_cb(i, len(image_page_urls))
                except Exception:
                    pass
            if self.fetch_delay and i < len(image_page_urls):
                await asyncio.sleep(self.fetch_delay)
        return resolved

# ============================== GOFILE ==============================

GOFILE_API = "https://api.gofile.io"


class GoFileError(Exception):
    pass


class GoFileClient:
    def __init__(self, token: str = "", folder_id: str = "", timeout: int = 60) -> None:
        self.token = token.strip()
        self.folder_id = folder_id.strip()
        self.timeout = aiohttp.ClientTimeout(total=None, sock_connect=timeout, sock_read=timeout)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def get_server(self, session: aiohttp.ClientSession) -> str:
        async with session.get(f"{GOFILE_API}/servers", headers=self._auth_headers()) as resp:
            try:
                data = await resp.json()
                servers = data["data"]["servers"]
                if isinstance(servers, dict):
                    servers = list(servers.values())
                name = servers[0]["name"] if isinstance(servers[0], dict) else servers[0]
                return str(name)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise GoFileError(f"getServers returned unexpected data: {data!r}") from exc

    async def upload_file(self, path: str | Path, progress_cb=None) -> dict:
        path = Path(path)
        if not path.is_file():
            raise GoFileError(f"File not found: {path}")
        size = path.stat().st_size
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            server = await self.get_server(session)
            url = f"https://{server}.gofile.io/uploadFile"
            mime, _ = mimetypes.guess_type(path.name)
            form = aiohttp.FormData()
            if self.token:
                form.add_field("token", self.token)
            if self.folder_id:
                form.add_field("folderId", self.folder_id)
            if progress_cb:
                form.add_field("file", _ProgressReader(path, progress_cb, size),
                               filename=path.name, content_type=mime or "application/octet-stream")
            else:
                form.add_field("file", open(path, "rb"),
                               filename=path.name, content_type=mime or "application/octet-stream")
            log.info("Uploading %s (%.2f MB) to %s", path.name, size / 1e6, server)
            async with session.post(url, data=form, headers=self._auth_headers()) as resp:
                try:
                    data = await resp.json()
                except Exception as exc:
                    raise GoFileError(f"GoFile upload HTTP {resp.status}: {(await resp.text())[:500]}") from exc
                if data.get("status") != "ok":
                    raise GoFileError(f"GoFile upload failed: {data}")
                return data["data"]


class _ProgressReader:
    def __init__(self, path: Path, cb, total: int, chunk: int = 1024 * 256) -> None:
        self._f = open(path, "rb")
        self._cb = cb
        self._total = total
        self._sent = 0
        self._chunk = chunk

    async def read(self, n: int = -1):
        data = self._f.read(self._chunk if n == -1 else n)
        if data:
            self._sent += len(data)
            try:
                r = self._cb(self._sent, self._total)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass
        else:
            try:
                self._f.close()
            except Exception:
                pass
        return data

    def __len__(self):
        return self._total

# ============================== PIPELINE ==============================

@dataclass
class JobProgress:
    stage: str = "queued"
    total_images: int = 0
    resolved: int = 0
    downloaded: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    upload_sent: int = 0
    upload_total: int = 0
    detail: str = ""

    @property
    def pct_download(self) -> float:
        if not self.total_images:
            return 0.0
        return 100.0 * (self.downloaded + self.failed) / self.total_images


@dataclass
class JobResult:
    gallery: GalleryInfo
    zip_path: Path
    zip_bytes: int
    gofile_url: str
    elapsed_s: float


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:max_len].rstrip(" .") or "gallery")


async def _download_one(session: aiohttp.ClientSession, url: str, dest: Path, retries: int = 3) -> int:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            headers = {"User-Agent": settings.eh_user_agent, "Referer": "https://e-hentai.org/"}
            async with session.get(url, headers=headers) as resp:
                if resp.status == 404:
                    raise EHentaiError(f"Image gone (404): {url}")
                resp.raise_for_status()
                data = await resp.read()
                if len(data) < 1024:
                    raise EHentaiError(f"Suspiciously small file ({len(data)}B): {url}")
                dest.write_bytes(data)
                return len(data)
        except Exception as exc:
            last = exc
            log.warning("Image download failed (%d/%d) %s: %s", attempt, retries, url, exc)
            await asyncio.sleep(min(2 ** attempt, 8))
    raise EHentaiError(f"Image download failed after {retries} retries: {url} ({last})")


async def process_gallery(gallery_url: str, job_id: str = "job",
                          progress: JobProgress | None = None) -> JobResult:
    t0 = time.monotonic()
    progress = progress or JobProgress()
    eh = EHentaiClient(user_agent=settings.eh_user_agent, cookies=settings.eh_cookies,
                       timeout=settings.http_timeout, max_retries=settings.http_max_retries,
                       fetch_delay=settings.eh_fetch_delay)
    progress.stage = "metadata"
    progress.detail = "Reading gallery pages…"
    gallery = await eh.collect_gallery(gallery_url, allow_exhentai=settings.eh_enable_exhentai)
    s_pages = gallery.image_page_urls
    if settings.max_images_per_gallery and len(s_pages) > settings.max_images_per_gallery:
        s_pages = s_pages[:settings.max_images_per_gallery]
    progress.total_images = len(s_pages)
    if not s_pages:
        raise EHentaiError("No images found in this gallery.")

    workdir = settings.download_dir / f"{gallery.gid}_{job_id}"
    imgdir = workdir / "img"
    imgdir.mkdir(parents=True, exist_ok=True)

    connector = aiohttp.TCPConnector(limit=settings.max_concurrent_images + 2)
    timeout = aiohttp.ClientTimeout(total=settings.http_timeout)
    async with aiohttp.ClientSession(cookies=settings.eh_cookies, timeout=timeout, connector=connector) as session:
        progress.stage = "resolving"

        def _on_resolve(done: int, total: int) -> None:
            progress.resolved = done
            progress.detail = f"Resolving image {done}/{total}…"

        resolved = await eh.resolve_image_urls(session, s_pages, progress_cb=_on_resolve)
        progress.stage = "downloading"
        sem = asyncio.Semaphore(settings.max_concurrent_images)

        async def _one(idx: int, img_url: str) -> None:
            ext = ".jpg"
            for cand in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                if cand in img_url.lower().split("?")[0]:
                    ext = ".jpg" if cand == ".jpeg" else cand
                    break
            dest = imgdir / f"{idx:04d}{ext}"
            async with sem:
                try:
                    n = await _download_one(session, img_url, dest, settings.http_max_retries)
                    progress.downloaded += 1
                    progress.bytes_downloaded += n
                except Exception as exc:
                    progress.failed += 1
                    log.error("Skipping image %d: %s", idx, exc)
                progress.detail = f"Downloaded {progress.downloaded}/{progress.total_images} ({progress.failed} failed)"

        await asyncio.gather(*[_one(i, u) for i, (_, u) in enumerate(resolved, 1)])

    if progress.downloaded == 0:
        raise EHentaiError("All image downloads failed (IP quota / 509 likely). Try again later.")

    progress.stage = "zipping"
    progress.detail = "Creating zip…"
    safe = sanitize_filename(gallery.display_title)
    zip_path = workdir / f"{safe} [{gallery.gid}].zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(imgdir.iterdir()):
            if f.is_file():
                zf.write(f, arcname=f.name)
    zip_bytes = zip_path.stat().st_size
    if settings.max_zip_mb and zip_bytes > settings.max_zip_mb * 1024 * 1024:
        raise EHentaiError(f"Zip is {zip_bytes / 1e6:.1f} MB, over the {settings.max_zip_mb} MB cap.")

    progress.stage = "uploading"
    progress.detail = "Uploading to GoFile…"
    gf = GoFileClient(token=settings.gofile_token, folder_id=settings.gofile_folder_id,
                      timeout=settings.http_timeout)

    def _on_upload(sent: int, total: int) -> None:
        progress.upload_sent = sent
        progress.upload_total = total
        progress.detail = f"Uploading {sent / 1e6:.1f}/{total / 1e6:.1f} MB…"

    payload = await gf.upload_file(zip_path, progress_cb=_on_upload)
    gofile_url = payload.get("downloadPage", "")
    if not gofile_url:
        raise EHentaiError(f"GoFile returned no downloadPage: {payload}")

    progress.stage = "done"
    progress.detail = "Done."
    elapsed = time.monotonic() - t0
    return JobResult(gallery=gallery, zip_path=zip_path, zip_bytes=zip_bytes,
                     gofile_url=gofile_url, elapsed_s=elapsed)


def cleanup_job(gallery_gid: str, job_id: str, keep_zip: bool = False) -> None:
    workdir = settings.download_dir / f"{gallery_gid}_{job_id}"
    if not workdir.exists():
        return
    if keep_zip:
        shutil.rmtree(workdir / "img", ignore_errors=True)
    else:
        shutil.rmtree(workdir, ignore_errors=True)

# ============================== DISCORD ==============================

def _bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled) + f" {pct:.0f}%"


class EHCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._gallery_sem = asyncio.Semaphore(max(1, settings.max_concurrent_galleries))
        self._active: dict[str, str] = {}

    def _check_channel(self, interaction: discord.Interaction) -> str | None:
        if settings.allowed_channel_ids:
            if interaction.channel_id not in settings.allowed_channel_ids:
                return "This bot only works in the designated channel."
        ch = interaction.channel
        if settings.require_nsfw_channel and isinstance(ch, (discord.TextChannel, discord.Thread)):
            if not getattr(ch, "nsfw", False):
                return "This command is only allowed in **NSFW-marked** channels."
        return None

    @staticmethod
    def _progress_embed(progress: JobProgress, gallery_url: str, started: float, job_id: str) -> discord.Embed:
        elapsed = time.monotonic() - started
        if progress.stage in ("downloading", "resolving"):
            pct = (100.0 * progress.resolved / progress.total_images
                   if progress.stage == "resolving" and progress.total_images
                   else progress.pct_download)
        elif progress.stage == "uploading" and progress.upload_total:
            pct = 100.0 * progress.upload_sent / progress.upload_total
        elif progress.stage == "done":
            pct = 100.0
        else:
            pct = 0.0
        em = discord.Embed(title="📥 E-Hentai download", color=discord.Color.blurple())
        em.add_field(name="Gallery", value=f"<{gallery_url}>", inline=False)
        em.add_field(name=f"Stage: `{progress.stage}`",
                     value=f"`{_bar(pct)}`\n{progress.detail}\n"
                           f"Images: {progress.downloaded}/{progress.total_images}"
                           + (f" ({progress.failed} failed)" if progress.failed else "")
                           + f" • {elapsed:.0f}s", inline=False)
        em.set_footer(text=f"Job {job_id}")
        return em

    @app_commands.command(name="ehd", description="Download an E-Hentai gallery and get a GoFile link.")
    @app_commands.describe(url="Gallery URL, e.g. https://e-hentai.org/g/12345/abcdef1234/")
    async def ehd(self, interaction: discord.Interaction, url: str) -> None:
        err = self._check_channel(interaction)
        if err:
            emoji = "🔞" if "NSFW" in err else "⛔"
            await interaction.response.send_message(f"{emoji} {err}", ephemeral=True)
            return
        try:
            _, _, canonical = normalize_gallery_url(url, settings.eh_enable_exhentai)
        except EHentaiError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        if len(self._active) >= max(1, settings.max_concurrent_galleries * 2):
            await interaction.response.send_message("⏳ Queue is full, try again in a minute.", ephemeral=True)
            return
        job_id = uuid.uuid4().hex[:8]
        self._active[job_id] = canonical
        await interaction.response.defer(thinking=True)
        msg = await interaction.original_response()
        progress = JobProgress()
        started = time.monotonic()
        await msg.edit(embed=self._progress_embed(progress, canonical, started, job_id))

        async def _updater() -> None:
            last_edit = 0.0
            while progress.stage not in ("done", "error"):
                await asyncio.sleep(1.0)
                if time.monotonic() - last_edit >= settings.progress_edit_interval:
                    last_edit = time.monotonic()
                    try:
                        await msg.edit(embed=self._progress_embed(progress, canonical, started, job_id))
                    except Exception:
                        pass

        updater = asyncio.create_task(_updater())
        try:
            async with self._gallery_sem:
                result = await process_gallery(canonical, job_id=job_id, progress=progress)
            progress.stage = "done"
            updater.cancel()
            try:
                await msg.edit(embed=self._progress_embed(progress, canonical, started, job_id))
            except Exception:
                pass
            g = result.gallery
            em = discord.Embed(title="✅ Gallery ready", description=f"**{(g.title_jpn or g.title)}**",
                               color=discord.Color.green(), url=result.gofile_url)
            em.add_field(name="Source", value=f"<{g.gallery_url}>", inline=False)
            em.add_field(name="Pages", value=f"{progress.downloaded}/{progress.total_images}", inline=True)
            em.add_field(name="Zip size", value=f"{result.zip_bytes / 1e6:.1f} MB", inline=True)
            em.add_field(name="Time", value=f"{result.elapsed_s:.0f}s", inline=True)
            em.add_field(name="GoFile link", value=result.gofile_url, inline=False)
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="⬇ Download from GoFile", url=result.gofile_url))
            await interaction.followup.send(embed=em, view=view)
        except EHentaiError as exc:
            progress.stage = "error"
            updater.cancel()
            await interaction.followup.send(f"❌ Download failed: {exc}")
        except Exception as exc:
            progress.stage = "error"
            updater.cancel()
            log.exception("Job %s crashed", job_id)
            await interaction.followup.send(f"❌ Unexpected error: `{exc}`")
        finally:
            updater.cancel()
            try:
                gid = canonical.rstrip("/").split("/")[-2]
                cleanup_job(gid, job_id, keep_zip=not settings.cleanup_after_upload)
            except Exception:
                pass
            self._active.pop(job_id, None)

    @app_commands.command(name="ping", description="Check if the bot is alive.")
    async def ping(self, interaction: discord.Interaction) -> None:
        err = self._check_channel(interaction)
        if err:
            await interaction.response.send_message(f"⛔ {err}", ephemeral=True)
            return
        ms = round(raw * 1000) if (raw := self.bot.latency) and not math.isnan(raw) else 0
        await interaction.response.send_message(f"🏓 Pong! `{ms}ms`", ephemeral=True)


class EHBot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.add_cog(EHCommands(self))
        synced = await self.tree.sync()
        log.info("Synced %d slash command(s).", len(synced))


def main() -> None:
    settings.validate()
    bot = EHBot(command_prefix=settings.command_prefix, intents=discord.Intents.default())

    @bot.event
    async def on_ready() -> None:
        log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")

    try:
        bot.run(settings.discord_token)
    except discord.LoginFailure:
        log.error("Discord login failed: invalid DISCORD_TOKEN.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
