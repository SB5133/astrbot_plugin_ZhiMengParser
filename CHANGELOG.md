# 更新日志

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
