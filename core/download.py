import asyncio
import time
from asyncio import Task, TimeoutError, create_task, gather, sleep, to_thread
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, AsyncGenerator, ParamSpec, TypeVar

import aiofiles
import yt_dlp
from aiohttp import ClientError, ClientSession, ClientTimeout
from msgspec import Struct, convert
from tqdm.asyncio import tqdm

from astrbot.api import logger

from .config import PluginConfig
from .constants import COMMON_HEADER
from .exception import (
    DownloadException,
    DurationLimitException,
    ParseException,
    SizeLimitException,
    ZeroSizeException,
)
from .utils import LimitedSizeDict, generate_file_name, merge_av, safe_unlink

P = ParamSpec("P")
T = TypeVar("T")


def auto_task(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Task[T]]:
    """装饰器：自动将异步函数调用转换为 Task, 完整保留类型提示"""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Task[T]:
        coro = func(*args, **kwargs)
        name = " | ".join(str(arg) for arg in args if isinstance(arg, str))
        return create_task(coro, name=func.__name__ + " | " + name)

    return wrapper


class VideoInfo(Struct):
    title: str
    """标题"""
    channel: str
    """频道名称"""
    uploader: str
    """上传者 id"""
    duration: int
    """时长"""
    timestamp: int
    """发布时间戳"""
    thumbnail: str
    """封面图片"""
    description: str
    """简介"""
    channel_id: str
    """频道 id"""

    @property
    def author_name(self) -> str:
        return f"{self.channel}@{self.uploader}"


class AdaptiveSemaphoreManager:
    """平台级自适应下载并发管理器

    - 每个平台拥有独立的 asyncio.Semaphore
    - 支持在解析器配置中单独设置并发数
    - 连续下载失败达到阈值时自动降低并发
    - 下载成功后按间隔逐步恢复并发
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._current: dict[str, int] = {}
        self._fail_streak: dict[str, int] = {}
        self._last_change: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _get_default_concurrency(self, platform: str) -> int:
        """获取平台默认并发数（优先使用解析器单独配置）"""
        try:
            parser_cfg = getattr(self.cfg.parser, platform)
            custom = parser_cfg.download_concurrency
            if custom is not None and custom > 0:
                return custom
        except AttributeError:
            pass
        return self.cfg.perf_download_default_concurrency

    def _ensure_sem(self, platform: str) -> asyncio.Semaphore:
        if platform not in self._semaphores:
            init = self._get_default_concurrency(platform)
            self._semaphores[platform] = asyncio.Semaphore(init)
            self._current[platform] = init
        return self._semaphores[platform]

    @asynccontextmanager
    async def acquire(self, platform: str | None) -> AsyncGenerator[None, None]:
        """获取指定平台的下载许可；未启用自适应或平台未知时直接通过"""
        if not platform or not self.cfg.perf_adaptive_download:
            yield
            return
        sem = self._ensure_sem(platform)
        async with sem:
            yield

    async def report_success(self, platform: str | None) -> None:
        if not platform or not self.cfg.perf_adaptive_download:
            return
        async with self._lock:
            self._fail_streak[platform] = 0
            now = time.time()
            interval = self.cfg.perf_download_recover_interval
            last = self._last_change.get(platform, 0)
            if interval > 0 and now - last < interval:
                return
            cur = self._current.get(platform, self._get_default_concurrency(platform))
            target = min(
                cur + self.cfg.perf_download_recover_step,
                self._get_default_concurrency(platform),
            )
            if target != cur:
                self._resize(platform, target)
                self._last_change[platform] = now
                logger.info(
                    f"[AdaptiveDownload] {platform} 下载成功，并发从 {cur} 恢复至 {target}"
                )

    async def report_failure(self, platform: str | None) -> None:
        if not platform or not self.cfg.perf_adaptive_download:
            return
        async with self._lock:
            self._fail_streak[platform] = self._fail_streak.get(platform, 0) + 1
            if self._fail_streak[platform] < self.cfg.perf_download_fail_threshold:
                return
            self._fail_streak[platform] = 0
            cur = self._current.get(platform, self._get_default_concurrency(platform))
            target = max(
                cur - self.cfg.perf_download_degrade_step,
                self.cfg.perf_download_min_concurrency,
            )
            if target != cur:
                self._resize(platform, target)
                self._last_change[platform] = time.time()
                logger.warning(
                    f"[AdaptiveDownload] {platform} 连续失败达到阈值，并发从 {cur} 降至 {target}"
                )

    def _resize(self, platform: str, new_size: int) -> None:
        """调整指定平台的信号量容量

        实现方式：创建新的 Semaphore 替换旧的。已持有旧信号量的任务
        释放后并发自然下降，新任务直接使用更小的信号量。
        """
        self._semaphores[platform] = asyncio.Semaphore(new_size)
        self._current[platform] = new_size


class Downloader:
    """下载器，支持youtube-dlp 和 流式下载"""

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.max_size = self.cfg.source_max_size
        self.default_headers: dict[str, str] = COMMON_HEADER.copy()
        # 视频信息缓存
        self.info_cache: LimitedSizeDict[str, VideoInfo] = LimitedSizeDict()
        # 用于流式下载的客户端
        self.client = ClientSession(
            timeout=ClientTimeout(total=self.cfg.download_timeout)
        )
        # 自适应并发管理器
        self.adaptive = AdaptiveSemaphoreManager(config)

    async def close(self):
        """关闭网络客户端"""
        await self.client.close()

    @staticmethod
    def _extract_host(url: str) -> str:
        """从 URL 中提取 host（含端口）作为 CDN 节点标识"""
        try:
            parsed = urlparse(url)
            if parsed.hostname:
                port = parsed.port
                if port and (
                    (parsed.scheme == "http" and port != 80)
                    or (parsed.scheme == "https" and port != 443)
                ):
                    return f"{parsed.hostname}:{port}"
                return parsed.hostname
        except Exception:
            pass
        return url

    @staticmethod
    def _classify_download_error(exc: Exception) -> str:
        """将下载异常分类为连接被拒/超时/其他"""
        if isinstance(exc, TimeoutError):
            return "timeout"
        msg = str(exc).lower()
        if "connection refused" in msg or "cannot connect to host" in msg:
            return "connection_refused"
        if "timeout" in msg or "timed out" in msg:
            return "timeout"
        return "other"

    @auto_task
    async def streamd(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        platform: str | None = None,
    ) -> Path:
        """流式下载

        Args:
            platform: 平台标识，用于自适应并发控制。留空则不启用平台级限流。
        """
        if not file_name:
            file_name = generate_file_name(url)
        file_path = self.cfg.cache_dir / file_name
        # 如果文件存在，则直接返回
        if file_path.exists():
            return file_path
        headers = headers or self.default_headers
        retries = self.cfg.download_retry_times

        async with self.adaptive.acquire(platform):
            start_time = time.time()
            start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
            host = self._extract_host(url)
            self.cfg.verbose(
                f"[Download] 开始下载 | file={file_name} | node={host} | start={start_ts}"
            )
            for attempt in range(retries + 1):
                try:
                    async with self.client.get(
                        url, headers=headers, allow_redirects=True, proxy=proxy
                    ) as response:
                        host = self._extract_host(str(response.url))
                        if attempt > 0:
                            self.cfg.verbose(
                                f"[Download] 节点切换 | file={file_name} | node={host} | attempt={attempt + 1}/{retries + 1}"
                            )

                        if response.status >= 400:
                            raise ClientError(f"HTTP {response.status} {response.reason}")
                        content_length = response.content_length
                        max_bytes = self.max_size * 1024 * 1024

                        if content_length == 0:
                            logger.warning(f"媒体 url: {url}, 大小为 0, 取消下载")
                            raise ZeroSizeException
                        if content_length and content_length > max_bytes:
                            logger.warning(
                                f"媒体 url: {url} 大小 {content_length / 1024 / 1024:.2f} MB 超过 {self.max_size} MB, 取消下载"
                            )
                            raise SizeLimitException

                        downloaded = 0
                        with self.get_progress_bar(file_name, content_length) as bar:
                            async with aiofiles.open(file_path, "wb") as file:
                                async for chunk in response.content.iter_chunked(
                                    1024 * 1024
                                ):
                                    downloaded += len(chunk)
                                    if downloaded > max_bytes:
                                        raise SizeLimitException
                                    await file.write(chunk)
                                    bar.update(len(chunk))

                        if downloaded == 0:
                            logger.warning(f"媒体 url: {url}, 实际大小为 0, 取消下载")
                            raise ZeroSizeException
                        if content_length and downloaded < content_length:
                            raise ClientError(
                                f"HTTP payload incomplete {downloaded}/{content_length}"
                            )

                        elapsed = time.time() - start_time
                        file_size = file_path.stat().st_size
                        self.cfg.verbose(
                            f"[Download] 下载成功 | file={file_name} | node={host} | "
                            f"elapsed={elapsed:.2f}s | size={file_size / 1024 / 1024:.2f}MB"
                        )

                    await self.adaptive.report_success(platform)
                    return file_path
                except (ZeroSizeException, SizeLimitException):
                    await safe_unlink(file_path)
                    await self.adaptive.report_failure(platform)
                    raise
                except (ClientError, TimeoutError) as exc:
                    await safe_unlink(file_path)
                    error_type = self._classify_download_error(exc)
                    if attempt < retries:
                        wait = 1 + attempt
                        self.cfg.verbose(
                            f"[Download] 下载失败，准备重试 | file={file_name} | node={host} | "
                            f"attempt={attempt + 1}/{retries + 1} | wait={wait}s | "
                            f"error_type={error_type} | reason={exc}"
                        )
                        await sleep(wait)
                        continue
                    await self.adaptive.report_failure(platform)
                    self.cfg.verbose(
                        f"[Download] 下载最终失败 | file={file_name} | node={host} | "
                        f"error_type={error_type} | reason={exc}"
                    )
                    logger.exception(f"下载失败 | url: {url}, file_path: {file_path}")
                    raise DownloadException("媒体下载失败") from exc
        raise DownloadException("媒体下载失败")

    @staticmethod
    def get_progress_bar(desc: str, total: int | None = None) -> tqdm:
        """获取进度条 bar

        Args:
            desc (str): 描述
            total (int | None): 总大小. Defaults to None.

        Returns:
            tqdm: 进度条
        """
        return tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            colour="green",
            desc=desc,
        )

    @auto_task
    async def download_video(
        self,
        url: str,
        *,
        video_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        platform: str | None = None,
    ) -> Path:
        if video_name is None:
            video_name = generate_file_name(url, ".mp4")
        return await self.streamd(
            url, file_name=video_name, headers=headers, proxy=proxy, platform=platform
        )

    @auto_task
    async def download_audio(
        self,
        url: str,
        *,
        audio_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        platform: str | None = None,
    ) -> Path:
        if audio_name is None:
            audio_name = generate_file_name(url, ".mp3")
        return await self.streamd(
            url, file_name=audio_name, headers=headers, proxy=proxy, platform=platform
        )

    @auto_task
    async def download_file(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        platform: str | None = None,
    ) -> Path:
        if file_name is None:
            file_name = generate_file_name(url, ".zip")
        return await self.streamd(
            url, file_name=file_name, headers=headers, proxy=proxy, platform=platform
        )

    @auto_task
    async def download_img(
        self,
        url: str,
        *,
        img_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        platform: str | None = None,
    ) -> Path:
        if img_name is None:
            img_name = generate_file_name(url, ".jpg")
        return await self.streamd(
            url, file_name=img_name, headers=headers, proxy=proxy, platform=platform
        )

    async def download_imgs_without_raise(
        self,
        urls: list[str],
        *,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        platform: str | None = None,
    ) -> list[Path]:
        paths_or_errs = await gather(
            *[
                self.download_img(url, headers=headers, proxy=proxy, platform=platform)
                for url in urls
            ],
            return_exceptions=True,
        )
        return [p for p in paths_or_errs if isinstance(p, Path)]

    @auto_task
    async def download_av_and_merge(
        self,
        v_url: str,
        a_url: str,
        *,
        output_path: Path,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        platform: str | None = None,
    ) -> Path:
        """
        download video and audio file by url with stream and merge
        """
        v_path, a_path = await gather(
            self.download_video(v_url, headers=headers, proxy=proxy, platform=platform),
            self.download_audio(a_url, headers=headers, proxy=proxy, platform=platform),
        )
        await merge_av(v_path=v_path, a_path=a_path, output_path=output_path)
        return output_path

    async def ytdlp_extract_info(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
    ) -> VideoInfo:
        if (info := self.info_cache.get(url)) is not None:
            return info
        opts = {
            "quiet": True,
            "skip_download": True,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if format:
            opts["format"] = format
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            raw = await to_thread(ydl.extract_info, url, download=False)
            if not raw:
                raise ParseException("获取视频信息失败")
        info = convert(raw, VideoInfo)
        self.info_cache[url] = info
        return info

    async def ytdlp_extract_raw(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
    ) -> dict[str, Any]:
        opts = {
            "quiet": True,
            "skip_download": True,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if format:
            opts["format"] = format

        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            raw = await to_thread(ydl.extract_info, url, download=False)
            if not isinstance(raw, dict):
                raise ParseException("yt-dlp 返回数据异常")
            return raw  # type: ignore

    @auto_task
    async def ytdlp_download_video(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
        platform: str | None = None,
    ) -> Path:
        info = await self.ytdlp_extract_info(
            url, cookiefile=cookiefile, headers=headers, proxy=proxy
        )
        if info.duration > self.cfg.max_duration:
            raise DurationLimitException

        video_path = self.cfg.cache_dir / generate_file_name(url, ".mp4")
        if video_path.exists():
            return video_path

        opts = {
            "outtmpl": str(video_path),
            "merge_output_format": "mp4",
            # "format": f"bv[filesize<={info.duration // 10 + 10}M]+ba/b[filesize<={info.duration // 8 + 10}M]",
            # "format": "bv*[height<=720]+ba/b[height<=720]",
            "format": format or "best",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if node:
            opts["js_runtimes"] = {"node": {}}

        async with self.adaptive.acquire(platform):
            start_time = time.time()
            start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
            host = self._extract_host(url)
            self.cfg.verbose(
                f"[Download] 开始 yt-dlp 下载 | file={video_path.name} | node={host} | start={start_ts}"
            )
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
                    await to_thread(ydl.download, [url])
                elapsed = time.time() - start_time
                file_size = video_path.stat().st_size if video_path.exists() else 0
                self.cfg.verbose(
                    f"[Download] yt-dlp 下载成功 | file={video_path.name} | node={host} | "
                    f"elapsed={elapsed:.2f}s | size={file_size / 1024 / 1024:.2f}MB"
                )
                await self.adaptive.report_success(platform)
                return video_path
            except Exception as exc:
                await self.adaptive.report_failure(platform)
                error_type = self._classify_download_error(exc)
                self.cfg.verbose(
                    f"[Download] yt-dlp 下载失败 | file={video_path.name} | node={host} | "
                    f"error_type={error_type} | reason={exc}"
                )
                raise

    @auto_task
    async def ytdlp_download_video_relaxed(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
        platform: str | None = None,
    ) -> Path:
        file_stem = generate_file_name(url)
        video_path = self.cfg.cache_dir / f"{file_stem}.mp4"
        if video_path.exists():
            return video_path

        opts = {
            "outtmpl": str(self.cfg.cache_dir / file_stem) + ".%(ext)s",
            "merge_output_format": "mp4",
            "format": format or None,
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
            "http_headers": headers or self.default_headers,
            "quiet": True,
            "no_warnings": True,
        }
        if not opts["format"]:
            opts.pop("format")
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if node:
            opts["js_runtimes"] = {"node": {}}

        async with self.adaptive.acquire(platform):
            start_time = time.time()
            start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
            host = self._extract_host(url)
            self.cfg.verbose(
                f"[Download] 开始 yt-dlp 宽松下载 | file={video_path.name} | node={host} | start={start_ts}"
            )
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
                    await to_thread(ydl.download, [url])
                if video_path.exists():
                    elapsed = time.time() - start_time
                    file_size = video_path.stat().st_size
                    self.cfg.verbose(
                        f"[Download] yt-dlp 宽松下载成功 | file={video_path.name} | node={host} | "
                        f"elapsed={elapsed:.2f}s | size={file_size / 1024 / 1024:.2f}MB"
                    )
                    await self.adaptive.report_success(platform)
                    return video_path

                candidates = sorted(self.cfg.cache_dir.glob(f"{file_stem}*.mp4"))
                if candidates:
                    elapsed = time.time() - start_time
                    file_size = candidates[0].stat().st_size
                    self.cfg.verbose(
                        f"[Download] yt-dlp 宽松下载成功（候选文件） | file={candidates[0].name} | node={host} | "
                        f"elapsed={elapsed:.2f}s | size={file_size / 1024 / 1024:.2f}MB"
                    )
                    await self.adaptive.report_success(platform)
                    return candidates[0]
                await self.adaptive.report_failure(platform)
                raise DownloadException("yt-dlp 视频下载失败")
            except Exception as exc:
                await self.adaptive.report_failure(platform)
                error_type = self._classify_download_error(exc)
                self.cfg.verbose(
                    f"[Download] yt-dlp 宽松下载失败 | file={video_path.name} | node={host} | "
                    f"error_type={error_type} | reason={exc}"
                )
                raise

    @auto_task
    async def ytdlp_download_audio(
        self,
        url: str,
        *,
        cookiefile: Path | None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        platform: str | None = None,
    ) -> Path:
        file_name = generate_file_name(url)
        audio_path = self.cfg.cache_dir / f"{file_name}.flac"
        if audio_path.exists():
            return audio_path

        opts = {
            "outtmpl": str(self.cfg.cache_dir / file_name) + ".%(ext)s",
            "format": format or "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "flac",
                    "preferredquality": "0",
                }
            ],
            "cookiefile": None,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)

        async with self.adaptive.acquire(platform):
            start_time = time.time()
            start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
            host = self._extract_host(url)
            self.cfg.verbose(
                f"[Download] 开始 yt-dlp 音频下载 | file={audio_path.name} | node={host} | start={start_ts}"
            )
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
                    await to_thread(ydl.download, [url])
                elapsed = time.time() - start_time
                file_size = audio_path.stat().st_size if audio_path.exists() else 0
                self.cfg.verbose(
                    f"[Download] yt-dlp 音频下载成功 | file={audio_path.name} | node={host} | "
                    f"elapsed={elapsed:.2f}s | size={file_size / 1024 / 1024:.2f}MB"
                )
                await self.adaptive.report_success(platform)
                return audio_path
            except Exception as exc:
                await self.adaptive.report_failure(platform)
                error_type = self._classify_download_error(exc)
                self.cfg.verbose(
                    f"[Download] yt-dlp 音频下载失败 | file={audio_path.name} | node={host} | "
                    f"error_type={error_type} | reason={exc}"
                )
                raise
