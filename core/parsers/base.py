"""Parser 基类定义"""

from abc import ABC
from asyncio import Task, TimeoutError, sleep
from collections.abc import Callable, Coroutine
from pathlib import Path
from re import Match, Pattern, compile
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout
from typing_extensions import Unpack

from astrbot.api import logger

from ..config import ParserItem, PluginConfig
from ..constants import ANDROID_HEADER, COMMON_HEADER, IOS_HEADER
from ..data import (
    AudioContent,
    Author,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    ParseResultKwargs,
    Platform,
    VideoContent,
)
from ..download import Downloader
from ..exception import ParseException, RedirectException

T = TypeVar("T", bound="BaseParser")
HandlerFunc = Callable[[T, Match[str]], Coroutine[Any, Any, ParseResult]]
KeyPatterns = list[tuple[str, Pattern[str]]]

_KEY_PATTERNS = "_key_patterns"


# 注册处理器装饰器
def handle(keyword: str, pattern: str):
    """注册处理器装饰器"""

    def decorator(func: HandlerFunc[T]) -> HandlerFunc[T]:
        if not hasattr(func, _KEY_PATTERNS):
            setattr(func, _KEY_PATTERNS, [])

        key_patterns: KeyPatterns = getattr(func, _KEY_PATTERNS)
        key_patterns.append((keyword, compile(pattern)))

        return func

    return decorator


class BaseParser:
    """所有平台 Parser 的抽象基类

    子类必须实现：
    - platform: 平台信息（包含名称和显示名称)
    """

    _registry: ClassVar[list[type["BaseParser"]]] = []
    """ 存储所有已注册的 Parser 类 """

    platform: ClassVar[Platform]
    """ 平台信息（包含名称和显示名称） """

    if TYPE_CHECKING:
        _key_patterns: ClassVar[KeyPatterns]
        _handlers: ClassVar[dict[str, HandlerFunc]]

    def __init__(self, config: PluginConfig, downloader: Downloader):
        self.headers = COMMON_HEADER.copy()
        self.ios_headers = IOS_HEADER.copy()
        self.android_headers = ANDROID_HEADER.copy()
        self.cfg = config
        self.data_dir = self.cfg.data_dir
        self.downloader = downloader
        self._session: ClientSession | None = None

    @property
    def proxy(self) -> str | None:
        try:
            parser_cfg: ParserItem = getattr(
                self.cfg.parser, self.__class__.platform.name
            )
            return self.cfg.proxy if parser_cfg.use_proxy else None
        except AttributeError:
            return None

    def __init_subclass__(cls, **kwargs):
        """自动注册子类到 _registry"""
        super().__init_subclass__(**kwargs)
        if ABC not in cls.__bases__:  # 跳过抽象类
            BaseParser._registry.append(cls)

        cls._handlers = {}
        cls._key_patterns = []

        # 获取所有被 handle 装饰的方法
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and hasattr(attr, _KEY_PATTERNS):
                key_patterns: KeyPatterns = getattr(attr, _KEY_PATTERNS)
                handler = cast(HandlerFunc, attr)
                for keyword, pattern in key_patterns:
                    cls._handlers[keyword] = handler
                    cls._key_patterns.append((keyword, pattern))

        # 按关键字长度降序排序
        cls._key_patterns.sort(key=lambda x: -len(x[0]))

    @classmethod
    def get_all_subclass(cls) -> list[type["BaseParser"]]:
        """获取所有已注册的 Parser 类"""
        return cls._registry

    @property
    def session(self) -> ClientSession:
        """获取当前实例的 session，惰性创建"""
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                proxy=self.proxy, timeout=ClientTimeout(total=self.cfg.common_timeout)
            )
        return self._session

    async def close_session(self) -> None:
        """关闭当前实例的 session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def parse(self, keyword: str, searched: Match[str]) -> ParseResult:
        """解析 URL 提取信息

        Args:
            keyword: 关键词
            searched: 正则表达式匹配对象，由平台对应的模式匹配得到

        Returns:
            ParseResult: 解析结果

        Raises:
            ParseException: 解析失败时抛出
        """
        handler = self._handlers[keyword]
        return await self._invoke_handler_with_retry(handler, searched)

    @staticmethod
    def _is_bilibili_auth_error(exc: BaseException) -> bool:
        """检测是否为 B 站凭证过期 / 登录相关错误（code -101、SESSDATA 失效等）"""
        msg = str(exc).lower()
        if not msg:
            return False
        markers = (
            "-101",
            "sessdata",
            "未登录",
            "未登录或",
            "账号未登录",
            "请先登录",
            "login",
            "credential",
            "凭证",
            "登录过期",
        )
        return any(marker in msg for marker in markers)

    @staticmethod
    def _is_5xx_error(exc: BaseException) -> bool:
        """检测是否为 5xx 服务器错误"""
        msg = str(exc).lower()
        if not msg:
            return False
        markers = (
            "500 ",
            " 500",
            "502 ",
            " 502",
            "503 ",
            " 503",
            "504 ",
            " 504",
            "internal server error",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
            "http 5",
        )
        return any(marker in msg for marker in markers)

    async def _invoke_handler_with_retry(
        self,
        handler: HandlerFunc[T],
        searched: Match[str],
    ) -> ParseResult:
        """调用 handler 并应用解析阶段重试配置。

        重试退避为固定档位：
            普通错误: 0.2s, 0.5s, 1.0s
            5xx 错误: 2s, 4s, 6s
        """
        if not self.cfg.parse_retry_enabled:
            return await handler(self, searched)

        max_retries = min(5, max(1, int(getattr(self.cfg, "parse_retry_count", 3))))
        immediate = bool(getattr(self.cfg, "parse_retry_immediate", False))

        last_exc: BaseException | None = None
        for attempt in range(max_retries + 1):
            try:
                return await handler(self, searched)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc

                # B 站凭证过期：跳过重试直接抛出
                if self._is_bilibili_auth_error(exc):
                    logger.warning(
                        "[Parser] B站凭证已过期，请重新执行 blogin 登录"
                    )
                    raise ParseException(
                        "B站凭证已过期，请重新执行 blogin 登录"
                    ) from exc

                if attempt >= max_retries:
                    logger.warning(
                        f"[Parser] API 请求最终失败，已重试 {attempt} 次"
                    )
                    raise

                if immediate:
                    logger.warning(
                        f"[Parser] API 请求失败，立即重试 ({attempt + 1}/{max_retries})（等待已关闭）"
                    )
                    continue

                if self._is_5xx_error(exc):
                    wait = [2.0, 4.0, 6.0][min(attempt, 2)]
                else:
                    wait = [0.2, 0.5, 1.0][min(attempt, 2)]
                logger.warning(
                    f"[Parser] API 请求失败，{wait}s 后重试 ({attempt + 1}/{max_retries})"
                )
                await sleep(wait)

        # 不可达，保持类型安全
        if last_exc:
            raise last_exc
        raise ParseException("解析失败：未知错误")

    async def request_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        **kwargs: Any,
    ) -> ClientResponse:
        """带解析重试配置的 HTTP 请求。

        使用 ``self.session`` 发起请求，遵循 ``parse_retry_enabled`` /
        ``parse_retry_immediate`` / ``parse_retry_count`` 三个配置；遇到
        B 站凭证过期错误时直接抛出，不进入重试循环。
        """
        if not self.cfg.parse_retry_enabled:
            return await self.session.request(
                method, url, headers=headers, proxy=proxy if proxy is not None else self.proxy, **kwargs
            )

        max_retries = min(5, max(1, int(getattr(self.cfg, "parse_retry_count", 3))))
        immediate = bool(getattr(self.cfg, "parse_retry_immediate", False))

        last_exc: BaseException | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = await self.session.request(
                    method,
                    url,
                    headers=headers,
                    proxy=proxy if proxy is not None else self.proxy,
                    **kwargs,
                )
                # 4xx/5xx 也视为可重试错误
                if resp.status >= 500:
                    body_snippet = (await resp.text())[:200]
                    await resp.release()
                    raise ClientError(
                        f"HTTP {resp.status} {resp.reason}: {body_snippet}"
                    )
                return resp
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if self._is_bilibili_auth_error(exc):
                    logger.warning("[Parser] B站凭证已过期，请重新执行 blogin 登录")
                    raise ParseException(
                        "B站凭证已过期，请重新执行 blogin 登录"
                    ) from exc

                if attempt >= max_retries:
                    logger.warning(
                        f"[Parser] API 请求最终失败，已重试 {attempt} 次"
                    )
                    raise

                if immediate:
                    logger.warning(
                        f"[Parser] API 请求失败，立即重试 ({attempt + 1}/{max_retries})（等待已关闭）"
                    )
                    continue

                if self._is_5xx_error(exc):
                    wait = [2.0, 4.0, 6.0][min(attempt, 2)]
                else:
                    wait = [0.2, 0.5, 1.0][min(attempt, 2)]
                logger.warning(
                    f"[Parser] API 请求失败，{wait}s 后重试 ({attempt + 1}/{max_retries})"
                )
                await sleep(wait)

        if last_exc:
            raise last_exc
        raise ParseException("请求失败：未知错误")

    async def parse_with_redirect(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> ParseResult:
        """先重定向再解析"""
        redirect_url = await self.get_redirect_url(url, headers=headers or self.headers)

        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")

        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    @classmethod
    def search_url(cls, url: str) -> tuple[str, Match[str]]:
        """搜索 URL 匹配模式"""
        for keyword, pattern in cls._key_patterns:
            if keyword not in url:
                continue
            if searched := pattern.search(url):
                return keyword, searched
        raise ParseException(f"无法匹配 {url}")

    @classmethod
    def result(cls, **kwargs: Unpack[ParseResultKwargs]) -> ParseResult:
        """构建解析结果"""
        return ParseResult(platform=cls.platform, **kwargs)

    async def get_redirect_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        """获取重定向后的 URL, 单次重定向"""
        headers = headers or COMMON_HEADER.copy()
        retries = self.cfg.download_retry_times
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url, headers=headers, allow_redirects=False, proxy=self.proxy
                ) as resp:
                    if resp.status >= 400:
                        raise ClientError(f"redirect check {resp.status} {resp.reason}")
                    return resp.headers.get("Location", url)
            except (ClientError, TimeoutError):
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                raise RedirectException()
        raise RedirectException()

    async def get_final_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        """获取重定向后的 URL, 允许多次重定向"""
        headers = headers or COMMON_HEADER.copy()
        retries = 2
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url, headers=headers, allow_redirects=True, proxy=self.proxy
                ) as resp:
                    if resp.status >= 400:
                        raise ClientError(
                            f"final url check {resp.status} {resp.reason}"
                        )
                    return str(resp.url)
            except (ClientError, TimeoutError):
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                raise RedirectException()
        raise RedirectException()

    def create_author(
        self,
        name: str,
        avatar_url: str | None = None,
        description: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建作者对象"""

        avatar_task = None
        if avatar_url:
            avatar_task = self.downloader.download_img(
                avatar_url,
                headers=headers or self.headers,
                proxy=self.proxy,
                platform=self.platform.name,
            )
        return Author(name=name, avatar=avatar_task, description=description)

    def create_video_content(
        self,
        url_or_task: str | Task[Path],
        cover_url: str | None = None,
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
    ):
        """创建视频内容"""
        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url,
                headers=headers or self.headers,
                proxy=self.proxy,
                platform=self.platform.name,
            )
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_video(
                url_or_task,
                headers=headers or self.headers,
                proxy=self.proxy,
                platform=self.platform.name,
            )

        return VideoContent(url_or_task, cover_task, duration)

    def create_video_content_by_task(
        self,
        path_task: Task[Path],
        cover_url: str | None = None,
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
    ):
        """创建视频内容，允许调用方自行决定下载任务实现"""
        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url,
                headers=headers or self.headers,
                proxy=self.proxy,
                platform=self.platform.name,
            )
        return VideoContent(path_task, cover_task, duration)

    def create_image_contents(
        self,
        image_urls: list[str],
        headers: dict[str, str] | None = None,
    ):
        """创建图片内容列表"""
        contents: list[ImageContent] = []
        for url in image_urls:
            task = self.downloader.download_img(
                url,
                headers=headers or self.headers,
                proxy=self.proxy,
                platform=self.platform.name,
            )
            contents.append(ImageContent(task))
        return contents

    def create_dynamic_contents(
        self,
        dynamic_urls: list[str],
        headers: dict[str, str] | None = None,
    ):
        """创建动态图片内容列表"""
        contents: list[DynamicContent] = []
        for url in dynamic_urls:
            task = self.downloader.download_video(
                url,
                headers=headers or self.headers,
                proxy=self.proxy,
                platform=self.platform.name,
            )
            contents.append(DynamicContent(task))
        return contents

    def create_audio_content(
        self,
        url_or_task: str | Task[Path],
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
    ):
        """创建音频内容"""
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_audio(
                url_or_task,
                headers=headers or self.headers,
                proxy=self.proxy,
                platform=self.platform.name,
            )

        return AudioContent(url_or_task, duration)

    def create_graphics_content(
        self,
        image_url: str,
        text: str | None = None,
        alt: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建图文内容 图片不能为空 文字可空 渲染时文字在前 图片在后"""
        image_task = self.downloader.download_img(
            image_url,
            headers=headers or self.headers,
            proxy=self.proxy,
            platform=self.platform.name,
        )
        return GraphicsContent(image_task, text, alt)

    def create_file_content(
        self,
        url_or_task: str | Task[Path],
        name: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建文件内容"""
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_file(
                url_or_task,
                headers=headers or self.headers,
                file_name=name,
                proxy=self.proxy,
                platform=self.platform.name,
            )

        return FileContent(url_or_task)
