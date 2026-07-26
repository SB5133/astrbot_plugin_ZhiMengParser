# main.py

import asyncio
import random
import re

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import (
    At,
    BaseMessageComponent,
    Image,
    Json,
    Plain,
    Reply,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.cache import RenderCacheManager
from .core.clean import CacheCleaner
from .core.compress import VideoCompressor
from .core.config import PluginConfig
from .core.debounce import Debouncer
from .core.download import Downloader
from .core.hw_detect import HardwareDetector
from .core.parsers import BaseParser, BilibiliParser
from .core.render import Renderer
from .core.sender import MessageSender
from .core.utils import extract_json_url


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        # 渲染缓存管理器
        self.render_cache = RenderCacheManager(
            cache_dir=self.cfg.cache_dir,
            ttl=self.cfg.perf_render_cache_ttl,
            max_count=self.cfg.perf_render_cache_max_count,
            enabled=self.cfg.perf_render_cache_enabled,
        )
        # 渲染器
        self.renderer = Renderer(self.cfg, cache_manager=self.render_cache)
        # 下载器
        self.downloader = Downloader(self.cfg)
        # 防抖器
        self.debouncer = Debouncer(self.cfg)
        # 仲裁器
        self.arbiter = EmojiLikeArbiter()
        # 消息发送器
        self.sender = MessageSender(self.cfg, self.renderer)
        # 缓存清理器
        self.cleaner = CacheCleaner(self.cfg)
        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}
        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

    async def initialize(self):
        """加载、重载插件时触发"""
        # 加载渲染器资源
        await asyncio.to_thread(Renderer.load_resources)
        # 注册解析器
        self._register_parser()
        # 异步硬件检测（结果仅输出到日志）
        try:
            detector = HardwareDetector()
            hw_info = await detector.detect()
            detector.log_summary()
            self.cfg.compressor = VideoCompressor(hw_info, self.cfg)
            # 若检测到手机端且用户未手动调整，给出提示
            if hw_info.is_mobile and self.cfg.video_compress_enable:
                logger.warning(
                    "[VideoCompress] 检测到手机/Termux/Android 环境，建议关闭视频压缩或选择 mediacodec 编码器"
                )
        except Exception as e:
            logger.exception(f"[VideoCompress] 硬件检测初始化失败: {e}")
            self.cfg.compressor = None

    async def terminate(self):
        """插件卸载时触发"""
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话 (去重后的实例)
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        # 关渲染线程池
        self.renderer.close()
        # 关缓存清理器
        await self.cleaner.stop()

    def _register_parser(self):
        """注册解析器（以 parser.enable 为唯一启用来源）"""
        # 所有 Parser 子类
        all_subclass = BaseParser.get_all_subclass()
        enabled_platforms = set(self.cfg.parser.enabled_platforms())

        enabled_classes: list[type[BaseParser]] = []
        enabled_names: list[str] = []
        for cls in all_subclass:
            platform_name = cls.platform.name

            if platform_name not in enabled_platforms:
                self.cfg.verbose(f"[parser] 平台未启用或未配置: {platform_name}")
                continue

            enabled_classes.append(cls)
            enabled_names.append(platform_name)

            # 一个平台一个 parser 实例
            parser = cls(self.cfg, self.downloader)

            # 关键词 → parser
            for keyword, _ in cls._key_patterns:
                self.parser_map[keyword] = parser

        self.cfg.verbose(f"启用平台: {'、'.join(enabled_names) if enabled_names else '无'}")

        # -------- 关键词-正则表（统一生成） --------
        patterns: list[tuple[str, re.Pattern[str]]] = []

        for cls in enabled_classes:
            for kw, pat in cls._key_patterns:
                patterns.append((kw, re.compile(pat) if isinstance(pat, str) else pat))

        # 长关键词优先，避免短词抢匹配
        patterns.sort(key=lambda x: -len(x[0]))

        self.key_pattern_list = patterns

        self.cfg.verbose(f"[parser] 关键词-正则对已生成: {[kw for kw, _ in patterns]}")

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        umo = event.unified_msg_origin

        # 使用群覆盖后的有效配置
        cfg = self.cfg.effective(event)
        sender = MessageSender(cfg, self.renderer)

        # 白名单
        if cfg.whitelist and umo not in cfg.whitelist:
            cfg.verbose(f"会话 {umo} 不在白名单，跳过")
            return

        # 黑名单
        if cfg.blacklist and umo in cfg.blacklist:
            cfg.verbose(f"会话 {umo} 在黑名单，跳过")
            return

        # 用户黑名单
        user_id = event.get_sender_id()
        if cfg.user_blacklist and user_id in cfg.user_blacklist:
            cfg.verbose(f"用户 {user_id} 在用户黑名单，跳过")
            return

        # 私聊开关
        if event.is_private_chat() and not cfg.enable_private_chat:
            cfg.verbose("私聊解析已关闭，跳过")
            return

        # 群覆盖总开关
        group_id = event.get_group_id()
        if group_id and not getattr(cfg, "enable", True):
            cfg.verbose(f"群 {group_id} 在覆盖配置中关闭了解析，跳过")
            return

        # 消息链
        chain = event.get_messages()
        if not chain:
            return

        seg1 = chain[0]
        text = event.message_str

        # 卡片解析：解析Json组件，提取URL
        if isinstance(seg1, Json):
            text = extract_json_url(seg1.data)
            cfg.verbose(f"解析Json组件: {text}")

        if not text:
            return

        cfg.verbose(f"收到消息: {text[:120]!r}, umo={umo}, user={user_id}, group={group_id}")

        self_id = event.get_self_id()

        # 指定机制：专门@其他bot的消息不解析
        if isinstance(seg1, At) and str(seg1.qq) != self_id:
            return

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            cfg.verbose(f"未匹配到支持的链接: {text[:120]!r}")
            return
        link = searched.group(0)
        cfg.verbose(f"匹配到平台: {keyword}, 链接: {link}")

        # 仲裁机制
        is_win = True
        if cfg.arbiter and isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                logger.warning(f"Unexpected raw_message type: {type(raw)}")
                return
            is_win = await self.arbiter.compete(
                bot=event.bot,
                ctx=ArbiterContext(
                    message_id=int(raw["message_id"]),
                    msg_time=int(raw["time"]),
                    self_id=int(raw["self_id"]),
                ),
            )
            if not is_win:
                cfg.verbose("Bot在仲裁中输了, 跳过解析")
                return
            cfg.verbose("Bot在仲裁中胜出, 准备解析...")
        elif not cfg.arbiter:
            cfg.verbose("仲裁机制已关闭，直接解析")

        # 原消息 ID，用于引用
        msg_id = self._get_source_message_id(event)
        if msg_id is not None:
            cfg.verbose(f"原消息 ID: {msg_id}")

        # 基于link防抖
        if self.debouncer.hit_link(umo, link):
            cfg.verbose(f"[链接防抖] 链接 {link} 在防抖时间内")
            strategy = (cfg.link_debounce_strategy or "skip").strip().lower()
            if strategy == "silent":
                cfg.verbose("[链接防抖] 策略为 silent，不发送任何反应")
                return
            if strategy == "tip":
                await self._send_debounce_tip(event, cfg, parser.platform.display_name, msg_id)
                return
            # 默认 skip：保持原行为，仅日志警告后跳过解析
            logger.warning(f"[链接防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        parser = self.parser_map[keyword]
        cfg.verbose(f"选用解析器: {parser.platform.display_name}")

        # 识别到链接后的反馈行为（贴表情 / 发送解析提示，带随机延时）
        await self._detect_action(event, cfg, parser.platform.display_name, msg_id)

        # 解析
        parse_res = await parser.parse(keyword, searched)

        # 基于资源ID防抖
        resource_id = parse_res.get_resource_id()
        if self.debouncer.hit_resource(umo, resource_id):
            logger.warning(f"[资源防抖] 资源 {resource_id} 在防抖时间内，跳过发送")
            return

        # 发送解析文本（标题/简介/作者/数据，模板可自定义）
        cfg.verbose(f"[send_parse_text] 开关状态: {cfg.send_parse_text}")
        parse_text_segments: list[BaseMessageComponent] | None = None
        parse_text_already_sent = False
        if cfg.send_parse_text:
            parse_text = sender.build_parse_text(parse_res)
            if parse_text:
                if cfg.merge_parse_text:
                    cfg.verbose("解析文本已缓存，将合并到转发结果中")
                    parse_text_segments = [Plain(parse_text)]
                else:
                    await sender.sleep_interval()
                    await event.send(event.plain_result(parse_text))
                    parse_text_already_sent = True
                    cfg.verbose("解析文本已单独发送")

        # 发送解析结果（媒体/卡片等），同时传入缓存的解析提示与解析文本用于合并套娃
        parser_tip = getattr(event, "_parser_tip", None)
        merge_quote_id = msg_id if cfg.merge_quote_target == "original" else None
        await sender.send_parse_result(
            event,
            parse_res,
            parser_tip=parser_tip,
            parse_text_segments=parse_text_segments,
            merge_quote_id=merge_quote_id,
            parse_text_already_sent=parse_text_already_sent,
        )

        # 解析完成后@用户 + 自定义文本（可选，不合并）
        if cfg.at_after_parse:
            await self._send_after_parse_at(event, cfg, parse_res, sender)

    @staticmethod
    def _get_source_message_id(event: AstrMessageEvent) -> int | str | None:
        """获取用户原消息的消息ID，用于引用回复"""
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict) and "message_id" in raw:
            return raw["message_id"]
        return getattr(event.message_obj, "message_id", None)

    async def _detect_action(
        self,
        event: AstrMessageEvent,
        cfg: PluginConfig,
        platform_name: str,
        msg_id: int | str | None,
    ):
        """识别到链接后的反馈行为：发送解析提示或贴表情（带随机延时）

        与 send_parse_text 解耦：detect_action 只控制识别到链接后的即时反馈，
        send_parse_text 只控制解析完成后是否发送解析文本。

        新增行为：
        - merge_parsing_tip: 缓存提示，后续合并到转发结果中（套娃）
        - quote_on_detect: 单独发出 text 提示时引用用户原消息
        """
        action = (cfg.detect_action or "none").strip().lower()
        cfg.verbose(
            f"[detect_action] 当前配置: {action}, send_parse_text: {cfg.send_parse_text}, "
            f"merge_parsing_tip={cfg.merge_parsing_tip}, quote_on_detect={cfg.quote_on_detect}"
        )
        if action not in ("text", "emoji"):
            cfg.verbose("识别反馈行为为 none，不发送即时反馈")
            return

        lo, hi = MessageSender._clamp_range(
            cfg.detect_delay_min, cfg.detect_delay_max
        )
        if hi > 0:
            await asyncio.sleep(random.uniform(lo, hi))

        if action == "emoji":
            await self._react_emoji(event, cfg)
            return

        # action == "text"：发送解析提示
        template = (cfg.parsing_tip or "").strip()
        if not template:
            cfg.verbose("[detect_action] parsing_tip 为空，跳过解析提示")
            return
        tip = MessageSender.render_template(template, {"platform": platform_name})
        cfg.verbose(f"[detect_action] 生成解析提示: {tip}")

        tip_chain: list[BaseMessageComponent] = [Plain(tip)]

        # 单独发出时才使用 quote_on_detect；合并到套娃时由 merge_quote_target 统一处理
        if cfg.merge_parsing_tip:
            cfg.verbose("解析提示已缓存，将合并到转发结果中（套娃）")
            event._parser_tip = tip_chain
            return

        if cfg.quote_on_detect and msg_id is not None:
            tip_chain.insert(0, Reply(id=msg_id))
            cfg.verbose(f"解析提示将引用用户消息 ID: {msg_id}")

        await event.send(event.chain_result(tip_chain))

    async def _send_debounce_tip(
        self,
        event: AstrMessageEvent,
        cfg: PluginConfig,
        platform_name: str,
        msg_id: int | str | None,
    ):
        """链接防抖触发 tip 策略时发送的提示文本"""
        template = (cfg.link_debounce_tip_text or "").strip()
        if not template:
            cfg.verbose("[link_debounce] tip 策略文本为空，不发送")
            return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        ctx = {
            "platform": platform_name,
            "user_name": user_name or "",
            "user_id": user_id or "",
        }
        tip = MessageSender.render_template(template, ctx)
        cfg.verbose(f"[link_debounce] 发送防抖提示: {tip}")

        tip_chain: list[BaseMessageComponent] = [Plain(tip)]
        if msg_id is not None:
            tip_chain.insert(0, Reply(id=msg_id))
            cfg.verbose(f"[link_debounce] 引用用户消息 ID: {msg_id}")

        try:
            await event.send(event.chain_result(tip_chain))
        except Exception as e:
            logger.warning(f"发送防抖提示失败: {e}")

    async def _react_emoji(self, event: AstrMessageEvent, cfg: PluginConfig):
        """给用户消息贴表情（仅 aiocqhttp 平台支持）"""
        if not isinstance(event, AiocqhttpMessageEvent):
            cfg.verbose("非 aiocqhttp 平台，跳过贴表情")
            return
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict) or "message_id" not in raw:
            return
        try:
            await event.bot.set_msg_emoji_like(
                message_id=int(raw["message_id"]),
                emoji_id=cfg.react_emoji_id or 76,
                emoji_type="1",
                set=True,
            )
        except Exception as e:
            logger.warning(f"贴表情失败: {e}")

    async def _send_after_parse_at(
        self,
        event: AstrMessageEvent,
        cfg: PluginConfig,
        parse_res,
        sender: MessageSender,
    ):
        """解析完成后发送 @用户 + 自定义文本，并引用用户发送的链接"""
        template = (cfg.at_after_parse_text or "").strip()
        if not template:
            cfg.verbose("[at_after_parse] 自定义文本为空，跳过")
            return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        if not user_id:
            logger.warning("[at_after_parse] 无法获取发送者ID，跳过")
            return

        ctx = {
            "platform": parse_res.platform.display_name,
            "title": parse_res.title,
            "author": parse_res.author.name if parse_res.author else None,
            "user_name": user_name,
            "user_id": user_id,
        }
        text = MessageSender.render_template(template, ctx).strip()
        if not text:
            cfg.verbose("[at_after_parse] 渲染后文本为空，跳过")
            return

        cfg.verbose(f"[at_after_parse] 发送 @用户 消息: {text}")

        segs: list[BaseMessageComponent] = [At(qq=user_id, name=user_name), Plain(text)]
        msg_id = self._get_source_message_id(event)
        if msg_id is not None:
            segs.insert(0, Reply(id=msg_id))
            cfg.verbose(f"[at_after_parse] 引用用户消息 ID: {msg_id}")

        await sender.sleep_interval()
        try:
            await event.send(event.chain_result(segs))
        except Exception as e:
            logger.warning(f"[at_after_parse] 发送失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.remove_blacklist(umo)
        yield event.plain_result("当前会话的解析已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.add_blacklist(umo)
        yield event.plain_result("当前会话的解析已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore
        qrcode = await parser.login.login_with_qrcode()
        yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.login.check_qr_state():
            yield event.plain_result(msg)
