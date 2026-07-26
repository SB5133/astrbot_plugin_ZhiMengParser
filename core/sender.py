import asyncio
import random
import string
from itertools import chain
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 控制是否渲染卡片、是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定“内容是什么”
    - 只负责“怎么发”
    """

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    @staticmethod
    def _clamp_range(lo: int | None, hi: int | None) -> tuple[float, float]:
        """把可空的上下限配置整理成非负且有序的区间"""
        lo_v, hi_v = lo or 0, hi or 0
        return max(0, min(lo_v, hi_v)), max(0, max(lo_v, hi_v))

    async def sleep_interval(self) -> None:
        """发送消息前的随机间隔（send_interval_min ~ send_interval_max 秒）"""
        lo, hi = self._clamp_range(
            self.cfg.send_interval_min, self.cfg.send_interval_max
        )
        if hi > 0:
            await asyncio.sleep(random.uniform(lo, hi))

    @staticmethod
    def render_template(template: str, ctx: dict[str, Any]) -> str:
        """按占位符渲染模板，当前解析结果未提供的占位符自动隐藏"""

        # 收集模板中真实占位符（跳过 {{ }} 转义）
        keys = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(template)
            if field_name is not None
        }
        missing = [k for k in keys if k not in ctx]
        if missing:
            logger.warning(
                f"解析文本模板包含未提供的占位符: {missing}，将自动隐藏"
            )

        class _SafeDict(dict):
            def __missing__(self, key):
                return ""

        filled = {k: ("" if v is None or v == "" else v) for k, v in ctx.items()}
        return template.format_map(_SafeDict(filled))

    def build_parse_text(self, result: ParseResult) -> str | None:
        """按模板构建解析文本，模板为空或渲染失败时返回 None"""
        template = (self.cfg.parse_text_template or "").strip()
        if not template:
            return None

        # 部分解析器（如 B 站）的 text 自带“简介: ”前缀，剥离避免重复
        text = result.text
        if text and text.startswith("简介:"):
            text = text[3:].strip()

        ctx: dict[str, Any] = {
            "platform": result.platform.display_name,
            "title": result.title,
            "text": text,
            "author": result.author.name if result.author else None,
            "url": result.url,
            "time": result.formatted_datetime(),
        }
        # 附加统计数据（点赞/投币/收藏等，由各解析器提供）
        ctx.update(result.extra)

        try:
            rendered = self.render_template(template, ctx).strip()
        except Exception as e:
            logger.warning(f"解析文本模板渲染失败: {e}")
            return None

        self.cfg.verbose(f"解析文本已生成: {rendered[:120]!r}")
        return rendered or None

    def _to_file_uri(self, path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        return path.as_uri()

    @staticmethod
    def _image_from_path(path: Path) -> Image:
        return Image.fromFileSystem(str(path))

    @staticmethod
    def _video_from_path(path: Path) -> Video:
        return Video.fromFileSystem(str(path))

    @staticmethod
    def _record_from_path(path: Path) -> Record:
        return Record.fromFileSystem(str(path))

    async def _maybe_compress(self, path: Path) -> Path:
        """如开启视频压缩则对视频进行压缩，失败时回退原文件"""
        compressor = getattr(self.cfg, "compressor", None)
        if not compressor or not compressor.enabled:
            return path
        try:
            return await compressor.compress(path)
        except Exception as e:
            logger.error(f"[MessageSender] 视频压缩失败，使用原文件: {e}")
            return path

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    def _build_send_plan(
        self,
        result: ParseResult,
        contents: list | tuple | None = None,
        *,
        force_merge_override: bool | None = None,
        render_card_override: bool | None = None,
    ) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做“策略决策”，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            match cont:
                case ImageContent() | GraphicsContent() | TextContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)

        # 仅在“单一重媒体且无其他内容”时，才允许渲染卡片
        is_single_heavy = len(heavy) == 1 and not light
        # 纯图片内容（无重媒体，且含图片/图文）也允许渲染卡片
        is_image_only = not heavy and any(
            isinstance(cont, (ImageContent, GraphicsContent)) for cont in light
        )
        # 卡片渲染总开关（旧配置缺该字段时默认开启）
        master = self.cfg.render_card
        if master is None:
            master = True
        render_card = master and (
            (is_single_heavy and self.cfg.single_heavy_render_card)
            or (is_image_only and self.cfg.image_render_card)
        )
        if render_card_override is not None:
            render_card = render_card_override
        # 实际消息段数量（卡片也算一个段）
        seg_count = len(light) + len(heavy) + (1 if render_card else 0)

        # 达到阈值后，强制合并转发，避免刷屏
        force_merge = seg_count >= self.cfg.forward_threshold
        if force_merge_override is not None:
            force_merge = force_merge_override

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            # 预览卡片：仅在“渲染卡片 + 不合并”时独立发送
            "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
        }

    async def _send_preview_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        plan: dict,
    ) -> bool:
        """
        发送预览卡片（独立消息）

        场景：
        - 只有一个重媒体
        - 未触发合并转发
        - 卡片作为“预览”，不与正文混合

        Returns:
            卡片是否实际发出
        """
        if not plan["preview_card"]:
            return False

        if image_path := await self.renderer.render_card(result, cfg=self.cfg):
            await self.sleep_interval()
            await event.send(event.chain_result([self._image_from_path(image_path)]))
            return True
        return False

    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件
        """
        segs: list[BaseMessageComponent] = []

        # 合并转发时，卡片以内联形式作为一个消息段参与合并
        if plan["render_card"] and plan["force_merge"]:
            if image_path := await self.renderer.render_card(result, cfg=self.cfg):
                segs.append(self._image_from_path(image_path))

        # 轻媒体处理
        for cont in plan["light"]:
            if isinstance(cont, TextContent):
                if cont.text:
                    segs.append(Plain(cont.text))
                continue

            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case ImageContent():
                    segs.append(self._image_from_path(path))
                case GraphicsContent() as g:
                    segs.append(self._image_from_path(path))
                    # GraphicsContent 允许携带补充文本
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except SizeLimitException:
                segs.append(Plain("此项媒体超过大小限制"))
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case VideoContent():
                    compressed_path = await self._maybe_compress(path)
                    segs.append(self._video_from_path(compressed_path))
                case DynamicContent():
                    segs.append(self._video_from_path(path))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=self._to_file_uri(path))
                        if self.cfg.audio_to_file
                        else self._record_from_path(path)
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=self._to_file_uri(path)))

        return segs

    @staticmethod
    def _add_reply_to_first(
        segs: list[BaseMessageComponent],
        msg_id: int | str | None,
    ) -> list[BaseMessageComponent]:
        """在消息段列表开头追加 Reply（如尚未存在）"""
        if msg_id is None or not segs:
            return segs
        if isinstance(segs[0], Reply):
            return segs
        return [Reply(id=msg_id), *segs]

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
        parser_tip: list[BaseMessageComponent] | None = None,
        parse_text_segments: list[BaseMessageComponent] | None = None,
        merge_quote_id: int | str | None = None,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - tip → parse_text → 每个原始消息段 依次成为一个 Node
        - 统一使用机器人自身身份
        - 若提供了 merge_quote_id，会在第一个节点开头插入 Reply 引用原消息
        """
        if not force_merge:
            return segs
        if not segs and not parser_tip and not parse_text_segments:
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()

        if parser_tip:
            nodes.nodes.append(
                Node(uin=self_id, name="解析器", content=parser_tip)
            )
        if parse_text_segments:
            nodes.nodes.append(
                Node(uin=self_id, name="解析器", content=parse_text_segments)
            )

        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name="解析器", content=[seg]))

        if nodes.nodes and merge_quote_id is not None:
            first = nodes.nodes[0]
            first.content = self._add_reply_to_first(first.content, merge_quote_id)

        return [nodes]

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []

    def _resolve_groups(self, result: ParseResult) -> list[SendGroup]:
        if result.send_groups:
            return result.send_groups
        return [SendGroup(contents=list(MessageSender._iter_contents(result)))]

    async def _send_group(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        group: SendGroup,
        parser_tip: list[BaseMessageComponent] | None = None,
        parse_text_segments: list[BaseMessageComponent] | None = None,
        merge_quote_id: int | str | None = None,
    ) -> bool:
        plan = self._build_send_plan(
            result,
            group.contents,
            force_merge_override=group.force_merge,
            render_card_override=group.render_card,
        )

        seg_count = len(plan["light"]) + len(plan["heavy"]) + (
            1 if plan["render_card"] else 0
        )
        self.cfg.verbose(
            f"发送计划: light={len(plan['light'])}, heavy={len(plan['heavy'])}, "
            f"seg_count={seg_count}, forward_threshold={self.cfg.forward_threshold}, "
            f"force_merge={plan['force_merge']}, render_card={plan['render_card']}, "
            f"has_parser_tip={bool(parser_tip)}, has_parse_text={bool(parse_text_segments)}"
        )

        # 记录本组是否已发送任何内容（提示、解析文本、卡片、媒体均计入）
        sent_any = False

        # 若未触发合并转发，缓存的解析提示 / 解析文本作为普通消息先发出
        if not plan["force_merge"]:
            if parser_tip:
                await self.sleep_interval()
                await event.send(
                    event.chain_result(
                        self._add_reply_to_first(parser_tip, merge_quote_id)
                    )
                )
                self.cfg.verbose("解析提示已单独发送")
                sent_any = True
                parser_tip = None
            if parse_text_segments:
                await self.sleep_interval()
                await event.send(
                    event.chain_result(
                        self._add_reply_to_first(parse_text_segments, merge_quote_id)
                    )
                )
                self.cfg.verbose("解析文本已单独发送")
                sent_any = True
                parse_text_segments = None

        preview_sent = await self._send_preview_card(event, result, plan)
        if preview_sent:
            sent_any = True

        segs = await self._build_segments(result, plan)
        segs = self._merge_segments_if_needed(
            event,
            segs,
            plan["force_merge"],
            parser_tip=parser_tip,
            parse_text_segments=parse_text_segments,
            merge_quote_id=merge_quote_id,
        )

        if not segs:
            # 无媒体段，但提示/解析文本/卡片已发出时仍视为成功，避免兜底文本重复
            self.cfg.verbose(f"本组无媒体段，已发送内容={sent_any}")
            return sent_any

        try:
            await self.sleep_interval()
            await event.send(event.chain_result(segs))
            self.cfg.verbose(f"本组消息已发送: {self._collect_seg_meta(segs)}")
            return True
        except Exception as e:
            seg_meta = self._collect_seg_meta(segs)
            logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            # 发送失败时，如果之前已发送过提示/解析文本/卡片，仍视为本组有成功输出
            return sent_any

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        """提取消息段元信息，用于失败日志定位。"""
        meta: list[dict[str, str]] = []

        for seg in segs:
            item = {"type": seg.__class__.__name__}
            for attr in ("file", "path", "url"):
                value = getattr(seg, attr, None)
                if value:
                    item["media"] = str(value)
                    break
            meta.append(item)

        return meta

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        parser_tip: list[BaseMessageComponent] | None = None,
        parse_text_segments: list[BaseMessageComponent] | None = None,
        merge_quote_id: int | str | None = None,
        parse_text_already_sent: bool = False,
    ):
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 构建发送计划
        2. 发送预览卡片（如有）
        3. 构建消息段
        4. 必要时合并转发
        5. 最终发送

        Args:
            parser_tip: 识别到链接时缓存的解析提示消息段，仅会合并到第一组转发中
            parse_text_segments: 解析完成后的解析文本消息段，仅会合并到第一组转发中
            merge_quote_id: 合并套娃要引用的原消息 ID，None 表示不引用
            parse_text_already_sent: 解析文本是否已在主流程中单独发送；
                为 True 时，若后续无内容可发，不再触发兜底文本，避免重复
        """
        groups = self._resolve_groups(result)
        self.cfg.verbose(f"解析结果分组数: {len(groups)}")

        sent = False
        for idx, group in enumerate(groups):
            tip = parser_tip if idx == 0 else None
            text = parse_text_segments if idx == 0 else None
            sent = (
                await self._send_group(
                    event,
                    result,
                    group,
                    parser_tip=tip,
                    parse_text_segments=text,
                    merge_quote_id=merge_quote_id,
                )
                or sent
            )

        # 主流程已单独发送过解析文本，等价于已发送内容
        if parse_text_already_sent:
            self.cfg.verbose("主流程已单独发送解析文本，兜底文本跳过")
            sent = True

        if not sent:
            segs = self._build_text_fallback(result)
            if not segs:
                logger.warning("发送结果为空，不执行发送")
                return

            try:
                await self.sleep_interval()
                await event.send(event.chain_result(segs))
                self.cfg.verbose("已发送兜底文本")
            except Exception as e:
                seg_meta = self._collect_seg_meta(segs)
                logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return
