"""视频压缩模块

根据硬件检测结果与用户配置，动态生成 ffmpeg 压缩命令。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .hw_detect import HardwareInfo
from .utils import safe_unlink


# 编码器映射表：每种编码器的画质参数名、预设参数名、可用预设值
ENCODER_CONFIG: dict[str, dict[str, Any]] = {
    "libx264": {
        "codec": "libx264",
        "quality_param": "-crf",
        "preset_param": "-preset",
        "presets": [
            "ultrafast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "slower",
            "veryslow",
        ],
        "quality_range": (0, 51),
        "default_preset": "medium",
    },
    "h264_nvenc": {
        "codec": "h264_nvenc",
        "quality_param": "-cq",
        "preset_param": "-preset",
        "presets": [
            "p1",  # fastest
            "p2",
            "p3",
            "p4",
            "p5",
            "p6",
            "p7",  # slowest
        ],
        "quality_range": (0, 51),
        "default_preset": "p4",
    },
    "h264_qsv": {
        "codec": "h264_qsv",
        "quality_param": "-global_quality",
        "preset_param": "-preset",
        "presets": ["veryfast", "faster", "fast", "medium", "slow", "slower"],
        "quality_range": (0, 51),
        "default_preset": "medium",
    },
    "h264_amf": {
        "codec": "h264_amf",
        "quality_param": "-quality",
        "preset_param": "-quality",
        "presets": ["speed", "balanced", "quality"],
        "quality_range": (0, 51),
        "default_preset": "balanced",
    },
    "h264_mediacodec": {
        "codec": "h264_mediacodec",
        "quality_param": "-b:v",
        "preset_param": None,
        "presets": [],
        "quality_range": None,  # 使用码率字符串
        "default_preset": None,
    },
}

# 品质模式 -> 各编码器默认值
# libx264/h264_nvenc/h264_qsv/h264_amf 使用 0-51 的品质值（越小越好）
# h264_mediacodec 使用目标视频码率（如 4M、2M）
QUALITY_PRESETS: dict[str, dict[str, int | str]] = {
    "quality": {
        "libx264": 18,
        "h264_nvenc": 20,
        "h264_qsv": 23,
        "h264_amf": 0,
        "h264_mediacodec": "4M",
    },
    "balance": {
        "libx264": 23,
        "h264_nvenc": 25,
        "h264_qsv": 28,
        "h264_amf": 25,
        "h264_mediacodec": "2M",
    },
    "speed": {
        "libx264": 30,
        "h264_nvenc": 32,
        "h264_qsv": 35,
        "h264_amf": 51,
        "h264_mediacodec": "1M",
    },
}

# 用户配置选项 -> 实际编码器名称
ENCODER_NAME_MAP: dict[str, str] = {
    "cpu": "libx264",
    "nvenc": "h264_nvenc",
    "qsv": "h264_qsv",
    "amf": "h264_amf",
    "mediacodec": "h264_mediacodec",
    "auto": "auto",
}

# 预设值兼容性映射（用户通用预设 -> 各编码器对应预设）
PRESET_MAP: dict[str, dict[str, str | None]] = {
    "ultrafast": {
        "libx264": "ultrafast",
        "h264_nvenc": "p1",
        "h264_qsv": "veryfast",
        "h264_amf": "speed",
        "h264_mediacodec": None,
    },
    "veryfast": {
        "libx264": "veryfast",
        "h264_nvenc": "p2",
        "h264_qsv": "veryfast",
        "h264_amf": "speed",
        "h264_mediacodec": None,
    },
    "faster": {
        "libx264": "faster",
        "h264_nvenc": "p3",
        "h264_qsv": "faster",
        "h264_amf": "speed",
        "h264_mediacodec": None,
    },
    "fast": {
        "libx264": "fast",
        "h264_nvenc": "p4",
        "h264_qsv": "fast",
        "h264_amf": "balanced",
        "h264_mediacodec": None,
    },
    "medium": {
        "libx264": "medium",
        "h264_nvenc": "p4",
        "h264_qsv": "medium",
        "h264_amf": "balanced",
        "h264_mediacodec": None,
    },
    "slow": {
        "libx264": "slow",
        "h264_nvenc": "p5",
        "h264_qsv": "slow",
        "h264_amf": "quality",
        "h264_mediacodec": None,
    },
    "slower": {
        "libx264": "slower",
        "h264_nvenc": "p6",
        "h264_qsv": "slower",
        "h264_amf": "quality",
        "h264_mediacodec": None,
    },
    "veryslow": {
        "libx264": "veryslow",
        "h264_nvenc": "p7",
        "h264_qsv": "slower",
        "h264_amf": "quality",
        "h264_mediacodec": None,
    },
}

# 快捷分辨率映射
RESOLUTION_MAP: dict[str, str | None] = {
    "original": None,
    "720p": "1280:720",
    "540p": "960:540",
    "360p": "640:360",
}


class VideoCompressor:
    """视频压缩器"""

    def __init__(self, hw_info: HardwareInfo, cfg: Any):
        """
        Args:
            hw_info: 硬件检测结果
            cfg: PluginConfig 或 EffectiveConfig 实例
        """
        self.hw = hw_info
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "video_compress_enable", False))

    def _resolve_encoder(self) -> str:
        """根据用户选择确定实际 ffmpeg 编码器，不可用时回退到 CPU"""
        user_choice = (getattr(self.cfg, "video_compress_encoder", "auto") or "auto").lower()
        encoder = ENCODER_NAME_MAP.get(user_choice, "libx264")

        if encoder == "auto":
            encoder = self.hw.recommended_encoder
            logger.info(f"[VideoCompress] 用户选择自动检测，推荐编码器: {encoder}")
            return encoder

        if not self.hw.has_encoder(encoder):
            reason = self._encoder_unavailable_reason(encoder)
            logger.error(
                f"[VideoCompress] 用户选择的编码器 {encoder} 不可用: {reason}"
            )
            logger.error(
                f"[VideoCompress] 检测结果: available_encoders={self.hw.available_encoders}, "
                f"gpu={self.hw.gpu_models}, dri={self.hw.dri_devices}, vaapi={self.hw.vaapi_drivers}"
            )
            fallback = self.hw.recommended_encoder
            logger.error(f"[VideoCompress] 已自动回退到 {fallback}")
            return fallback

        return encoder

    def _encoder_unavailable_reason(self, encoder: str) -> str:
        """返回编码器不可用的原因"""
        if not shutil.which("ffmpeg"):
            return "未安装 ffmpeg"
        if encoder == "h264_nvenc" and not any("NVIDIA" in g.upper() for g in self.hw.gpu_models):
            return "未检测到 NVIDIA 显卡"
        if encoder == "h264_qsv" and not any(
            intel in g.upper() for g in self.hw.gpu_models for intel in ("INTEL", "ARC", "UHD", "IRIS")
        ):
            return "未检测到 Intel 显卡"
        if encoder == "h264_amf" and not any(amd in g.upper() for g in self.hw.gpu_models for amd in ("AMD", "RADEON")):
            return "未检测到 AMD 显卡"
        if encoder == "h264_mediacodec" and not self.hw.is_mobile:
            return "当前不是手机/Termux/Android 环境"
        return "ffmpeg 未启用该编码器"

    def _get_quality_value(self, encoder: str) -> int | str:
        """获取当前品质模式下对应编码器的值"""
        mode = (getattr(self.cfg, "video_compress_quality_mode", "balance") or "balance").lower()
        if mode not in QUALITY_PRESETS:
            mode = "balance"
        return QUALITY_PRESETS[mode].get(encoder, 23)

    def _build_cmd(self, input_path: Path, output_path: Path) -> list[str]:
        """构建 ffmpeg 压缩命令"""
        encoder = self._resolve_encoder()
        config = ENCODER_CONFIG[encoder]
        mode = (getattr(self.cfg, "video_compress_quality_mode", "balance") or "balance").lower()

        cmd: list[str] = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-c:v",
            config["codec"],
        ]

        # 画质/码率参数
        quality_value = self._get_quality_value(encoder)
        if mode == "custom" and encoder == "h264_mediacodec":
            # 自定义模式下允许用户指定码率
            custom_bitrate = (getattr(self.cfg, "video_compress_custom_bitrate", "") or "").strip()
            if custom_bitrate:
                quality_value = custom_bitrate
        cmd.extend([config["quality_param"], str(quality_value)])

        # 预设参数
        if config["preset_param"]:
            preset = self._resolve_preset(encoder, mode)
            if preset:
                cmd.extend([config["preset_param"], preset])

        # 自定义模式高级参数
        if mode == "custom":
            # 分辨率缩放
            resolution = (getattr(self.cfg, "video_compress_custom_resolution", "original") or "original").strip().lower()
            scale = RESOLUTION_MAP.get(resolution)
            if scale is None and "x" in resolution:
                # 用户手动输入 1920x1080 格式
                width, _, height = resolution.partition("x")
                if width.isdigit() and height.isdigit():
                    scale = f"{width}:{height}"
            if scale:
                cmd.extend(["-vf", f"scale={scale}"])

            # 帧率
            fps = (getattr(self.cfg, "video_compress_custom_fps", "original") or "original").strip().lower()
            if fps != "original" and fps.replace("fps", "").isdigit():
                cmd.extend(["-r", fps.replace("fps", "")])

            # 编码线程数（仅 CPU 编码时生效）
            threads = getattr(self.cfg, "video_compress_custom_threads", 0) or 0
            if encoder == "libx264" and threads > 0:
                cmd.extend(["-threads", str(threads)])

        # 音频参数
        audio_bitrate = (getattr(self.cfg, "video_compress_custom_audio_bitrate", "128k") or "128k").strip()
        if mode != "custom":
            # 非自定义模式也使用音频码率配置，默认 128k
            audio_bitrate = "128k"
        cmd.extend([
            "-c:a", "aac",
            "-b:a", audio_bitrate,
        ])

        # 移动设备额外限制：限制 profile 为 baseline 提高兼容性
        if self.hw.is_mobile and encoder == "h264_mediacodec":
            cmd.extend(["-profile:v", "baseline", "-level", "3.1"])

        cmd.append(str(output_path))
        return cmd

    def _resolve_preset(self, encoder: str, mode: str) -> str | None:
        """确定编码预设值"""
        config = ENCODER_CONFIG[encoder]
        if not config["preset_param"]:
            return None

        if mode == "custom":
            user_preset = (getattr(self.cfg, "video_compress_custom_preset", "medium") or "medium").strip().lower()
            mapped = PRESET_MAP.get(user_preset, {}).get(encoder)
            if mapped:
                return mapped
            return config["default_preset"]

        # 非自定义模式根据品质模式选择默认预设
        mode_presets: dict[str, dict[str, str | None]] = {
            "quality": {
                "libx264": "slow",
                "h264_nvenc": "p5",
                "h264_qsv": "slower",
                "h264_amf": "quality",
            },
            "balance": {
                "libx264": "medium",
                "h264_nvenc": "p4",
                "h264_qsv": "medium",
                "h264_amf": "balanced",
            },
            "speed": {
                "libx264": "veryfast",
                "h264_nvenc": "p2",
                "h264_qsv": "veryfast",
                "h264_amf": "speed",
            },
        }
        return mode_presets.get(mode, {}).get(encoder, config["default_preset"])

    async def compress(self, input_path: Path, platform: str | None = None) -> Path:
        """压缩单个视频文件，返回输出路径"""
        if not self.enabled:
            return input_path

        if not input_path.exists():
            raise FileNotFoundError(f"待压缩文件不存在: {input_path}")

        output_path = input_path.with_name(f"{input_path.stem}_compressed{input_path.suffix}")
        if output_path.exists():
            return output_path

        cmd = self._build_cmd(input_path, output_path)
        logger.info(f"[VideoCompress] 开始压缩 | input={input_path.name} | cmd={' '.join(cmd)}")
        start = time.time()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.cfg.download_timeout * 2
            )
        except asyncio.TimeoutError:
            await safe_unlink(output_path)
            raise RuntimeError("视频压缩超时")
        except FileNotFoundError:
            raise RuntimeError("ffmpeg 未安装或无法找到可执行文件")

        if proc.returncode != 0:
            await safe_unlink(output_path)
            err = stderr.decode("utf-8", errors="ignore").strip()[-500:]
            raise RuntimeError(f"视频压缩失败: {err}")

        elapsed = time.time() - start
        in_size = input_path.stat().st_size
        out_size = output_path.stat().st_size
        ratio = (1 - out_size / in_size) * 100 if in_size > 0 else 0
        if ratio >= 0:
            save_text = f"节省 {ratio:.0f}%"
        else:
            save_text = f"增大 {-ratio:.0f}%"
        logger.info(
            f"[VideoCompress] 视频压缩完成: 原大小 {in_size / 1024 / 1024:.1f}MB → "
            f"压缩后 {out_size / 1024 / 1024:.1f}MB ({save_text}) | "
            f"input={input_path.name} | elapsed={elapsed:.2f}s"
        )
        return output_path

    def get_recommended_config(self) -> dict[str, Any]:
        """返回根据硬件检测得到的推荐配置"""
        return {
            "video_compress_enable": not self.hw.is_mobile,
            "video_compress_quality_mode": self.hw.recommended_quality_mode,
            "video_compress_encoder": self._encoder_to_option(self.hw.recommended_encoder),
        }

    @staticmethod
    def _encoder_to_option(encoder: str) -> str:
        for opt, enc in ENCODER_NAME_MAP.items():
            if enc == encoder:
                return opt
        return "cpu"
