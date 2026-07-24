# 更新日志

## v1.0.0

插件更名为 **astrbot_plugin_ZhiMengParser**，由稚梦接手维护后的首个版本。

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


本次升级（包括小黑盒网页逆向）由 kimi k3 智能体协助完成。
本次升级已通过人工验收，不会影响任何插件已有的功能。
