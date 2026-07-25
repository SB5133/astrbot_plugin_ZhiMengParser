# 更新日志

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
