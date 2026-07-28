import asyncio
import json
import random
import shutil
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

from .compress import QUALITY_PRESETS
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
from .utils import safe_unlink


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
        self.last_download_stats: dict[str, Any] = {}
        self.last_render_stats: dict[str, Any] = {}
        # 本次任务产生的临时视频文件，发送完成后统一清理
        self._temp_video_paths: list[Path] = []
        # 渲染累计耗时（由 render_card 调用点累加）
        self._render_elapsed: float = 0.0

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

    # ------------------------------------------------------------------
    # 视频发送优化：探测 → 分类（直发/转封装/转码/回退）
    # ------------------------------------------------------------------

    async def _probe_video_format(self, path: Path) -> dict[str, Any] | None:
        """使用 ffprobe 探测视频格式，失败返回 None"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-print_format",
                "json",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return None
            return json.loads(stdout.decode("utf-8", errors="replace"))
        except FileNotFoundError:
            return None
        except Exception:
            return None

    @staticmethod
    def _has_faststart(path: Path) -> bool:
        """通过读取文件头判断 mp4 是否已 faststart（moov 原子在 mdat 之前）"""
        try:
            with open(path, "rb") as f:
                head = f.read(2 * 1024 * 1024)  # 取前 2MB
            idx_moov = head.find(b"moov")
            idx_mdat = head.find(b"mdat")
            if idx_moov < 0:
                return False
            if idx_mdat < 0 or idx_moov < idx_mdat:
                return True
            return False
        except Exception:
            return False

    def _select_video_encoder(self) -> str:
        """根据硬件检测选择 h264 编码器；硬件不可用时回退 libx264"""
        compressor = getattr(self.cfg, "compressor", None)
        if compressor is not None:
            try:
                enc = compressor.hw.recommended_encoder
                if enc and enc != "mediacodec":
                    return enc
            except Exception:
                pass
        return "libx264"

    def _track_temp(self, path: Path) -> Path:
        """记录临时视频文件，发送完成后清理"""
        try:
            self._temp_video_paths.append(path)
        except Exception:
            pass
        return path

    async def _cleanup_temp_videos(self) -> None:
        """清理本次任务产生的临时视频文件"""
        if not self._temp_video_paths:
            return
        paths = list(self._temp_video_paths)
        self._temp_video_paths.clear()
        await asyncio.gather(*(safe_unlink(p) for p in paths), return_exceptions=True)

    async def _fast_remux(self, input_path: Path) -> Path | None:
        """快速转封装到 mp4 + faststart，不重新编码"""
        output = input_path.with_name(f"{input_path.stem}_remux.mp4")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0 or not output.exists() or output.stat().st_size == 0:
                await safe_unlink(output)
                logger.warning(
                    f"Sender 快速转封装失败: "
                    f"{stderr.decode('utf-8', errors='replace').strip()[:200]}"
                )
                return None
            return self._track_temp(output)
        except Exception as e:
            await safe_unlink(output) if output.exists() else None
            logger.warning(f"Sender 快速转封装异常: {e}")
            return None

    async def _transcode_to_h264(self, input_path: Path, encoder: str) -> Path | None:
        """将 hevc/av1/vp9 转码到 h264，使用硬件加速或 libx264 回退"""
        output = input_path.with_name(f"{input_path.stem}_transcode.mp4")
        mode = (
            getattr(self.cfg, "video_compress_quality_mode", "balance") or "balance"
        ).lower()
        if mode not in QUALITY_PRESETS:
            mode = "balance"
        qp = QUALITY_PRESETS[mode].get(encoder, QUALITY_PRESETS[mode]["libx264"])

        cmd: list[str] = ["ffmpeg", "-y", "-i", str(input_path), "-c:v", encoder]
        if encoder == "libx264":
            cmd.extend(["-crf", str(qp), "-preset", "veryfast"])
        elif encoder == "h264_nvenc":
            cmd.extend(["-cq", str(qp), "-preset", "p2"])
        elif encoder == "h264_qsv":
            cmd.extend(["-global_quality", str(qp), "-preset", "veryfast"])
        elif encoder == "h264_amf":
            cmd.extend(["-quality", "speed"])
        else:
            cmd.extend(["-crf", str(qp), "-preset", "veryfast"])
        cmd.extend(["-c:a", "aac", "-b:a", "128k"])
        cmd.append(str(output))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0 or not output.exists() or output.stat().st_size == 0:
                if output.exists():
                    await safe_unlink(output)
                logger.warning(
                    f"Sender 转码失败: "
                    f"{stderr.decode('utf-8', errors='replace').strip()[:200]}"
                )
                return None
            return self._track_temp(output)
        except Exception as e:
            if output.exists():
                await safe_unlink(output)
            logger.warning(f"Sender 转码异常: {e}")
            return None

    async def _send_video_optimized(self, path: Path) -> Path:
        """根据视频格式探测结果决定发送方式，返回实际要发送的文件路径

        分类：
        - A 最优：mp4 + h264 + aac + faststart → 直接发送，跳过 ffmpeg
        - B 转封装：h264 + aac 但容器/faststart 不符合 → ffmpeg -c copy +faststart
        - C 转码：明确检测到 hevc/av1/vp9 → 转码为 h264
        - D 回退：其它（未知编码、ffprobe 失败等）→ 直接发送原文件

        注意：压缩已在下载阶段的 download_av_and_merge 完成后执行一次，
        发送阶段不再重复压缩，避免耗时翻倍。
        """
        if not getattr(self.cfg, "video_send_fast_optimization", True):
            return path

        if shutil.which("ffprobe") is None:
            logger.info("Sender ffprobe 不可用，直接发送原文件")
            return path

        info = await self._probe_video_format(path)
        if not info or not isinstance(info, dict):
            logger.info("Sender 无法识别视频格式，直接发送原文件")
            return path

        fmt = info.get("format") or {}
        streams = info.get("streams") or []
        container = (fmt.get("format_name") or "").lower()

        video_codec: str | None = None
        audio_codec: str | None = None
        for s in streams:
            if not isinstance(s, dict):
                continue
            ctype = s.get("codec_type")
            cname = (s.get("codec_name") or "").lower()
            if ctype == "video" and video_codec is None and cname:
                video_codec = cname
            elif ctype == "audio" and audio_codec is None and cname:
                audio_codec = cname

        mp4_tags = ("mp4", "mov", "m4a", "3gp", "3g2", "mj2")
        is_mp4_like = any(tag in container for tag in mp4_tags)
        is_faststart = self._has_faststart(path) if is_mp4_like else False

        logger.info(
            f"Sender 视频格式检测: 容器={container or 'N/A'}, 视频={video_codec or 'N/A'}, "
            f"音频={audio_codec or 'N/A'}, faststart={is_faststart}"
        )

        # A：完全符合，直发
        if (
            video_codec == "h264"
            and audio_codec == "aac"
            and is_mp4_like
            and is_faststart
        ):
            logger.info("Sender 视频格式已支持，直接发送跳过 ffmpeg")
            return path

        # B：仅需转封装
        if video_codec == "h264" and audio_codec == "aac":
            logger.info("Sender 视频格式需转封装，使用快速模式")
            remuxed = await self._fast_remux(path)
            if remuxed is not None:
                return remuxed
            logger.info("Sender 快速转封装失败，直接发送原文件")
            return path

        # C：明确非兼容编码 → 转码
        if video_codec in ("hevc", "av1", "vp9"):
            encoder = self._select_video_encoder()
            logger.info(
                f"Sender 检测到 H.265 或 AV1 或 VP9 编码，转码为 H.264（硬件加速: {encoder}）"
            )
            transcoded = await self._transcode_to_h264(path, encoder)
            if transcoded is not None:
                return transcoded
            logger.info("Sender 转码失败，直接发送原文件")
            return path

        # D：未知/字段缺失
        logger.info("Sender 无法识别视频格式，直接发送原文件")
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

        import time as _t

        render_start = _t.time()
        image_path = await self.renderer.render_card(result, cfg=self.cfg)
        self._render_elapsed += _t.time() - render_start
        if image_path:
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
            import time as _t

            render_start = _t.time()
            image_path = await self.renderer.render_card(result, cfg=self.cfg)
            self._render_elapsed += _t.time() - render_start
            if image_path:
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
                case VideoContent() as v:
                    # 封面图复用：直接使用解析阶段已下载的封面，不从视频中重新提取
                    if v.cover:
                        logger.info("Sender 封面图复用: 已使用解析阶段封面")
                    optimized_path = await self._send_video_optimized(path)
                    segs.append(self._video_from_path(optimized_path))
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

        # 重置本次任务的临时统计
        self._render_elapsed = 0.0

        # 累积本次任务的下载/渲染统计
        download_ok = False
        download_size = 0
        download_elapsed = 0.0
        render_ok = False
        render_elapsed = 0.0
        import time as _time
        download_start = _time.time()

        sent = False
        for idx, group in enumerate(groups):
            tip = parser_tip if idx == 0 else None
            text = parse_text_segments if idx == 0 else None
            ret = await self._send_group(
                event,
                result,
                group,
                parser_tip=tip,
                parse_text_segments=text,
                merge_quote_id=merge_quote_id,
            )
            if isinstance(ret, tuple) and len(ret) == 4:
                group_dl_ok, group_size, group_dl_elapsed, group_render_ok = ret
            else:
                group_dl_ok = bool(ret)
                group_size = 0
                group_dl_elapsed = 0.0
                group_render_ok = False
            if group_dl_ok:
                download_ok = True
                download_size += group_size
                download_elapsed += group_dl_elapsed
            if group_render_ok:
                render_ok = True
            sent = sent or group_dl_ok or group_render_ok
        render_elapsed = self._render_elapsed

        # 兜底：若分组未捕获到下载大小，遍历 video_contents 累加实际文件大小
        if download_size <= 0:
            for v in getattr(result, "video_contents", []):
                try:
                    p = await v.get_path()
                    if p and p.exists():
                        download_size += p.stat().st_size
                        download_ok = True
                except Exception:
                    pass

        if download_elapsed <= 0:
            download_elapsed = _time.time() - download_start

        self.last_download_stats = {
            "ok": download_ok,
            "size": download_size,
            "elapsed": download_elapsed,
        }
        self.last_render_stats = {"ok": render_ok}

        # 主流程已单独发送过解析文本，等价于已发送内容
        if parse_text_already_sent:
            self.cfg.verbose("主流程已单独发送解析文本，兜底文本跳过")
            sent = True

        if not sent:
            segs = self._build_text_fallback(result)
            if not segs:
                logger.warning("发送结果为空，不执行发送")
                await self._cleanup_temp_videos()
                return

            try:
                await self.sleep_interval()
                await event.send(event.chain_result(segs))
                self.cfg.verbose("已发送兜底文本")
            except Exception as e:
                seg_meta = self._collect_seg_meta(segs)
                logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            await self._cleanup_temp_videos()
            return

        await self._cleanup_temp_videos()
