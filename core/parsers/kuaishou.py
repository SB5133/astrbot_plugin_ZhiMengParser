import re
import time
from random import choice
from typing import ClassVar, TypeAlias
from urllib.parse import quote

import aiohttp
import msgspec
from msgspec import Struct, field

from astrbot.api import logger

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Platform
from ..download import Downloader
from .base import BaseParser, ParseException, handle

# 快手实况图(动态图)兜底解析接口模板, {url} 为分享链接占位符。
# 实况视频仅存在于快手 App 原生接口(带签名), Web 端 INIT_STATE 完全不含
# 视频字段, 因此借助第三方聚合解析接口换取带 pkey 签名的临时 mp4 直链。
# 免费公共接口可用性无保证, 支持在 live_photo_api 配置里每行一个模板,
# 按顺序尝试; 连续失败达到阈值后熔断冷却, 避免每次解析都白等超时。
DEFAULT_LIVE_PHOTO_API = "https://api.bugpk.com/api/ksjx?url={url}"
_LIVE_API_FAIL_THRESHOLD = 3
_LIVE_API_COOLDOWN = 300.0


class KuaiShouParser(BaseParser):
    """快手解析器"""

    # 平台信息
    platform: ClassVar[Platform] = Platform(name="kuaishou", display_name="快手")

    # 实况接口熔断状态 (类级默认值兜底 __new__ 构造)
    _live_api_fail_count: int = 0
    _live_api_cooldown_until: float = 0.0

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.kuaishou
        self.ios_headers.update({"Referer": "https://v.kuaishou.com/"})
        self.cookiejar = CookieJar(config, self.mycfg, domain="kuaishou.com")
        if self.cookiejar.cookies_str:
            self.ios_headers["cookie"] = self.cookiejar.cookies_str
        self._live_api_fail_count = 0
        self._live_api_cooldown_until = 0.0

    # https://v.kuaishou.com/2yAnzeZ
    @handle("v.kuaishou", r"v\.kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")
    # https://www.kuaishou.com/short-video/3xhjgcmir24m4nm
    @handle("kuaishou", r"(?:www\.)?kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")
    # https://v.m.chenzhongtech.com/fw/photo/3xburnkmj3auazc
    @handle("chenzhongtech", r"(?:v\.m\.)?chenzhongtech\.com/fw/[A-Za-z\d._?%&+\-=/#]+")
    async def _parse_v_kuaishou(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL
        url = f"https://{searched.group(0)}"
        real_url = await self.get_redirect_url(url, headers=self.ios_headers)

        if len(real_url) <= 0:
            raise ParseException("failed to get location url from url")

        # /fw/long-video/ 返回结果不一样, 统一替换为 /fw/photo/ 请求
        real_url = real_url.replace("/fw/long-video/", "/fw/photo/")

        async with self.session.get(real_url, headers=self.ios_headers) as resp:
            if resp.status >= 400:
                raise ParseException(f"获取页面失败 {resp.status}")
            response_text = await resp.text()

        pattern = r"window\.INIT_STATE\s*=\s*(.*?)</script>"
        matched = re.search(pattern, response_text)

        if not matched:
            raise ParseException("failed to parse video JSON info from HTML")

        json_str = matched.group(1).strip()
        init_state = msgspec.json.decode(json_str, type=KuaishouInitState)
        photo = next(
            (d.photo for d in init_state.values() if d.photo is not None), None
        )
        if photo is None:
            raise ParseException("window.init_state don't contains videos or pics")

        # 简洁的构建方式
        contents = []

        # 实况图检测:
        # - 单图作品: ext_params.single.type == 3, H5 数据里的确切标记
        # - 图集作品: H5 数据与普通图集无法区分 (atlas.type 恒为 1), 而实况
        #   视频只存在于 App 原生接口, 只能探测聚合接口, 返回 type=="live"
        #   才切实况路径, 探测失败/非 live 静默走原图集路径
        live_urls: list[str] = []
        probe_live = photo.is_live_photo or (
            photo.is_picture and bool(photo.img_urls) and not photo.video_url
        )
        if probe_live and self.mycfg.live_photo_enabled is not False:
            # 单图实况已由 H5 标记确认, 接口没兑现也计入接口失败;
            # 图集探测未确认, 返回非 live 属正常否定, 不计入
            live_urls = await self._fetch_live_photo_urls(
                url, confirmed=photo.is_live_photo
            )
            if live_urls:
                logger.info(
                    f"[快手] 检测到实况图, 获取到 {len(live_urls)} 个实况视频"
                )

        # 添加视频内容
        if video_url := photo.video_url:
            contents.append(
                self.create_video_content(
                    video_url, photo.cover_url, photo.duration, headers=self.ios_headers
                )
            )

        # 添加图片内容
        img_urls = photo.img_urls
        # 快手新版图文作品不再走 ext_params.atlas，而是把图放在 coverUrls，
        # 老逻辑在这里会拿到空列表 → 最终只发一条文本、图全丢
        if not img_urls and (photo.is_picture or not photo.video_url):
            img_urls = photo.cover_url_list
        # 实况视频已拿到时, 静态图按配置决定是否同时发送
        if img_urls and not (live_urls and self.mycfg.live_photo_send_image is False):
            contents.extend(
                self.create_image_contents(img_urls, headers=self.ios_headers)
            )

        # 添加实况视频内容 (DynamicContent, 发送阶段按视频消息发出)
        if live_urls:
            contents.extend(
                self.create_dynamic_contents(live_urls, headers=self.ios_headers)
            )

        # 构建作者
        author = self.create_author(
            photo.name, photo.head_url, headers=self.ios_headers
        )

        return self.result(
            title=photo.caption,
            author=author,
            contents=contents,
            timestamp=photo.timestamp // 1000,
            extra={
                "like": photo.like_count,
                "comment": photo.comment_count,
                "share": photo.forward_count,
                "view": photo.view_count,
            },
        )

    async def _fetch_live_photo_urls(
        self, page_url: str, confirmed: bool = False
    ) -> list[str]:
        """通过聚合解析接口换取快手实况图的视频直链。

        流程: 按 live_photo_api 配置逐行取接口模板(空则用默认), 依序尝试,
        第一个返回实况直链的接口生效。HTTP 失败/结构变化视为接口失败,
        累计连续失败达到阈值后熔断, 冷却期内不再发请求直接降级,
        避免接口挂掉时每条快手图集解析都白等一次超时。

        Args:
            page_url: 分享链接
            confirmed: True 表示 H5 标记已确认是实况(单图实况), 此时接口
                正常响应却没有实况直链也计入接口失败; False 表示仅图集
                探测, 返回非 live 属正常否定, 不计失败。

        任何失败均静默降级为空列表, 上层继续按普通图片作品发送。
        """
        if time.time() < self._live_api_cooldown_until:
            # 熔断冷却期, 静默跳过
            return []

        # 原生代发服务优先 (FridaRPC, App 进程内代发, 热更自动跟随);
        # 本地服务挂了不值得等, 3s 短超时, 失败静默回落三方接口链
        native_url = (self.mycfg.live_photo_native_url or "").strip()
        if native_url:
            native_videos = await self._fetch_live_photo_native(native_url, page_url)
            if native_videos:
                self._live_api_fail_count = 0
                return native_videos

        templates = [
            t.strip()
            for t in (self.mycfg.live_photo_api or "").splitlines()
            if t.strip()
        ] or [DEFAULT_LIVE_PHOTO_API]

        api_fail = 0
        confirmed_miss = False
        for template in templates:
            urls = await self._try_live_api(template, page_url)
            if urls:
                self._live_api_fail_count = 0
                return urls
            if urls is None:
                api_fail += 1  # 接口不可用/结构异常
            elif confirmed:
                confirmed_miss = True  # 接口正常但未返回实况

        if api_fail or confirmed_miss:
            self._live_api_fail_count += 1
            if self._live_api_fail_count >= _LIVE_API_FAIL_THRESHOLD:
                self._live_api_cooldown_until = time.time() + _LIVE_API_COOLDOWN
                self._live_api_fail_count = 0
                logger.warning(
                    f"[快手] 实况接口连续失败, 熔断 {_LIVE_API_COOLDOWN:.0f}s 内跳过探测, "
                    "期间实况图降级为静态图"
                )
        else:
            self._live_api_fail_count = 0
        return []

    async def _fetch_live_photo_native(
        self, native_url: str, page_url: str
    ) -> list[str]:
        """从 FridaRPC 原生代发服务获取实况视频直链。

        GET {native_url}/live_photo?url=<分享链接> -> {"ok": true, "videos": [...]}
        任何失败(服务不可达/超时/响应变化)静默返回空列表, 回落三方接口链。
        """
        api = f"{native_url.rstrip('/')}/live_photo?url={quote(page_url, safe='')}"
        try:
            async with self.session.get(
                api,
                headers=self.ios_headers,
                proxy=self.proxy,
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                if resp.status >= 400:
                    return []
                data = await resp.json(content_type=None)
            if data and data.get("ok"):
                return list(data.get("videos") or [])
        except Exception:  # noqa: BLE001
            pass
        return []

    async def _try_live_api(self, template: str, page_url: str) -> list[str] | None:
        """尝试单个接口模板。

        Returns:
            实况直链列表; None 表示接口不可用或返回结构异常;
            空列表表示接口正常响应但不是实况类型。
        """
        api_url = template.format(url=quote(page_url, safe=""))
        try:
            async with self.session.get(
                api_url, headers=self.ios_headers, proxy=self.proxy
            ) as resp:
                if resp.status >= 400:
                    logger.warning(
                        f"[快手] 实况图接口请求失败 HTTP {resp.status}: {template}, "
                        "尝试下一接口/降级"
                    )
                    return None
                data = await resp.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[快手] 实况图接口请求异常: {e}, 尝试下一接口/降级")
            return None

        try:
            if str(data.get("code")) != "200":
                logger.warning(
                    f"[快手] 实况图接口返回异常 code={data.get('code')}: {template}"
                )
                return None
            payload = data.get("data") or {}
            if str(payload.get("type") or "").lower() != "live":
                return []
            videos: list[str] = []
            for item in payload.get("live_photo") or []:
                video = (item or {}).get("video") if isinstance(item, dict) else None
                if video:
                    videos.append(video)
            return videos
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[快手] 实况图接口响应解析失败: {e}, 尝试下一接口/降级")
            return None


class CdnUrl(Struct):
    cdn: str
    url: str | None = None


class Atlas(Struct):
    music_cdn_list: list[CdnUrl] = field(name="musicCdnList", default_factory=list)
    cdn_list: list[CdnUrl] = field(name="cdnList", default_factory=list)
    size: list[dict] = field(name="size", default_factory=list)
    img_route_list: list[str] = field(name="list", default_factory=list)

    @property
    def img_urls(self):
        if len(self.cdn_list) == 0 or len(self.img_route_list) == 0:
            return []
        cdn = choice(self.cdn_list).cdn
        return [f"https://{cdn}/{url}" for url in self.img_route_list]


class SingleParams(Struct):
    """ext_params.single: 单图作品配置 (实况图时 type == 3)"""

    type: int | str | None = None
    volume: int | None = None
    music: str | None = None
    mtype: int | None = None


class ExtParams(Struct):
    atlas: Atlas = field(default_factory=Atlas)
    # single 形状随作品类型变化, 放宽为 Union 防止整体解码失败
    single: SingleParams | str | int | None = field(default=None)


class Photo(Struct):
    # 标题
    caption: str
    timestamp: int
    duration: int = 0
    user_name: str = field(default="未知用户", name="userName")
    head_url: str | None = field(default=None, name="headUrl")
    cover_urls: list[CdnUrl] = field(name="coverUrls", default_factory=list)
    main_mv_urls: list[CdnUrl] = field(name="mainMvUrls", default_factory=list)
    single_picture: bool = field(default=False, name="singlePicture")
    photo_type: str | None = field(default=None, name="photoType")
    ext_params: ExtParams = field(name="ext_params", default_factory=ExtParams)

    like_count: int | str | None = field(default=None, name="likeCount")
    """点赞数"""
    comment_count: int | str | None = field(default=None, name="commentCount")
    """评论数"""
    forward_count: int | str | None = field(default=None, name="forwardCount")
    """转发数"""
    view_count: int | str | None = field(default=None, name="viewCount")
    """播放数"""

    @property
    def name(self) -> str:
        return self.user_name.replace("\u3164", "").strip()

    @property
    def cover_url(self):
        return choice(self.cover_urls).url if len(self.cover_urls) != 0 else None

    @property
    def cover_url_list(self) -> list[str]:
        """封面图地址。

        coverUrls 里放的是同一张图的多个 CDN 镜像（路径相同、仅 host 不同，
        如 p2.a.yximgs.com / p23.a.yximgs.com），全部返回会导致重复发同一张图，
        因此这里只取第一个有效地址。
        """
        for c in self.cover_urls:
            if c.url:
                return [c.url]
        return []

    @property
    def is_picture(self) -> bool:
        """是否为图文作品（非视频）"""
        if self.single_picture:
            return True
        return "PICTURE" in (self.photo_type or "").upper()

    @property
    def is_live_photo(self) -> bool:
        """是否为单图实况作品 (ext_params.single.type == 3)。

        仅覆盖单图实况; 图集型实况 (每张图都是 live) 在 H5 数据里
        atlas.type 恒为 1, 与普通图集无法区分, 由上层探测聚合接口确认。
        """
        single = self.ext_params.single
        if not isinstance(single, SingleParams) or single.type is None:
            return False
        try:
            return int(single.type) == 3
        except (TypeError, ValueError):
            return False

    @property
    def video_url(self):
        return choice(self.main_mv_urls).url if len(self.main_mv_urls) != 0 else None

    @property
    def img_urls(self):
        return self.ext_params.atlas.img_urls


class TusjohData(Struct):
    result: int
    photo: Photo | None = None


KuaishouInitState: TypeAlias = dict[str, TusjohData]
