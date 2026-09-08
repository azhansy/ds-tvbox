# TVBox 订阅地址

复制下面两个地址到 TVBox 对应设置中。订阅内容由上游维护。

## 直播 · Guovin

填写位置：设置 → 直播 → 直播地址（不同客户端名称可能略有不同）。

```text
https://github.com/Guovin/iptv-api/releases/download/playlist-latest/result.m3u
```

M3U 格式通过，1287 条线路，含珠江台和卫视；频道播放由上游检测，本项目未逐台验证。

此版本通过时间：2026-09-08 07:56 UTC。

内容指纹：`ec4e232c11b9`。

## 点播 · 饭太硬

填写位置：设置 → 点播 → 配置地址（不同客户端名称可能略有不同）。

```text
http://www.饭太硬.cc/tv
```

配置解析、49 个站点结构、插件 DEX 和 MD5 校验通过；电视端搜索及影片播放未验证。

此版本通过时间：2026-09-08 07:56 UTC。

内容指纹：`3e1fce773e81`。

## 下载直播文件

[下载 result.m3u](https://github.com/azhansy/ds-tvbox/releases/download/playlist-latest/result.m3u) · [查看发布页和更新时间](https://github.com/azhansy/ds-tvbox/releases/tag/playlist-latest)

下载后传到电视或 U 盘，在支持本地直播文件的客户端中选择它。播放频道仍需联网，本地文件需手动重新下载更新。

附件是上次成功发布的直播列表，更新时间以发布页为准；直播检测或上传失败时保留旧附件。

## 自动更新

- 直播、点播检测及 Release 同步每两天执行一次，也可在 Actions 页面手动运行 Refresh subscriptions。
- Action 每日 UTC 19:17（北京时间次日 03:17）判断日期，按连续 UTC 天数隔天执行，跨月不重置；非执行日不访问订阅源。GitHub 排队可能延迟运行。
- 直播直接使用 Guovin 的 result.m3u；不再自行采集、合并或筛选频道。
- 直播列表检测通过后同步到本仓库的固定 Release，附件为 result.m3u；内容不变时不重复上传。
- 点播从[饭太硬导航页](https://www.xn--sss604efuw.net/)发现主/备用入口，检查配置和插件后才写入地址。
- 仅内容或检测状态变化时提交 README 和检查记录；失败保留上次地址、标记异常并让 Action 报错。每次运行时间和结果见 Action 摘要。
- 本项目不执行点播插件。配置检查通过不等于全部影片能播放；电视端效果取决于客户端和所在网络。
- 固定地址由上游直接更新，不是本项目保存的快照；上游变动与下次检查之间可能存在时间差。

[Guovin 上游](https://github.com/Guovin/iptv-api) · [直播发布页](https://github.com/Guovin/iptv-api/releases/tag/playlist-latest)

本地检查：`python -m pip install --require-hashes -r requirements.lock`，`python -m unittest discover -s tests`，`python scripts/check_sources.py`。
