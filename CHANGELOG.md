# 更新日志

## v1.6.3

### 新增

- **视频发送快速优化**（`core/sender.py`）
  - 发送前通过 `ffprobe` 探测视频格式（容器、视频/音频编码、faststart），按四档分类处理：
    - A 直发：`mp4 + h264 + aac + faststart` → 跳过 ffmpeg 直接发送
    - B 快速转封装：`h264 + aac` 但容器或 faststart 不符合 → `ffmpeg -c copy -movflags +faststart` 0.2-0.5s 即可完成
    - C 转码：明确检测到 `hevc / av1 / vp9` → 转码为 `h264`，复用 `compress.py` 中的硬件检测推荐编码器（`h264_nvenc` / `h264_qsv` / `h264_amf`），不可用时回退 `libx264`
    - D 回退：未知编码、`ffprobe` 不可用、字段缺失 → 回退到原有 `_maybe_compress`，避免误判触发转码
  - 封面图直接复用 `VideoContent.cover`（解析阶段已下载的文件），不再用 ffmpeg 从视频中重新抽帧
  - 转封装 / 转码产生的临时文件（`*_remux.mp4`、`*_transcode.mp4`）在 `send_parse_result` 完成后统一通过 `safe_unlink` 清理
  - 新增配置开关 `video_send_fast_optimization`（默认 true），关闭后完全回退原有逻辑

### 备注

- 所有 `ffmpeg` / `ffprobe` 调用均使用 `asyncio.create_subprocess_exec` 异步执行，不阻塞事件循环
- 转码参数沿用 `compress.py` 的 `QUALITY_PRESETS` 映射，与现有视频压缩体验一致
- 转码/转封装完成后会校验输出文件大小，异常时自动回退到 `_maybe_compress`

## v1.6.2

### 新增

- **解析阶段重试**（`core/parsers/base.py`）
  - `parse()` 入口统一包装 `_invoke_handler_with_retry`，新增 `request_with_retry` 协程供子类调用
  - 默认重试 3 次，最大不超过 5 次；间隔固定档位：普通错误 0.2s / 0.5s / 1.0s，5xx 服务器错误 2s / 4s / 6s
  - B 站凭证过期（`-101` / SESSDATA / 登录相关关键字）跳过重试直接抛 `ParseException` 并打印 `[Parser] B站凭证已过期，请重新执行 blogin 登录`
  - 新增 `parse_retry_enabled` / `parse_retry_immediate` / `parse_retry_count` 三个配置

- **卡片渲染容错与降级**（`core/render.py`）
  - 卡片渲染循环对单张图片失败仅记录 WARNING 后跳过，不影响整体卡片
  - 新增 `_safe_download_image` 提供封面图独立重试与日志：`[Render] 图片下载失败，0.5s 后重试 (1/2)` / `[Render] 图片下载成功（重试 1 次后）`
  - 新增 `card_render_disabled` 总开关：开启后所有卡片渲染被完全跳过，结果走纯文本发送
  - 封面图降级三种模式：`placeholder`（默认占位图）/ `skip`（跳过封面区域）/ `text_only`（放弃卡片降级纯文本）
  - 降级过程通过 `_cover_fallback` 实现，对应日志：`[Render] 封面图下载失败，使用默认占位图` 等

- **分块下载断点续传**（`core/download.py`）
  - 每个分块对应 `<filename>.part_<idx>.tmp` 临时文件，记录已下载字节数
  - 续传时根据临时文件大小生成 `Range: bytes=<start + downloaded>-` 头部继续下载
  - 已有完成的分块直接合并到目标文件并跳过
  - 服务器返回 200（不支持 Range）时自动回退到完整下载
  - 插件启动时调用 `cleanup_stale_range_temp_files` 删除超过 24 小时的临时文件
  - 新增 `range_download_resume_enabled` 配置（默认 true）

- **任务汇总日志**（`main.py` / `core/sender.py`）
  - `MessageSender` 新增 `last_download_stats` 与 `last_render_stats` 暴露下载/渲染状态
  - 任务结束后输出 `[Task] 汇总: 解析成功 | 下载成功 (26.64MB) | 渲染卡片成功 | 下载视频时间 6.32s | 总耗时 12.38s`
  - 发送流程中插入 `正在发送...` / `发送成功，耗时 Xs 一共耗时 Ys` 提示

### 配置项

新增全局 + 群覆盖配置：

- `parse_retry_enabled`（bool，默认 true）
- `parse_retry_immediate`（bool，默认 false）
- `parse_retry_count`（int，默认 3，1-5）
- `card_render_disabled`（bool，默认 false）
- `card_render_retry_enabled`（bool，默认 true）
- `card_render_retry_count`（int，默认 2，1-5）
- `card_render_retry_delay`（float，默认 0.5，0.3-2）
- `card_render_fallback_mode`（enum，默认 placeholder）
- `card_placeholder_image`（string，默认 logo.png）
- `range_download_resume_enabled`（bool，默认 true）

所有重试次数受最大 5 次限制；配置变更仅对新任务生效；断点续传实时读取开关状态。

## v1.6.1

### 修复

- **CDN 测速空备选节点判断**（`core/download.py` `_download_single`）
  - 当 `cdn_fallback_urls` 为空时跳过限流测速，直接使用原节点下载，避免主节点误判为限流后抛出 `None` 相关问题
  - 主节点限流且无备选节点时，抛出明确的 `DownloadException`，错误信息包含节点与速度信息，便于排查

- **批量下载失败隔离**（`core/download.py` `download_batch`）
  - 单张图片下载失败时记录 URL、平台与异常详情到日志，继续处理后续图片
  - 增加批次进度日志与整体成功/失败汇总
  - 单张失败不再中断整个批量流程

## v1.6.0

### 新增：下载优化完整方案（全部默认关闭）

本次更新引入 8 项下载层优化，所有开关默认关闭，可在配置面板按需开启。

- **DNS 预解析**（`dns_prefetch`）
  - 新增 `core/dns_cache.py`
  - 插件启动时预解析所有已启用解析器对应平台的域名
  - 支持启动预解析、定时刷新、单域名 2 秒超时、整体 10 秒超时
  - 修改后需重启插件生效

- **CDN 节点优选**（`cdn_prefetch_enabled`）
  - 下载多个备选 URL 时，对每个 URL 下载 256KB 测速，选择最快节点
  - 测速数据不丢弃，作为第一个分块复用（实际当前实现先测速后正式下载）
  - 测速结果缓存 5 分钟
  - 仅对 B站（多 durl/dash 节点）和 YouTube（yt-dlp formats）生效
  - 仅文件 >= 10MB 时执行

- **分块并发下载**（`enable_range_download`）
  - 新增 HTTP Range 分块下载，压缩关闭时生效
  - 自动按文件大小分配分块数：10MB 以下 1 块、10-50MB 2 块、50-100MB 3 块、100-500MB 4 块、500MB-1GB 6 块、>1GB 8 块
  - 实际分块数取自动分配值与用户上限（含平台单独上限、内存监控降级）的较小值
  - 支持风控降级：403/416 回退单连接，同一平台连续失败 3 次禁用分块 10 分钟

- **分块下载内存自适应**（`range_memory_adaptive`）
  - 根据可用内存选择合并策略
  - 文件 < 可用内存 50%：内存合并
  - 文件 < 可用内存 80%：内存合并 + 自动降低分块数
  - 文件 >= 可用内存 80%：流式合并
  - 保留可用内存的 10% 不使用

- **流式压缩**（`enable_streaming_compress`）
  - 视频压缩开启时生效
  - 当前实现为：下载完成后立即使用 ffmpeg 进行文件级压缩（保留传统压缩回退）
  - 压缩参数根据品质模式动态生成

- **后台内存监控**（`memory_monitor`）
  - 新增 `core/memory_monitor.py`
  - 每 30 秒检测系统内存
  - 可用内存低于阈值时自动降低分块数上限
  - 每次解析完成后主动检查并恢复上限

- **日志 URL 脱敏**（`core/utils.py`）
  - 新增 `sanitize_url` 函数
  - 对 `mid`、`access_key`、`sign`、`buvid`、`uid`、`token`、`session_id`、`traceid` 替换为 `***`
  - 所有下载日志中的 URL 均经过脱敏

- **视频下载缓存**（`video_cache_enabled`）
  - 新增 `core/cache.py` 中的 `VideoCacheManager`
  - 缓存键：`hash(原始URL + 文件大小 + 分辨率 + 压缩品质模式 + CRF值)`
  - 支持 TTL 和 LRU 淘汰

### 其他修改

- `core/download.py` 重构：
  - 集成 CDN 优选、分块下载、视频缓存、压缩、URL 脱敏
  - 保留自适应下载并发
  - 新增智能阈值判断（<10MB 跳过优化，10-15MB 不测速可压缩，>=20MB 完全启用）
- `core/utils.py`：新增 `sanitize_url`、`merge_av_streaming`、`memory_info`
- `core/sender.py`：`_maybe_compress` 跳过已带 `_compressed` 后缀的文件，避免重复压缩
- `core/config.py`：添加所有新配置字段及群覆盖字段
- `_conf_schema.json`：新增全部配置面板项
- `requirements.txt`：新增 `psutil`
- `main.py`：初始化并启动 VideoCache、MemoryMonitor、DNSCacheManager

## v1.5.3

### 重构

- 重构 `core/hw_detect.py` 编码器检测逻辑，采用"硬件检测 + 编码器测试"双层验证：
  - 第一步：硬件检测
    - NVIDIA NVENC：检查 `/dev/nvidia*` 设备及 `nvidia-smi` 命令
    - Intel QSV：检查 `/dev/dri/renderD128` / `/dev/dri/card0` 及 `vainfo`
    - AMD AMF：检查 `/dev/dri` 设备 vendor `0x1002` 或 `lspci` 中的 AMD/Radeon/ATI 字样
    - MediaCodec：仅在 Android / Termux 环境启用
  - 第二步：编码器快速测试
    - 对通过硬件检测的候选编码器执行 `ffmpeg -f lavfi -i color=c=black:s=2x2:d=0.04 -c:v {encoder} -f null -`
    - 超时 5 秒，返回码 0 表示真正可用
    - 测试日志格式：`[HWDetect] 编码器测试: h264_qsv 可用`
  - `libx264` 作为 CPU 兜底，只要 ffmpeg 支持即保留，无需硬件检测
  - 最终 `available_encoders` 只包含同时通过硬件检测和编码器测试的编码器
- 新增 `HardwareInfo.encoder_hw_status` 与 `encoder_test_results` 字段
- `log_summary()` 新增输出：
  - `[HWDetect] 编码器硬件状态: ...`
  - `[HWDetect] 编码器测试结果: ...`
- 所有检测步骤均带错误捕获；ffmpeg 未安装时输出 ERROR 日志提示安装

## v1.5.2

### 修复

- 修复 `core/hw_detect.py` 中 ffmpeg 编码器检测正则错误：
  - 原正则为 `V\.\.\.\.\s+encoder`，只匹配 `V` 后面 4 个点；但 `ffmpeg -encoders` 实际输出前缀为 1 位类型 + 5 位能力标志（如 `V....D`、`V.....`），导致始终匹配不到编码器。
  - 修正为 `V[\.\w]{5}\s+encoder`，可正确识别 `libx264`、`h264_nvenc`、`h264_qsv`、`h264_amf`、`h264_mediacodec`。

## v1.5.1

### 修复

- 修复插件加载时 `bilibili_api` 初始化 `curl_cffi` 客户端失败的问题：
  - 将 `select_client("curl_cffi")` 与 `request_settings.set("impersonate", "chrome131")` 从模块顶层移至 `BilibiliParser.__init__` 中延迟执行
  - 模块导入阶段不再触发网络客户端初始化，避免 pip 依赖尚未就绪时即导入相关模块
  - 当 `curl_cffi` 不可用时，自动回退到 `aiohttp` 客户端并输出 WARN 日志；若两者均不可用则抛出异常并记录 ERROR 日志

## v1.5.0

### 新增

- 视频压缩功能（独立总开关 `video_compress_enable`，默认关闭）：
  - 四档品质模式：`quality`（画质优先）、`balance`（平衡）、`speed`（速度优先）、`custom`（自定义）
  - 五种编码方式：`auto`（自动检测推荐）、`cpu`（libx264）、`nvenc`（NVIDIA NVENC）、`qsv`（Intel QSV）、`amf`（AMD AMF）、`mediacodec`（手机端）
  - 插件启动时自动运行硬件检测（`core/hw_detect.py`），结果仅输出到日志：
    - CPU 型号、核心数、AVX/AVX2 支持情况
    - 显卡型号、/dev/dri 设备、VA-API 驱动状态
    - ffmpeg 可用编码器列表（libx264/h264_nvenc/h264_qsv/h264_amf/h264_mediacodec）
  - 编码器不可用时以 ERROR 级别输出原因与检测结果，并自动回退到 CPU 软件编码
  - 根据硬件自动推荐默认配置：高性能显卡默认画质优先+硬件编码；老旧 CPU 默认速度优先+CPU；手机端提示关闭或选择 mediacodec
- 自定义模式下开放高级参数：
  - 编码速度预设（preset，通用 8 档，自动映射到各编码器对应预设）
  - 分辨率缩放（original/720p/540p/360p 快捷选项，支持手动输入如 1920x1080）
  - 音频码率（32k/64k/96k/128k）
  - 帧率（original 或指定如 30fps）
  - CPU 编码线程数（0=自动）
  - MediaCodec 自定义视频码率
- 新增 `core/compress.py`：`VideoCompressor` 负责动态生成 ffmpeg 命令、执行压缩、失败回退；每种编码器均有独立的品质参数映射表
- 发送阶段（`core/sender.py`）对 `VideoContent` 自动调用压缩，失败时使用原视频

## v1.4.9

### 优化

- 群覆盖配置 `group_overrides` 中所有配置项已移除“留空使用全局配置/模板”类提示，仅保留行为说明
- 开启 `verbose_logging` 后，下载流程关键节点新增详细日志：
  - 开始下载：记录文件名、目标 CDN 节点（URL host）、当前时间
  - 节点切换（重定向/重试后的新 host）：记录当前是第几次尝试、节点域名
  - 重试：记录当前重试次数、等待秒数、错误类型与原因
  - 下载成功：记录总耗时（秒）、文件大小（MB）
  - 下载失败：记录失败类型（`connection_refused` / `timeout` / `other`）、当前节点、最终失败原因
- 新增 `Downloader._extract_host` 与 `_classify_download_error` 辅助方法，统一日志输出格式
- 流式下载（`streamd`）与 yt-dlp 下载（视频、宽松视频、音频）均已接入上述日志

## v1.4.8

### 新增

- 链接防抖触发策略 `link_debounce_strategy`：
  - `skip`（默认）：保持原行为，仅跳过解析并输出警告日志
  - `silent`：完全静默，不发送任何反应
  - `tip`：发送自定义提示文本 `link_debounce_tip_text`（默认“你已经发过一次{platform}链接啦~”），支持占位符 `{platform}` `{user_name}` `{user_id}`，并引用用户原链接消息
- 群覆盖配置 `group_overrides` 支持单独覆盖链接防抖策略与提示文本

### 优化

- 将原消息 ID 获取提前到链接防抖之前，使防抖 tip 与解析提示都能引用原消息
- 详细日志记录当前防抖策略与处理路径，方便开启 `verbose_logging` 后排查

## v1.4.7

### 修复

- 修复 B站等解析结果文本可能重复发送的问题：
  - `core/sender.py` 的 `_send_group` 现在会记录本组是否已发送解析提示、解析文本、预览卡片或媒体段中的任意一种；只要已发送任何内容，即视为本组发送成功，避免触发兜底文本造成重复
  - `core/main.py` 增加 `parse_text_already_sent` 标记；当「发送解析文本」开启且未合并到套娃时，主流程已单独发出解析文本后，`send_parse_result` 不再触发兜底文本
  - 该修复为通用逻辑，覆盖所有平台的解析结果（B站、抖音、快手、小红书、微博、视频号、YouTube、TikTok、Instagram、Iwara、知乎、NGA、网易云、AcFun 等）

### 优化

- 在关键发送节点补充详细日志（解析提示单独发送、解析文本单独发送、兜底文本跳过等），方便开启 `verbose_logging` 后排查重复发送问题

## v1.4.6

### 新增

- 自适应下载并发（性能优化分区）：
  - `perf_adaptive_download`：总开关，默认关闭
  - 每个解析器可单独设置 `download_concurrency`，未设置时使用 `perf_download_default_concurrency`
  - `perf_download_fail_threshold`：同一平台连续下载失败多少次后自动降级
  - `perf_download_degrade_step`：每次降级减少的并发数
  - `perf_download_min_concurrency`：并发数最低限制
  - `perf_download_recover_step`：成功后每次恢复的并发数
  - `perf_download_recover_interval`：两次并发调整的最小间隔，避免抖动
- 新增 `core/download.py` 的 `AdaptiveSemaphoreManager`：
  - 每个平台独立 asyncio.Semaphore
  - 连续失败达到阈值自动降低并发并输出警告日志
  - 下载成功且满足恢复间隔后逐步恢复并发
  - 已接入 `streamd`、`download_*`、`ytdlp_download_*` 等核心下载路径

### 优化

- `core/parsers/base.py` 及各平台解析器（B站/抖音/快手/小红书/微博/视频号/Instagram/YouTube/TikTok/Iwara/知乎等）的下载调用统一传入 `platform=self.platform.name`
- 群覆盖配置 `group_overrides` 支持覆盖自适应下载相关全部配置

## v1.4.0

### 新增

- 性能优化配置分区，包含三个独立开关：
  - `perf_render_thread_pool`（线程池渲染）：Pillow 卡片渲染在独立线程池执行，避免阻塞主事件循环
  - `perf_render_cache_enabled`（智能渲染缓存）：相同内容+相同样式的卡片只渲染一次，后续直接读缓存
  - `perf_render_cache_ttl`（缓存 TTL）+ `perf_render_cache_max_count`（缓存最大数量）：带过期时间与 LRU 数量限制的缓存管理器
- 新增 `core/cache.py` 的 `RenderCacheManager`：
  - 智能缓存键基于 `ParseResult` 内容、卡片样式配置、源媒体文件 mtime 生成 SHA256，内容/样式/源文件变化自动失效
  - 缓存项超过 TTL 自动失效
  - 超过最大数量时按 LRU 淘汰并删除对应文件
  - 缓存索引持久化到磁盘，插件重启后仍在

### 优化

- `core/render.py` 的 `render_card` 改为优先查缓存、未命中再渲染，渲染函数可在线程池中执行
- 修复 `core/config.py` 中 `verbose()` 方法被错误地放在 `effective()` 的 `return` 之后导致无法调用的问题

## v1.3.0

### 新增

- 用户黑名单 `user_blacklist`：填写用户ID后，该用户发送的链接不再触发解析
- 私聊开关 `enable_private_chat`：关闭后不再在私聊中触发解析
- 仲裁机制开关 `arbiter` 现在真正生效：关闭后不再贴表情仲裁，适合单Bot环境
- 群覆盖配置 `group_overrides`：可为指定群单独覆盖核心配置（启用、detect_action、send_parse_text、render_card、forward_threshold、合并套娃、@用户文本/模板等）
- 解析文本合并为套娃 `merge_parse_text`：开启后，解析完成后的解析文本不再单独发出，而是作为合并转发消息的一个节点与解析结果一起套娃发出
- 合并套娃引用目标 `merge_quote_target`：original=套娃引用用户原链接；merged=套娃不引用原链接，作为独立合并消息发出

### 优化

- 解析提示套娃与解析文本套娃完全分离：可单独开启/关闭，同时开启时按 tip → parse_text → media 的顺序合并为一个套娃
- 解析完成后 @用户 消息保持独立，不参与合并转发
- 使用群覆盖后的 effective 配置贯穿主流程与发送器

### 修复

- 修复切换 `detect_action=text` 仍会出现表情回应的问题：该表情实际来自仲裁机制，现在关闭仲裁或单独关闭仲裁后不再出现

## v1.2.0

### 新增

- 详细日志开关 `verbose_logging`：开启后插件会在日志中详细记录每一步操作（匹配、仲裁、防抖、发送计划、转发合并、解析文本生成等），方便排查问题
- 解析提示合并为套娃 `merge_parsing_tip`：开启后，识别到链接时发送的解析提示文字不会单独发出，而是作为合并转发消息的第一个节点与解析结果一起套娃发出（需达到转发阈值）
- 解析提示引用原链接 `quote_on_detect`：开启 text 识别反馈时，解析提示文字会引用用户发送的链接消息（QQ 平台效果最佳）
- 解析完成后 @用户 `at_after_parse` + `at_after_parse_text`：解析完成后额外发送一条 @用户 + 自定义文本的消息，并引用用户发送的链接；文本支持 `{platform}` `{title}` `{author}` `{user_name}` `{user_id}` 占位符

### 优化

- 保留并明确转发阈值 `forward_threshold` 行为：当解析结果生成的消息段数量达到阈值时继续触发合并转发

## v1.1.0

### 新增

- 解析文本模板新增占位符缺失警告：当模板使用了当前平台未提供的统计占位符时，插件会在日志中输出黄色警告，并自动隐藏该占位符
- 微信视频号解析结果现在也会返回统一的统计占位符：`{like}`点赞、`{favorite}`收藏、`{comment}`评论、`{share}`转发

### 优化

- 完善并统一各平台统计占位符：B站视频同时支持 `{reply}` 与 `{comment}`（值相同），抖音/小红书/微博/快手/视频号均提供 `{like}/{favorite}/{comment}/{share}`，部分平台额外提供 `{view}` 播放量
- 更新配置说明，在「解析文本模板」hint 中完整列出通用占位符及各平台支持的统计占位符

### 修复

- 修复 `_conf_schema.json` 中 `detect_action` 使用 AstrBot 不支持的 `select` 类型导致插件载入失败的问题，改为 `string` 类型
- 修复 `clean_cron` 为空字符串时 `CacheCleaner` 初始化报错的问题，空值时跳过自动清理任务
- 修复「识别到链接后的行为」与「发送解析文本」开关耦合的问题：两者现在独立控制，前者只控制识别到链接后的即时反馈，后者只控制解析完成后是否发送解析文本

## v1.0.0

插件更名为 **astrbot_plugin_ZhiMengParser**，由稚梦爆改后的首个版本。

### 新增

- 玻璃拟态（Glassmorphism）卡片渲染：内容包裹在圆角半透明玻璃面板中，支持自定义背景图、背景高斯模糊、面板不透明度调节（新增配置项 `card_bg_image`、`card_bg_blur`、`card_glass_opacity`）
- 纯图片内容（如小红书图文、微博图集）现在也会渲染简介卡片，新增配置项 `image_render_card` 可开关

### 修复

- 修复小红书短链接（xhslink.cn / xhslink.com）无法识别的问题

### 变更

- 移除视频封面中央的播放按钮图标
- 插件包名由 astrbot_plugin_parser 更名为 astrbot_plugin_ZhiMengParser

## v0.7891

### 新增

- 增加 iwara 视频 / 图片解析器 @sssysy
- 新增微信视频号(shipinhao)解析器 @anon0v0

### 优化

- 扩展 Twitter 解析器，使其能够同时处理 twitter.com 和 x.com URL，并规范化处理 @Zhalslar

- 基于 SSR 为抖音链接新增规范分享页解析能力 @piexian

### 修复

- 修复本地媒体发送时的文件路径问题 @piexian
- 无匹配视频流时优雅报错，避免 IndexError 中断解析 @anon0v0


本次升级由 kimi k3 智能体协助完成。
本次升级已通过人工验收，不会影响任何插件已有的功能。
