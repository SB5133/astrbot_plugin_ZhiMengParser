from __future__ import annotations

import json
import zoneinfo
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.context import Context
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


class ConfigNodeContainer:
    """
    配置节点容器, 把 list 的 dict 变成 dict 的对象集合。

    - nodes: list[dict[str, Any]]
    - item_cls 用于包装 dict 成强类型节点
    - key_name 作为属性名访问, 默认为 "__template_key"
    """

    def __init__(
        self,
        nodes: list[dict[str, Any]],
        item_cls: type[ConfigNode],
        key_name="__template_key",
    ):
        self._nodes: dict[str, ConfigNode] = {}
        for node in nodes:
            key = node.get(key_name)
            if not key:
                logger.warning(f"[node] 缺少 {key_name}，已跳过")
                continue
            if key in self._nodes:
                logger.warning(f"[node] {key} 重复配置，已覆盖")
            self._nodes[key] = item_cls(node)

    def __getattr__(self, name: str) -> ConfigNode:
        if name in self._nodes:
            return self._nodes[name]
        raise AttributeError(name)

    def __iter__(self):
        return iter(self._nodes.values())

    def keys(self):
        return self._nodes.keys()

    def items(self):
        return self._nodes.items()


# ================ 插件自定义配置 ==================


class ParserItem(ConfigNode):
    __template_key: str
    enable: bool
    use_proxy: bool
    download_concurrency: int | None
    cookies: str | None
    show_body_text: bool | None
    video_send_mode: str | None
    video_codec_list: list | None
    video_quality: str | None
    nsfw: str | None

    @property
    def name(self) -> str:
        return self._data.get("__template_key")


class ParserConfig(ConfigNodeContainer):
    acfun: ParserItem
    bilibili: ParserItem
    douyin: ParserItem
    instagram: ParserItem
    kuaishou: ParserItem
    ncm: ParserItem
    nga: ParserItem
    tiktok: ParserItem
    twitter: ParserItem
    weibo: ParserItem
    xiaoheihe: ParserItem
    zhihu: ParserItem
    xhs: ParserItem
    youtube: ParserItem
    iwara: ParserItem
    shipinhao: ParserItem

    def __init__(self, nodes: list[dict[str, Any]]):
        super().__init__(nodes, item_cls=ParserItem)

    def platforms(self) -> list[str]:
        return list(self._nodes.keys())

    def enabled_platforms(self) -> list[str]:
        return [k for k, v in self._nodes.items() if getattr(v, "enable", True)]


class GroupOverride(ConfigNode):
    group_id: str
    enable: bool | None
    link_debounce_strategy: str | None
    link_debounce_tip_text: str | None
    detect_action: str | None
    send_parse_text: bool | None
    render_card: bool | None
    image_render_card: bool | None
    single_heavy_render_card: bool | None
    forward_threshold: int | None
    merge_parsing_tip: bool | None
    merge_parse_text: bool | None
    quote_on_detect: bool | None
    merge_quote_target: str | None
    at_after_parse: bool | None
    at_after_parse_text: str | None
    parsing_tip: str | None
    parse_text_template: str | None

    perf_render_thread_pool: bool | None
    perf_render_cache_enabled: bool | None
    perf_render_cache_ttl: int | None
    perf_render_cache_max_count: int | None

    perf_adaptive_download: bool | None
    perf_download_default_concurrency: int | None
    perf_download_fail_threshold: int | None
    perf_download_degrade_step: int | None
    perf_download_min_concurrency: int | None
    perf_download_recover_step: int | None
    perf_download_recover_interval: int | None

    dns_prefetch_enabled: bool | None
    cdn_prefetch_enabled: bool | None
    enable_range_download: bool | None
    range_download_max_parts: int | None
    range_download_max_parts_platforms: dict | None
    range_memory_fallback_to_single: bool | None
    range_merge_threshold_mb: int | None
    range_part_timeout: int | None
    range_total_timeout: int | None
    range_memory_adaptive: bool | None
    range_memory_reserve_percent: int | None
    enable_streaming_compress: bool | None
    video_cache_enabled: bool | None
    video_cache_ttl: int | None
    video_cache_max_count: int | None
    memory_monitor_enabled: bool | None
    memory_monitor_interval: int | None
    memory_monitor_warning_threshold: int | None


class EffectiveConfig:
    """
    群覆盖配置代理。

    对 base PluginConfig 做只读覆盖：如果指定群在 group_overrides 中有配置，
    优先使用群配置；否则回退到全局配置。
    """

    def __init__(self, base: PluginConfig, overrides: dict[str, Any]):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, key: str) -> Any:
        if key in self._overrides:
            val = self._overrides[key]
            # None 与空字符串视为未覆盖，回退到全局配置
            if val is not None and val != "":
                return val
        return getattr(self._base, key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key.startswith("_"):
            object.__setattr__(self, key, value)
            return
        raise AttributeError("EffectiveConfig 是只读的，不允许写入")


class PluginConfig(ConfigNode):
    whitelist: list[str]
    blacklist: list[str]
    user_blacklist: list[str]

    arbiter: bool
    debounce_interval: int
    link_debounce_strategy: str
    link_debounce_tip_text: str
    enable_private_chat: bool

    verbose_logging: bool

    source_max_size: int
    source_max_minute: int

    video_compress_enable: bool
    video_compress_quality_mode: str
    video_compress_encoder: str
    video_compress_custom_preset: str
    video_compress_custom_resolution: str
    video_compress_custom_audio_bitrate: str
    video_compress_custom_fps: str
    video_compress_custom_threads: int
    video_compress_custom_bitrate: str

    audio_to_file: bool
    single_heavy_render_card: bool
    image_render_card: bool
    forward_threshold: int

    card_bg_image: str
    card_bg_blur: int
    card_glass_opacity: int

    render_card: bool
    send_parse_text: bool
    detect_action: str
    merge_parsing_tip: bool
    merge_parse_text: bool
    merge_quote_target: str
    quote_on_detect: bool
    at_after_parse: bool
    at_after_parse_text: str
    detect_delay_min: int
    detect_delay_max: int
    send_interval_min: int
    send_interval_max: int
    react_emoji_id: int
    parsing_tip: str
    parse_text_template: str

    group_overrides: list[dict[str, Any]]

    show_download_fail_tip: bool
    download_timeout: int
    download_retry_times: int
    common_timeout: int

    perf_render_thread_pool: bool
    perf_render_cache_enabled: bool
    perf_render_cache_ttl: int
    perf_render_cache_max_count: int

    perf_adaptive_download: bool
    perf_download_default_concurrency: int
    perf_download_fail_threshold: int
    perf_download_degrade_step: int
    perf_download_min_concurrency: int
    perf_download_recover_step: int
    perf_download_recover_interval: int

    dns_prefetch: dict[str, Any]
    cdn_prefetch_enabled: bool
    enable_range_download: bool
    range_download_max_parts: int
    range_download_max_parts_platforms: dict[str, Any]
    range_memory_fallback_to_single: bool
    range_merge_threshold_mb: int
    range_part_timeout: int
    range_total_timeout: int
    range_memory_adaptive: bool
    range_memory_reserve_percent: int
    enable_streaming_compress: bool
    video_cache_enabled: bool
    video_cache_ttl: int
    video_cache_max_count: int
    memory_monitor: dict[str, Any]

    proxy: str | None

    clean_cron: str

    parsers_template: list[dict[str, Any]]

    _plugin_name = "astrbot_plugin_ZhiMengParser"

    def __init__(self, config: AstrBotConfig, context: Context):
        super().__init__(config)
        self.context = context
        self.admins_id = self.context.get_config().get("admins_id", [])

        # ---------- 内置配置 ----------
        self.emoji_cdn = "https://cdn.jsdelivr.net/npm/emoji-datasource-facebook@14.0.0/img/facebook/64/"
        self.emoji_style = "FACEBOOK"  # 可选：APPLE、FACEBOOK、GOOGLE、TWITTER

        # ---------- 派生字段 ----------
        self.proxy = self.proxy or None
        self.max_duration = self.source_max_minute * 60
        self.max_size = self.source_max_size * 1024 * 1024

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        # ---------- 路径 ----------
        self.data_dir = StarTools.get_data_dir(self._plugin_name)
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = self.data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        self.default_template_file = self.plugin_dir / "default_template.json"

        # ---------- Parser ----------
        if not self.parsers_template:
            self.parsers_template[:] = self.load_parser_template(
                self.default_template_file
            )
            self.save_config()

        self.parser = ParserConfig(self.parsers_template)

        # ---------- 群覆盖配置 ----------
        self._group_overrides: dict[str, dict[str, Any]] = {}
        for node in self.group_overrides or []:
            try:
                g = GroupOverride(node)
                gid = g.group_id
                if not gid:
                    continue
                overrides = {}
                for field in GroupOverride._fields():
                    if field == "group_id":
                        continue
                    val = getattr(g, field, None)
                    if val is not None:
                        overrides[field] = val
                if overrides:
                    self._group_overrides[gid] = overrides
            except Exception as e:
                logger.warning(f"[group_overrides] 解析群配置失败: {e}, node={node}")

        # ---------- 视频压缩器占位，由 main.py 在 initialize 中异步初始化 ----------
        self.compressor: Any | None = None

    def verbose(self, message: str) -> None:
        """详细日志输出。开启 verbose_logging 时使用 INFO 级别，否则使用 DEBUG 级别。"""
        if self.verbose_logging:
            logger.info(f"[ZhiMengParser|详细] {message}")
        else:
            logger.debug(f"[ZhiMengParser] {message}")

    def effective(self, event: AstrMessageEvent) -> EffectiveConfig:
        """根据事件所属群/会话返回带群覆盖的配置代理"""
        group_id = event.get_group_id()
        overrides = self._group_overrides.get(group_id, {})
        return EffectiveConfig(self, overrides)

    @staticmethod
    def load_parser_template(file: Path) -> list[dict[str, Any]]:
        try:
            with file.open(encoding="utf-8-sig") as f:
                template = json.loads(f.read())
                logger.info(f"[parser] 加载模板成功: {file}")
                return template
        except Exception as e:
            logger.error(f"[parser] 加载模板失败: {e}")
            return []

    def add_blacklist(self, umo: str):
        if umo not in self.blacklist:
            self.blacklist.append(umo)
            self.save_config()

    def remove_blacklist(self, umo: str):
        if umo in self.blacklist:
            self.blacklist.remove(umo)
            self.save_config()
