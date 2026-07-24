<div align="center">

> 由于作者原插件存在大量 Bug，本插件由 **稚梦** 深度爆改，重构优化，稳定可靠。

![:name](https://count.getloli.com/@astrbot_plugin_ZhiMengParser?name=astrbot_plugin_ZhiMengParser&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_ZhiMengParser

_✨ 稚梦爆改 · 全能链接解析器 ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-稚梦-blue)](https://github.com/SB5133/astrbot_plugin_ZhiMengParser)

</div>

## 📖 介绍

这款由 **稚梦** 深度爆改的全能链接解析器，在保持低耦合高内聚优良基因的同时，将性能压榨至极致。无论是视频、图集还是音频，也无论链接来自 B 站、抖音、小红书还是 YouTube，都能被它丝滑捕获，一网打尽。

当前支持的平台和类型：

| 平台       | 触发的消息形态                             | 视频 | 图集 | 音频 |
| ---------- | ----------------------------------------- | ---- | ---- | ---- |
| B 站       | av 号/BV 号/链接/短链/卡片/小程序          | ✅   | ✅   | ✅   |
| 抖音       | 链接（分享链接，兼容电脑端链接）           | ✅   | ✅   | ❌   |
| 微博       | 链接（博文/视频/show/文章）                | ✅   | ✅   | ❌   |
| 小红书     | 链接（含短链）/卡片                       | ✅   | ✅   | ❌   |
| 小黑盒     | 链接/卡片                                 | ✅   | ✅   | ❌   |
| 知乎       | 链接/卡片                                 | ✅   | ✅   | ❌   |
| 快手       | 链接（含标准链接和短链）                  | ✅   | ✅   | ❌   |
| AcFun      | 链接                                      | ✅   | ❌   | ❌   |
| YouTube    | 链接（含短链）                            | ✅   | ❌   | ✅   |
| TikTok     | 链接                                      | ✅   | ❌   | ❌   |
| Instagram  | 链接                                      | ✅   | ✅   | ❌   |
| Twitter    | 链接                                      | ✅   | ✅   | ❌   |

**本插件目标：凡是链接，皆可解析！** 更多平台适配持续开发中，欢迎提交 PR 共同完善。

---

## 🎨 效果图

插件默认启用 PIL 实现的通用媒体卡片渲染，效果图如下：

<div align="center">

<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/video.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/9_pic.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/4_pic.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/repost_video.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/repost_2_pic.png" width="160" />

</div>

---

## 💿 安装

直接在 AstrBot 的插件市场搜索 `ZhiMeng Parser`，点击安装，等待完成即可。

也可以从 [GitHub 仓库](https://github.com/SB5133/astrbot_plugin_ZhiMengParser) 下载源码，解压到 AstrBot 的 `data/plugins/` 目录后重载。

## ⚙️ 配置

请在 AstrBot 的插件配置面板查看并修改。

## 🎉 指令

| 指令     | 权限  | 说明                       |
| -------- | ----- | -------------------------- |
| 开启解析 | ADMIN | 开启当前会话的解析功能     |
| 关闭解析 | ADMIN | 关闭当前会话的解析功能     |
| blogin   | ADMIN | 扫码获取 B 站凭证          |

---

## 🧠 插件工作流程

当插件运行后，每一条消息的处理流程如下：

1. **消息接收**  
   监听所有消息事件，获取消息链与原始文本内容，支持普通文本、链接、卡片（JSON 组件）。

2. **基础过滤**  
   - 跳过已被禁用的会话  
   - 跳过空消息  
   - 若消息首段为 `@` 且目标不是本 Bot，则不解析

3. **链接提取与匹配**  
   - 若为卡片消息，先从 JSON 中提取 URL  
   - 采用「关键词 + 正则」双重匹配，定位对应解析器  
   - 未匹配到解析规则则直接退出

4. **仲裁判定（Emoji Like Arbiter）**  
   - 仅在 `aiocqhttp` 平台生效  
   - 通过固定表情进行 Bot 间仲裁  
   - 未胜出的 Bot 自动放弃解析

5. **防抖判定（Link Debouncer）**  
   - 对同一会话内的相同链接进行时间窗口限制  
   - 命中防抖规则则跳过解析，避免短时间重复处理

6. **内容解析**  
   - 调用对应平台解析器获取媒体信息  
   - 生成统一的 `ParseResult` 数据结构

7. **媒体下载与消息构建**  
   - 下载视频 / 图片 / 音频 / 文件  
   - 根据配置决定音频发送方式  
   - 可按配置提示下载失败项

8. **卡片渲染（可选）**  
   - 在非简洁模式或无直传媒体时生成媒体卡片  
   - 使用 PIL 渲染并缓存图片

9. **消息合并与发送**  
   - 当消息段数量超过阈值时自动合并为转发消息  
   - 最终将结果发送到对应会话

---

## 🧩 扩展

插件支持自定义解析器，通过继承 `BaseParser` 类并实现 `platform`、`handle` 即可。

示例解析器请参考：[示例解析器](https://github.com/SB5133/astrbot_plugin_ZhiMengParser/blob/main/core/parsers/example.py)

---

## 🙏 致谢

本项目核心代码来自 [nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)，请前往原仓库给作者点个 Star！