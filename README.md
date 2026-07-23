# DS TVBox 多仓配置

DS TVBox 是一个公开、可审计的 TVBox/FongMi 配置聚合仓。它从显式登记的公开来源读取配置，执行安全检查、点播/直播健康探测和失效过滤，再把确定性的静态配置发布到 `generated` 分支。

本仓库不托管、缓存、代理或转码影视文件，也不提供服务端影视搜索。搜索、详情与播放请求由客户端直接访问配置中通过准入和健康检查的上游。

## 客户端地址

首次 `generated` 发布成功后使用以下**裸地址**，不要添加查询参数：

- TVBox/FongMi 多线路：`https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/index.json`
- 旧影视仓多仓：`https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/warehouse.json`
- 不支持多仓的客户端：`https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/configs/stable.json`

多仓协议本身不会跨仓搜索。第一条“DS 稳定聚合”会把本轮通过完整验效的点播站点合并到当前配置，客户端只搜索当前配置中的 `sites[]`。

## 权利与风险说明

“公开可访问”“配置开源”和“媒体内容已获传播授权”是三件不同的事：

- `verified`：有明确授权、自有或公共领域证据；
- `open_license`：配置数据有明确开放许可证，媒体权利仍单独记录；
- `public_unverified`：无需凭证可公开访问，但配置或媒体授权尚未核实，客户端名称带 `⚠️`；
- `unknown/restricted/takedown`：不会进入客户端公开产物。

明确禁止再分发、需要 Cookie/Token/账号、携带签名参数、依赖 DRM/付费墙绕过、私网地址、危险协议或远程可执行 Spider 的来源都会被拒绝。Catalog 新候选不会继承父仓许可证；缺少候选自身证据时只进入人工报告。

本项目的 MIT License 仅适用于本仓库原创代码与文档，不授予第三方配置或媒体内容的权利。

## 更新机制

GitHub Actions 每天执行一次 due-check。只有距离上次完整成功发布满 `240` 小时才刷新；失败不推进成功时间，下一天重试。安全下架和回滚状态会绕过 240 小时门槛进行恢复。维护者也可手动强制运行。

发布采用不可变 release 目录、双 manifest、候选两提交、非 force fast-forward、commit URL 与裸 Raw URL 双重回读。批量网络故障会保留上一成功版；安全移除不会回退到仍含违规来源的旧版。

## 本地验证

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ds-tvbox validate-static
.venv/bin/pytest
```

完整刷新和发布命令见：

```bash
.venv/bin/ds-tvbox --help
```

实现和验收契约以 [SPEC.md](SPEC.md) 为准。

## 下架与安全问题

- 权利人或来源维护者可通过 [Takedown 请求](https://github.com/azhansy/ds-tvbox/issues/new?template=takedown.yml) 提交来源、URL、权利关系和可公开证据。
- 安全漏洞请按 [SECURITY.md](SECURITY.md) 的方式私下报告，不要在公开 Issue 中提交凭证或敏感 URL。
