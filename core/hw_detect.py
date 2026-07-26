"""硬件检测模块

插件启动时运行一次，检测 CPU/GPU/可用 ffmpeg 编码器等信息，
结果仅输出到日志，不发送到聊天窗口。
"""

from __future__ import annotations

import asyncio
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger


@dataclass
class HardwareInfo:
    """硬件信息聚合"""

    cpu_model: str = ""
    cpu_cores: int = 0
    avx_supported: bool = False
    avx2_supported: bool = False
    gpu_models: list[str] = field(default_factory=list)
    dri_devices: list[str] = field(default_factory=list)
    vaapi_drivers: list[str] = field(default_factory=list)
    available_encoders: list[str] = field(default_factory=list)
    is_mobile: bool = False
    platform_system: str = ""
    os_name: str = ""
    kernel_version: str = ""
    detection_errors: list[str] = field(default_factory=list)

    def has_encoder(self, encoder: str) -> bool:
        return encoder in self.available_encoders

    @property
    def recommended_encoder(self) -> str:
        """根据可用编码器返回推荐编码方式"""
        if self.is_mobile:
            return "mediacodec"
        priority = ["h264_nvenc", "h264_qsv", "h264_amf"]
        for enc in priority:
            if self.has_encoder(enc):
                return enc
        return "libx264"

    @property
    def recommended_quality_mode(self) -> str:
        """根据硬件性能返回推荐品质模式"""
        if self.is_mobile:
            return "speed"
        if self.recommended_encoder != "libx264":
            return "quality"
        if self.cpu_cores >= 8 and self.avx2_supported:
            return "balance"
        return "speed"


class HardwareDetector:
    """异步硬件检测器"""

    def __init__(self):
        self.info = HardwareInfo(platform_system=platform.system())

    async def detect(self) -> HardwareInfo:
        """执行完整硬件检测"""
        await self._detect_os()
        await asyncio.gather(
            self._detect_cpu(),
            self._detect_gpu(),
            self._detect_dri(),
            self._detect_vaapi(),
            self._detect_mobile(),
        )
        await self._detect_ffmpeg_encoders()
        return self.info

    async def _detect_os(self) -> None:
        """检测操作系统名称与内核版本"""
        self.info.kernel_version = platform.release()
        sys = self.info.platform_system

        # Linux 优先读取 /etc/os-release
        os_release = Path("/etc/os-release")
        if os_release.exists():
            try:
                content = os_release.read_text(encoding="utf-8", errors="ignore")
                pretty_name = ""
                name = ""
                version_id = ""
                for line in content.splitlines():
                    if line.startswith("PRETTY_NAME="):
                        pretty_name = line.split("=", 1)[1].strip().strip('"')
                    elif line.startswith("NAME="):
                        name = line.split("=", 1)[1].strip().strip('"')
                    elif line.startswith("VERSION_ID="):
                        version_id = line.split("=", 1)[1].strip().strip('"')

                if pretty_name:
                    self.info.os_name = pretty_name
                elif name and version_id:
                    self.info.os_name = f"{name} {version_id}"
                elif name:
                    self.info.os_name = name
            except Exception as e:
                self.info.detection_errors.append(f"OS 检测失败: {e}")

        # /etc/os-release 不存在或为 Linux 但解析失败，尝试 lsb_release
        if not self.info.os_name and sys == "Linux":
            rc, out, _ = await self._run_cmd(["lsb_release", "-ds"])
            if rc == 0 and out.strip():
                self.info.os_name = out.strip().strip('"')

        # Windows / macOS 回退
        if not self.info.os_name and sys == "Windows":
            self.info.os_name = platform.platform()
        elif not self.info.os_name and sys == "Darwin":
            try:
                ver, _, _ = platform.mac_ver()
                self.info.os_name = f"macOS {ver}" if ver else platform.platform()
            except Exception:
                self.info.os_name = platform.platform()

        # 最终兜底
        if not self.info.os_name:
            self.info.os_name = platform.platform()

    async def _run_cmd(self, cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
        """异步执行命令，返回 (returncode, stdout, stderr)"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode or 0, stdout.decode("utf-8", errors="ignore"), stderr.decode("utf-8", errors="ignore")
        except asyncio.TimeoutError:
            return -1, "", "timeout"
        except FileNotFoundError:
            return -2, "", "command not found"
        except Exception as e:
            return -3, "", str(e)

    async def _detect_cpu(self) -> None:
        """检测 CPU 型号、核心数、AVX 指令集"""
        self.info.cpu_cores = self._get_cpu_count()
        sys = self.info.platform_system

        try:
            if sys == "Windows":
                self.info.cpu_model = await self._windows_cpu_model()
                self.info.avx_supported = await self._windows_has_avx()
                self.info.avx2_supported = await self._windows_has_avx2()
            elif sys == "Linux":
                self.info.cpu_model = self._linux_cpu_model()
                flags = self._linux_cpu_flags()
                self.info.avx_supported = "avx" in flags
                self.info.avx2_supported = "avx2" in flags
            elif sys == "Darwin":
                self.info.cpu_model = await self._macos_cpu_model()
                self.info.avx_supported = True  # 近十年 Mac 均支持 AVX
                self.info.avx2_supported = True
            else:
                self.info.cpu_model = platform.processor() or "未知"
        except Exception as e:
            self.info.detection_errors.append(f"CPU 检测失败: {e}")
            self.info.cpu_model = platform.processor() or "未知"

    def _get_cpu_count(self) -> int:
        try:
            return len(os.sched_getaffinity(0))  # type: ignore
        except Exception:
            return multiprocessing.cpu_count()  # type: ignore

    async def _windows_cpu_model(self) -> str:
        _, out, _ = await self._run_cmd(["wmic", "cpu", "get", "Name", "/value"])
        for line in out.splitlines():
            if line.strip().startswith("Name="):
                return line.split("=", 1)[1].strip()
        return "未知"

    async def _windows_has_avx(self) -> bool:
        # 通过 PowerShell 的 CPU 功能字符串判断
        _, out, _ = await self._run_cmd(
            [
                "powershell",
                "-Command",
                "(Get-CimInstance Win32_Processor).Caption",
            ]
        )
        # Caption 不含指令集信息，改用 systeminfo 中的 Hyper-V 要求
        _, out2, _ = await self._run_cmd(["systeminfo"])
        return "AVX" in out2.upper() or "SSE4" in out2.upper() or "AES" in out2.upper()

    async def _windows_has_avx2(self) -> bool:
        _, out2, _ = await self._run_cmd(["systeminfo"])
        return "AVX2" in out2.upper()

    def _linux_cpu_model(self) -> str:
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return "未知"

    def _linux_cpu_flags(self) -> set[str]:
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("flags"):
                        return set(line.split(":", 1)[1].strip().split())
        except Exception:
            pass
        return set()

    async def _macos_cpu_model(self) -> str:
        _, out, _ = await self._run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"])
        return out.strip() or "Apple Silicon"

    async def _detect_gpu(self) -> None:
        """检测显卡型号"""
        sys = self.info.platform_system
        gpus: list[str] = []
        try:
            if sys == "Windows":
                _, out, _ = await self._run_cmd(
                    ["wmic", "path", "win32_VideoController", "get", "Name", "/value"]
                )
                for line in out.splitlines():
                    if line.strip().startswith("Name="):
                        name = line.split("=", 1)[1].strip()
                        if name:
                            gpus.append(name)
            elif sys == "Linux":
                # 优先尝试 lspci
                _, out, _ = await self._run_cmd(["lspci"])
                for line in out.splitlines():
                    if "VGA" in line or "3D" in line or "Display" in line:
                        name = line.split(":", 2)[-1].strip()
                        if name:
                            gpus.append(name)
                # 备用 /sys/class/drm
                if not gpus:
                    gpus = self._linux_drm_gpus()
            elif sys == "Darwin":
                _, out, _ = await self._run_cmd(
                    ["system_profiler", "SPDisplaysDataType"]
                )
                for line in out.splitlines():
                    if "Chipset Model" in line:
                        gpus.append(line.split(":", 1)[1].strip())
        except Exception as e:
            self.info.detection_errors.append(f"GPU 检测失败: {e}")
        self.info.gpu_models = gpus

    def _linux_drm_gpus(self) -> list[str]:
        gpus: list[str] = []
        try:
            drm_path = Path("/sys/class/drm")
            if not drm_path.exists():
                return gpus
            for entry in drm_path.iterdir():
                device = entry / "device"
                if not device.exists():
                    continue
                vendor = (device / "vendor").read_text(encoding="utf-8", errors="ignore").strip() if (device / "vendor").exists() else ""
                model = (device / "product").read_text(encoding="utf-8", errors="ignore").strip() if (device / "product").exists() else ""
                label = f"{vendor}:{model}".strip(":")
                if label:
                    gpus.append(label)
        except Exception:
            pass
        return gpus

    async def _detect_dri(self) -> None:
        """检测 /dev/dri 设备（仅 Linux）"""
        if self.info.platform_system != "Linux":
            return
        try:
            dri_path = Path("/dev/dri")
            if dri_path.exists():
                self.info.dri_devices = [
                    str(p) for p in dri_path.iterdir() if p.is_char_device() or p.is_block_device() or p.is_fifo()
                ]
        except Exception as e:
            self.info.detection_errors.append(f"/dev/dri 检测失败: {e}")

    async def _detect_vaapi(self) -> None:
        """检测 VA-API 驱动状态"""
        _, out, err = await self._run_cmd(["vainfo"], timeout=10)
        text = out + err
        if "VA-API version" in text:
            # 提取驱动名称
            for line in text.splitlines():
                if "Driver version" in line or "vainfo: VA-API" in line:
                    self.info.vaapi_drivers.append(line.strip())
        elif "libva" in text.lower():
            self.info.vaapi_drivers.append("VA-API 库存在但无法初始化")

    async def _detect_mobile(self) -> None:
        """判断是否为手机/Termux/Android 环境"""
        sys = self.info.platform_system
        if sys == "Android":
            self.info.is_mobile = True
            return
        # 检测 Termux
        if Path("/data/data/com.termux").exists() or shutil.which("termux-setup-storage"):
            self.info.is_mobile = True
            return
        # 检测 Linux ARM 且屏幕分辨率/电池等特征（简单判断）
        if sys == "Linux" and platform.machine().lower() in ("aarch64", "arm64", "armv7l"):
            self.info.is_mobile = True

    async def _detect_ffmpeg_encoders(self) -> None:
        """检测 ffmpeg 可用编码器"""
        if not shutil.which("ffmpeg"):
            self.info.detection_errors.append("未检测到 ffmpeg，视频压缩功能将不可用")
            return
        _, out, err = await self._run_cmd(["ffmpeg", "-hide_banner", "-encoders"])
        text = out + err
        # ffmpeg -encoders 输出格式：前缀 1 位类型 + 5 位能力标志（如 V....D、V.....），再跟空格和编码器名
        targets = {
            "libx264": r"V[\.\w]{5}\s+libx264",
            "h264_nvenc": r"V[\.\w]{5}\s+h264_nvenc",
            "h264_qsv": r"V[\.\w]{5}\s+h264_qsv",
            "h264_amf": r"V[\.\w]{5}\s+h264_amf",
            "h264_mediacodec": r"V[\.\w]{5}\s+h264_mediacodec",
        }
        for encoder, pattern in targets.items():
            if re.search(pattern, text):
                self.info.available_encoders.append(encoder)

    def log_summary(self) -> None:
        """将检测结果输出到日志"""
        lines = [
            "[HWDetect] ====== 硬件检测结果 ======",
            f"[HWDetect] 操作系统: {self.info.os_name}",
            f"[HWDetect] 内核版本: {self.info.kernel_version}",
            f"[HWDetect] 平台: {self.info.platform_system}",
            f"[HWDetect] CPU: {self.info.cpu_model}",
            f"[HWDetect] CPU 核心数: {self.info.cpu_cores}",
            f"[HWDetect] AVX/AVX2: {self.info.avx_supported}/{self.info.avx2_supported}",
            f"[HWDetect] GPU: {', '.join(self.info.gpu_models) if self.info.gpu_models else '未检测到'}",
            f"[HWDetect] /dev/dri: {', '.join(self.info.dri_devices) if self.info.dri_devices else '无'}",
            f"[HWDetect] VA-API: {', '.join(self.info.vaapi_drivers) if self.info.vaapi_drivers else '未检测/不可用'}",
            f"[HWDetect] 可用编码器: {', '.join(self.info.available_encoders) if self.info.available_encoders else '无'}",
            f"[HWDetect] 推荐编码器: {self.info.recommended_encoder}",
            f"[HWDetect] 推荐品质模式: {self.info.recommended_quality_mode}",
            f"[HWDetect] 手机端环境: {self.info.is_mobile}",
        ]
        for line in lines:
            logger.info(line)
        for err in self.info.detection_errors:
            logger.error(f"[HWDetect] {err}")


# 避免循环导入，在方法内导入
import multiprocessing  # noqa: E402
import os  # noqa: E402
