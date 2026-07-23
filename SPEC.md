# DS TVBox 多仓影视配置仓需求规格

> 文档状态：`implemented_pending_production_verification`
> 版本：`1.0.3`
> 日期：`2026-07-22`
> 目标仓库：`azhansy/ds-tvbox`
> 默认分支：`main`
> 自动发布分支：`generated`

## 1. 背景与需求结论

本项目要建设一个托管在 GitHub 的 TVBox 配置仓。用户把仓库提供的 Raw JSON 地址填入兼容的 TVBox、FongMi 或影视仓类客户端后，可以：

- 选择不同仓库或配置线路；
- 在当前配置线路内浏览和搜索点播内容；
- 查看详情并获得客户端可处理的播放地址；
- 查看直播频道；同频道存在多个候选地址时，由更新任务择优发布一条，并在后续更新中替补失效线路；
- 持续使用由 GitHub Actions 定期过滤失效项后发布的配置。

这里的“接口”是 TVBox 配置入口，不是由 GitHub 提供的影视搜索服务端 API。GitHub 仓库只负责发现、校验、标准化和托管静态配置；搜索、详情、解析和播放均发生在 TVBox 客户端或其配置引用的上游服务中。

### 1.1 已确认的产品决策

| 编号 | 决策 |
| --- | --- |
| D-001 | 当前 TVBox/FongMi 主入口使用顶层 `urls` 协议。 |
| D-002 | 额外生成 `storeHouse` 入口，兼容支持旧影视仓多仓协议的客户端。 |
| D-003 | 多仓协议本身不提供跨仓搜索；额外生成第一线路 `configs/stable.json`，聚合可安全合并且健康的站点。 |
| D-004 | 直播跨客户端统一使用 M3U，避免不同客户端嵌套直播 JSON 字段不一致。 |
| D-005 | Action 每天唤醒检查，但只有距上次成功发布满 `240` 小时才执行完整更新。 |
| D-006 | 允许公开发布互联网可直接访问、但许可证或内容授权尚未核实的 TVBox 来源；必须标记为 `public_unverified`，不能宣传为“已授权”或“开源”。 |
| D-007 | 明确禁止再分发、需要破解或任何访问凭证、绕过 DRM/付费墙，或已收到下架请求的来源不得发布。 |
| D-008 | V1 不在发布工作流中执行第三方 JAR、JavaScript、Python、二进制或 shell。 |
| D-009 | `vod_site` 的客户端字段必须由 Registry 的 `client_site` 显式给出，不从域名、标题或实现默认值猜测。 |
| D-010 | 每个含可发布点播项的来源固定生成一份独立 config；index/depot 使用第 8.3 节唯一排序与分流规则。 |
| D-011 | Catalog 父仓的许可证和权利状态不继承给下游候选；没有独立候选级证据时固定降为 `unknown`，仅进入人工候选报告。 |

## 2. 目标与成功标准

### 2.1 产品目标

1. 提供稳定、可复制到客户端的 GitHub Raw 地址。
2. 同时支持标准多线路、旧多仓兼容和单配置直连三种使用方式。
3. 自动发现显式登记的公开上游配置和直播列表。
4. 对点播和直播执行与其能力相匹配的健康检查，过滤技术失效项。
5. 每次发布可追踪来源、上游版本、健康结果和差异，并可通过普通 Git 提交回滚。
6. 将“技术可用性”和“权利状态”分开记录，避免把公开可访问误写成已获授权。

### 2.2 V1 成功标准

以下是 `bootstrap/regular` 发布的正常可用性标准；第 14.2 节的 `safety` 发布为停止违规内容可暂时低于数量标准，但必须明确降级。

- `dist/index.json` 可被目标版本 TVBoxOS 和 FongMi 识别为配置线路列表。
- `dist/warehouse.json` 可被支持 `storeHouse` 的客户端展开。
- `dist/configs/stable.json` 可被不支持多仓的客户端直接导入。
- 稳定配置至少包含 `1` 个通过约定验效的点播站点和 `1` 个有效直播频道。
- 在稳定配置内搜索一个已知标题，可以完成“搜索 → 详情 → 播放地址”闭环。
- 直播可以完成“M3U → HLS 播放列表 → 有限媒体分片读取”闭环。
- 失效源不会进入本次候选产物；若出现批量网络误判，线上上一成功版本保持不变。
- 满 240 小时才自动刷新；失败不推进成功时间，下一天自动重试。

## 3. 非目标

V1 不做以下事情：

- 不开发或分发新的 TVBox 客户端 APK。
- 不在 GitHub 中保存、下载、转码、代理或转发完整影视文件。
- 不建设服务端全库搜索 API；搜索由客户端加载当前配置的 `sites[]` 后执行。
- 不保证所有地区、运营商、IPv4/IPv6 环境下都能播放。
- 不绕过 DRM、付费墙、会员登录、地域限制、验证码或访问频控。
- 不采集、提交或公开 Cookie、Token、Authorization、账号密码、云盘凭证或私有 App Key。
- 不自动解密第三方私有配置或逆向封闭 API。
- 不承诺未核实来源具有内容传播授权。
- 不承诺纯静态直连配置具备完整内容分级或家长控制；`categories` 只能作为兼容客户端的浏览分类白名单，不能替代对上游搜索响应的服务端过滤。
- 不承诺 GitHub Actions 严格在第 240 小时执行；以“满 240 小时后的下一次有效调度”为准。

## 4. 客户端兼容基线

实现与测试至少覆盖以下协议基线；后续升级基线时必须更新兼容测试：

| 客户端/项目 | 目标能力 | 调研基线 |
| --- | --- | --- |
| FongMi/TV | `urls`、标准 `sites`、外部 M3U 直播 | `5fdff00a602dc56e8ba756174daef20edab024f2` |
| q215613905/TVBoxOS | `urls`、标准 `sites`、聚合搜索 | `0409954033a44582b431d89934e3980900f4a265` |
| mlabalabala/box | `storeHouse → urls → config` | `c5dc2b98569e829650dc680ae2588299fad79542` |

兼容原则：

- 所有发布 URL 使用绝对 HTTPS URL，不依赖客户端正确解析相对路径。
- `sites[]` 只使用目标客户端共同支持的字段；扩展字段必须经过兼容测试。
- 每个 `sites[].key` 在单份内层配置中唯一。
- 直播统一发布为 M3U；内层配置只用 `lives[].url` 引用它。
- `dist/index.json` 第一条始终是稳定聚合配置，客户端自动加载第一条时即可使用。

## 5. 用户使用流程

### 5.1 标准 TVBox/FongMi

用户在配置地址中输入：

```text
https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/index.json
```

客户端读取 `urls[]`，保存配置线路并默认加载第一条“DS 稳定聚合”。用户切换线路后，搜索只覆盖该线路内 `searchable != 0` 的健康站点。

### 5.2 旧影视仓多仓客户端

用户输入：

```text
https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/warehouse.json
```

客户端先选择 `storeHouse[]` 中的稳定仓或公共实验仓，再加载该仓的 `urls[]`。

### 5.3 不支持多仓的客户端

用户直接输入：

```text
https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/configs/stable.json
```

该地址直接返回一份包含健康 `sites[]` 和直播入口的标准 TVBox 配置。

普通 GitHub 仓库首页返回 HTML，不能作为 TVBox 配置地址。README 必须突出显示上述 Raw 地址。

三个用户入口虽然都位于浮动 `generated` ref，但每种使用方式只先读取其中一个根文件；根文件内的后续仓、配置和直播 URL 全部锁定同一 `dist/releases/gNNNNNNNN/`。因此 CDN 在切换期返回旧根或新根都可用，客户端不会把两代子文件混在一起。实现不得声称多个根文件会在 CDN 层同时生效。

## 6. 总体架构

```text
显式来源登记 / 公开候选发现
              │
              ▼
      下载与不可信输入防护
              │
              ▼
    格式识别、绝对地址化、去重
              │
              ▼
  点播链路检查 / 直播媒体有限探测
              │
              ▼
 技术状态 + 权利状态 + 隔离/拒绝原因
              │
              ▼
  临时目录生成 index/config/M3U/report
              │
              ▼
 Schema、数量、差异和批量失败闸门
              │
              ▼
 generated 分支两阶段提交 → Raw 回读校验
```

### 6.1 职责边界

仓库负责：

- 维护来源注册表、拒绝清单和策略参数；
- 获取公开配置与直播列表；
- 记录上游位置、Git commit 或内容哈希；
- 静态校验、健康探测、去重和确定性生成；
- 发布静态 TVBox 配置、M3U、状态及报告。

客户端负责：

- 加载仓库和配置线路；
- 初始化直接 API；V1 不向客户端发布 Spider 可执行依赖；
- 在当前配置内执行聚合搜索；
- 请求列表、详情、播放地址；
- 播放视频、直播和 EPG；客户端自身支持的其他播放能力不属于本仓库承诺。

## 7. 建议仓库结构

```text
.
├── .github/
│   └── workflows/
│       ├── refresh.yml
│       └── validate.yml
├── config/
│   ├── policy.yaml
│   └── compatibility.yaml
├── schemas/
│   ├── source-registry.schema.json
│   ├── tvbox-config.schema.json
│   ├── depot.schema.json
│   └── report.schema.json
├── sources/
│   ├── registry.yaml
│   └── denylist.yaml
├── src/
│   └── ds_tvbox/
├── tests/
│   ├── fixtures/
│   ├── unit/
│   └── integration/
├── README.md
├── SPEC.md
└── pyproject.toml
```

`generated` 分支只保存生成物：

```text
dist/
├── index.json
├── warehouse.json
├── configs/
│   └── stable.json
├── live/
│   └── stable.m3u
├── manifest.json
├── health.json
├── reports/
│   ├── latest.json
│   └── latest.md
└── releases/
    └── g00000001/
        ├── index.json
        ├── warehouse.json
        ├── depots/
        │   ├── stable.json
        │   └── public-unverified.json
        ├── configs/
        │   ├── stable.json
        │   └── <source_id>.json
        ├── live/
        │   └── stable.m3u
        ├── manifest.json
        └── health.json
state/
└── release.json
```

创建新内容 release 时，`release_id` 固定为 `g` 加 8 位零填充的单调递增 `generation`，例如首版 `g00000001`。`state/release.json` 另用 `active_release_id` 指明当前激活内容；正常发布两者对应，回滚事件会继续递增 `generation` 但把 `active_release_id` 指回旧 release，从而不会在下次刷新复用已存在目录。回滚导致的 release 编号空洞是合法状态，编号不得回收。根级 `index.json`、`warehouse.json` 和 `configs/stable.json` 是三个用户入口；它们在单个文件内只引用对应 `dist/releases/<active_release_id>/` 下的不可变资源。根级 live/health 是当前版逐字节别名，根 manifest 是指向当前 release manifest 的独立指针清单。普通刷新只新增 release 目录并切换根级入口，不修改或删除历史 release；V1 无限期保留历史 release，只有第 18 节的安全/下架处置可以例外删除。

## 8. 对外数据契约

### 8.1 标准多线路入口 `dist/index.json`

```json
{
  "urls": [
    {
      "name": "DS 稳定聚合",
      "url": "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/releases/g00000001/configs/stable.json"
    },
    {
      "name": "DS 公共实验源",
      "url": "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/releases/g00000001/configs/example-source.json"
    }
  ]
}
```

要求：

- 顶层只能有一个 `urls` 数组。
- 第一项必须是稳定聚合配置。
- `name` 非空且不重复；`url` 为绝对 HTTPS URL。
- 只收录本轮技术状态允许发布的配置。
- 每个 `url` 必须包含与 `state.active_release_id` 完全相同的 `release_id`，禁止引用根级浮动别名或其他 release；回滚后不得拿状态事件 `generation` 推导该路径。

### 8.2 多仓兼容入口 `dist/warehouse.json`

```json
{
  "storeHouse": [
    {
      "sourceName": "DS 稳定仓",
      "sourceUrl": "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/releases/g00000001/depots/stable.json"
    },
    {
      "sourceName": "DS 公共实验仓",
      "sourceUrl": "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/releases/g00000001/depots/public-unverified.json"
    }
  ]
}
```

每个 `sourceUrl` 必须返回 `urls[]` 文件，不能直接指向普通内层 `sites[]` 配置；两个 URL 必须锁定同一 `release_id`，depot 内的所有 URL 也必须继续锁定该 release。

- `depots/stable.json` 第一项为本代稳定聚合，之后可列出 `publication_status: stable` 的独立配置，不按权利状态冒充授权。
- `depots/public-unverified.json` 只列 `rights_status: public_unverified` 且发布状态为 `stable/experimental` 的独立配置，名称全部带 `⚠️`。健康直接 API 可能同时出现在两个 depot，这是“技术稳定视图”和“权利风险视图”的有意交集。
- `withheld/rejected` 以及全部 `type: 3` 不得出现在任一 depot。

### 8.3 稳定内层配置 `dist/configs/stable.json`

```json
{
  "sites": [
    {
      "key": "src_example_01",
      "name": "示例点播源",
      "type": 1,
      "api": "https://vod.example.test/api.php/provide/vod/",
      "searchable": 1,
      "quickSearch": 1,
      "filterable": 1,
      "changeable": 1
    }
  ],
  "lives": [
    {
      "name": "DS 稳定直播",
      "type": 0,
      "url": "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/dist/releases/g00000001/live/stable.m3u"
    }
  ],
  "parses": []
}
```

稳定聚合规则：

- “稳定”只表示技术链路和客户端兼容性达到稳定发布标准，不表示其中所有内容权利都已经核实；实际权利状态必须同时查看 `rights_status`。
- V1 只合并通过完整链路验证的 `type: 0/1` 直接 API；`type: 4` 可按矩阵保留为独立实验线路，`type: 3` Spider 不进入任何客户端可见产物。
- `key` 冲突时改写为 `src_<source_id>_<原 key 或短哈希>`。
- 显示名称冲突时添加来源短名，但不伪造上游名称。
- 所有允许发布的相对 `api`、直播和图片 URL 必须按原始配置 URL 解析为绝对 HTTPS 地址。
- V1 不自动聚合公共 VIP 解析器；直接播放地址应由站点 API 或已准入的客户端适配器返回。
- 根级 `dist/configs/stable.json` 与当前 release 内同名文件逐字节相同，因此直连用户也只会继续请求该 release 的 M3U；它不是再引用根级浮动 M3U 的拼接配置。

客户端可见配置使用递归字段白名单：

- 顶层只允许 `sites`、`lives`、`parses`；`parses` 固定为空数组，`spider`、`rules`、远程脚本加载字段及其他未知键不得输出。
- `type: 0/1` 站点只允许 `key/name/type/api/searchable/quickSearch/filterable/changeable/categories`。
- `type: 4` 仅在无需 `ext`、Header、parser 或其他客户端扩展时允许实验发布，字段白名单与上项相同；出现非空 `ext` 即 `client_extension_unsupported/withheld`。
- 任意站点出现 `jar`、可执行型 `ext`、`type: 3`，或任意层出现 `.jar/.js/.py/.dex/.so` 可执行依赖 URL 时，该站点 `rejected`，依赖不下载给客户端；上游同配置中完全独立的合规 `type: 0/1` 可继续逐项提取。
- `lives[]` 每项只允许 `name/type/url`，其中 `url` 必须指向本代仓内 HTTPS M3U；M3U 的允许字段见 8.4。

生成后必须对完整 JSON/M3U 语法树递归扫描，而不是只检查顶层字符串；任何黑名单字段或可执行依赖残留都让候选发布失败。

独立 config 与 depot 使用以下唯一生成算法：

1. 每个至少含一个 `stable/experimental` 点播项的来源生成且只生成一份 `configs/<source_id>.json`；同一 `vod_config` 的多个可发布站点留在该文件中，不按站点拆文件。纯直播来源和仅含 `withheld/rejected` 项的来源不生成独立 config。
2. 独立 config 的 `sites[]` 只含该来源的可发布站点；`lives[]` 与本代稳定聚合使用相同的仓内 M3U 引用（本代无频道时为 `[]`）；`parses` 固定为 `[]`。
3. `index.json.urls[]` 第一项永远是稳定聚合；其后列出全部独立 config，排序键固定为 `(publication_rank, rights_rank, source_id)`，其中 `stable=0`、`experimental=1`，`verified=0`、`open_license=1`、`public_unverified=2`。来源内同时有 stable/experimental 项时取最优即最小 publication rank。
4. `depots/stable.json.urls[]` 第一项永远是稳定聚合，其后只列至少含一个 `stable` 点播项的独立 config，按 `(rights_rank, source_id)` 排序。
5. `depots/public-unverified.json.urls[]` 只列 `rights_status: public_unverified` 且至少含一个 `stable/experimental` 点播项的独立 config，按 `(publication_rank, source_id)` 排序；允许为空数组，不能用稳定聚合占位冒充纯风险视图。
6. `vod_site` 独立线路名称取来源 `client_site.name`；`vod_config` 独立线路名称固定为 `DS <source_id>`。`public_unverified` 名称统一且只添加一次 `⚠️ ` 前缀。所有名称按最终显示字符串去重，冲突时追加 ` [<source_id>]`。
7. `configs/stable.json.sites[]` 只含 `stable` 点播项，按 `(source_id, site.key)` 的 UTF-8 字节序排序；独立 config 内按 `site.key` 排序。所有序列化排序均在警告前缀和 key 重写完成后执行。
8. Safety 可生成空 `sites/lives`；此时 index 和 stable depot 仍各保留唯一稳定聚合入口，风险 depot 为 `urls: []`。

### 8.4 M3U

```m3u
#EXTM3U
#EXTINF:-1 tvg-id="example.news" group-title="公开频道",示例新闻
https://live.example.test/channel/index.m3u8
```

要求：

- UTF-8、LF 换行、第一行是 `#EXTM3U`。
- 同一规范化播放 URL 只保留一次。
- 频道显示名非空；无法验证的 logo/EPG 只移除附属字段，不因此删除可播放频道。
- V1 每个规范化频道只发布一条当前最优播放 URL。其他候选 URL 保留在健康状态库，下次刷新时可以替补；V1 不声称 M3U 在客户端运行时支持同频道自动换线。

### 8.5 发布清单

每代不可变清单 `dist/releases/<release_id>/manifest.json` 至少包含：

```json
{
  "schema_version": "1.0.0",
  "generated_at": "2026-07-22T12:00:00Z",
  "generation": 1,
  "release_id": "g00000001",
  "generator_version": "1.0.0",
  "source_count": 2,
  "vod_site_count": 1,
  "live_channel_count": 1,
  "previous_commit_sha": null,
  "content_workflow_run_id": "123456789",
  "content_workflow_run_attempt": 1,
  "artifacts": {
    "dist/releases/g00000001/index.json": "sha256:<hex>",
    "dist/releases/g00000001/warehouse.json": "sha256:<hex>",
    "dist/releases/g00000001/depots/stable.json": "sha256:<hex>",
    "dist/releases/g00000001/depots/public-unverified.json": "sha256:<hex>",
    "dist/releases/g00000001/configs/stable.json": "sha256:<hex>",
    "dist/releases/g00000001/configs/ikun-vod.json": "sha256:<hex>",
    "dist/releases/g00000001/live/stable.m3u": "sha256:<hex>",
    "dist/releases/g00000001/health.json": "sha256:<hex>"
  },
  "upstreams": [
    {
      "source_id": "iptv-org-cn-cctv",
      "fetch_mode": "github_tracked_file",
      "reviewed_revision": "061e61103edeab1ecd1bc4d99cb38992f003653e",
      "resolved_revision": "061e61103edeab1ecd1bc4d99cb38992f003653e",
      "resolved_fetch_url": "https://raw.githubusercontent.com/iptv-org/iptv/061e61103edeab1ecd1bc4d99cb38992f003653e/streams/cn_cctv.m3u",
      "terms_sha256": {
        "LICENSE": "640514163b17f977adc997cb16f51871122cfb0555ebad1a3f01e167b7ba8857",
        "README.md": "cf46be9e40b9ee97e120b0c2461b8316b1d7872682b91ccada76e69e88b72563"
      }
    }
  ]
}
```

该 `artifacts` 只允许包含自身 `dist/releases/<release_id>/` 下的 index、warehouse、两个 depot、stable、所有本代客户端可见的独立 `configs/<source_id>.json`、M3U 和 health；不得包含任何根级浮动文件、其他 release 或 manifest 自身。键使用仓库相对路径并稳定排序。因此历史 release manifest 在后续切换根入口后仍能永久独立验真。

根级指针清单 `dist/manifest.json` 使用独立结构：

```json
{
  "schema_version": "1.0.0",
  "generated_at": "2026-07-22T12:00:00Z",
  "active_release_id": "g00000001",
  "previous_commit_sha": null,
  "content_workflow_run_id": "123456789",
  "content_workflow_run_attempt": 1,
  "release_manifest": {
    "path": "dist/releases/g00000001/manifest.json",
    "sha256": "sha256:<hex>"
  },
  "aliases": {
    "dist/index.json": "sha256:<hex>",
    "dist/warehouse.json": "sha256:<hex>",
    "dist/configs/stable.json": "sha256:<hex>",
    "dist/live/stable.m3u": "sha256:<hex>",
    "dist/health.json": "sha256:<hex>"
  }
}
```

`aliases` 覆盖所有根级当前入口/便捷别名；其中 index、warehouse、stable、live、health 必须分别与当前 release 内同名文件逐字节相同。`release_manifest.sha256` 把根指针闭包到当前不可变 release，但 release manifest 不反向包含根路径。

根 manifest 的 `previous_commit_sha` 记录“这份内容快照首次生成时”的上一 `generated` HEAD，用于内容血缘；回滚会逐字节恢复目标根 manifest，因此该字段不保证等于回滚补偿提交的直接父提交。分支事件级的上一 HEAD 与后续回滚目标始终以 `state/release.json.previous_release_head_sha` 为准，正常内容发布时两者应相等。

release manifest 与根 manifest 的 `content_workflow_run_id/content_workflow_run_attempt` 是内容来源身份，两者必须一致且在历史 release 中不可变。`bootstrap/regular/safety` 创建新内容时，内容来源身份等于本次发布事件身份；`rollback` 复用旧内容时，两个 manifest 保留目标旧身份，而新状态和报告记录本次回滚事件身份，禁止为追求身份相等而改写旧 manifest。

以下文件明确不进入任一自引用哈希集合：

- `dist/manifest.json` 自身；其 SHA-256 只作为 Job 输出和 Action artifact 元数据保存；
- release manifest 自身；它的 SHA-256 只写入根 manifest 和 Job 输出；
- `state/release.json`，因为它会在发布确认提交中从 `pending` 变为 `success`；
- `dist/reports/latest.json` 和 `dist/reports/latest.md`，因为确认提交会补入 `content_commit_sha` 和最终发布结论。

发布确认提交只能修改上述状态和报告文件，不得修改根 manifest、release manifest、`aliases` 或 release `artifacts` 覆盖的任何文件；否则候选作废并重新生成。

release manifest 的 `upstreams[]` 按 `source_id` 排序，并为每个启用来源记录 `fetch_mode`、人工审查 revision、实际解析 revision/URL 和本轮条款哈希；`direct_url` 的两个 revision 字段均为 `null`。由于准入已拒绝凭证化 URL，这里可以保存完整公开获取 URL；若未来政策允许敏感参数，必须先修改本契约为脱敏哈希，不能直接泄露。

## 9. 来源注册与准入

### 9.1 `sources/registry.yaml`

每个来源必须显式登记，不能仅凭 GitHub 搜索结果直接进入发布：

```yaml
version: 1
sources:
  - id: example-vod
    kind: vod_config
    parser: tvbox_json
    enabled: true
    fetch:
      mode: github_tracked_file
      reviewed_url: https://raw.githubusercontent.com/example/tvbox-config/0123456789abcdef0123456789abcdef01234567/tvbox.json
      repository_url: https://github.com/example/tvbox-config
      track_ref: main
      config_path: tvbox.json
      reviewed_revision: 0123456789abcdef0123456789abcdef01234567
    terms_watch:
      - type: github_path
        url: null
        path: LICENSE
        reviewed_sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    discovery_evidence: null
    rights_status: public_unverified
    config_license_status: unknown
    content_rights_status: unverified
    license_spdx: NOASSERTION
    license_url: null
    rights_evidence:
      summary: Publicly reachable without credentials at review time.
      urls: []
    allowed_hosts:
      - api.github.com
      - raw.githubusercontent.com
    allow_discovered_media_hosts: false
    http_exceptions: []
    category_policy:
      deny_names: []
    client_site: null
    catalog: null
    reviewed_at: 2026-07-22
```

字段要求：

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定、唯一、仅含小写字母、数字和连字符。 |
| `kind` | `vod_site`、`vod_config`、`live_playlist` 或 `repository_catalog`。 |
| `parser` | 显式解析器名称，禁止按响应内容执行代码。 |
| `enabled` | 必填布尔值，无默认值；`false` 时只保留登记，不获取、不发布。 |
| `fetch` | 固定对象，包含 `mode/reviewed_url/repository_url/track_ref/config_path/reviewed_revision`；模式及快照语义见下文。 |
| `terms_watch` | 必填数组；每项是 `{type, url, path, reviewed_sha256}`，按模式监控公开条款。无证据可监控时为 `[]`，不得省略。 |
| `discovery_evidence` | 候选发现证据对象 `{repository_url, revision, config_path, selector}`；并非实际获取仓时也不得混入 `fetch`。不适用时为 `null`。 |
| `rights_status` | 综合发布分类，见 9.2。 |
| `config_license_status` | 配置本身的许可状态：`verified`、`unknown` 或 `restricted`。 |
| `content_rights_status` | 配置所引用媒体的权利状态：`verified`、`unverified`、`restricted` 或 `takedown`。 |
| `license_spdx` | 明确许可证；未知时为 `NOASSERTION`。 |
| `license_url` | 许可证或授权页面 URL；未知时为 `null`。 |
| `rights_evidence` | 固定对象 `{summary, urls[]}`，记录为什么允许进入当前候选池及其证据 URL。 |
| `allowed_hosts` | 获取配置、API 和固定依赖时允许访问的主机集合。 |
| `allow_discovered_media_hosts` | 是否允许探测合法列表/详情响应中新出现的公网媒体 host；即使为 `true` 也必须逐跳通过 10.2 的 SSRF 防护。 |
| `http_exceptions` | 必填数组；每项必须精确给出 `{host, port, path_prefix, reason, reviewed_at}`。默认空数组；只有匹配项可使用明文 HTTP。 |
| `category_policy` | 可选分类展示策略；生成器从首页分类中剔除 `deny_names` 后，把剩余名称写入 `sites[].categories`。这是客户端浏览白名单，不是内容安全边界。 |
| `client_site` | `vod_site` 必填对象 `{key,name,searchable,quickSearch,filterable,changeable}`；其他 kind 必须为 `null`。`key/name` 非空，四个能力值只能为 `0/1`，生成器不得臆造默认值。 |
| `catalog` | `repository_catalog` 的确定扫描契约，见 9.5；其他 kind 必须为 `null`。 |
| `reviewed_at` | 最近人工审查日期。 |

所有来源对象必须显式包含上述顶层字段，未知字段由 Schema 拒绝；可选对象用 `null`，不能靠缺省值让实现者猜测。`category_policy` 非空时固定为 `{deny_names: string[]}`。`vod_site` 输出字段逐字节取自 `client_site`，仅允许生成器按第 8.3 节添加风险前缀、稳定来源前缀或 categories；完整链路通过时仍使用登记的能力开关，不把探测成功反向改写为 `1`。

`parser` 仅允许 `maccms_json`、`maccms_xml`、`tvbox_json`、`tvbox_json5`、`m3u`、`txt_live`、`repository_catalog`；`parsers_by_glob[].parser` 仅允许 `tvbox_json`、`tvbox_json5`、`m3u`、`txt_live`。MacCMS parser 只用于 `vod_site` 的直接 API，不是仓库文件候选格式；`repository_catalog` 也只能作为来源顶层 parser。`allowed_hosts` 必须非空且只含精确小写 DNS 主机名，不接受 IP、端口或通配符。`http_exceptions[].port` 范围为 `1..65535`，`path_prefix` 必须以 `/` 开头，`reason` 非空，`reviewed_at` 为 `YYYY-MM-DD`。`license_spdx` 必须是 SPDX 标识或 `NOASSERTION`。

`fetch.mode` 的唯一取值与条件如下：

| 模式 | 允许 kind | 字段要求 | 每轮实际获取 |
| --- | --- | --- | --- |
| `direct_url` | `vod_site/vod_config/live_playlist` | `reviewed_url` 必填；其余四个 fetch 字段均为 `null` | 始终获取同一 `reviewed_url` |
| `github_tracked_file` | `vod_config/live_playlist` | 六个 fetch 字段全非空；`reviewed_revision` 为 40 位 commit，`reviewed_url` 必须包含该 commit 和 `config_path` | 把 `track_ref` 解析为 `resolved_revision`，构造只含该 commit 的 Raw URL |
| `github_repository` | `repository_catalog` | `repository_url/track_ref/reviewed_revision` 非空，`reviewed_url/config_path` 为 `null` | 按 `resolved_revision` 和 `catalog` 扫描仓库树 |

`kind × parser × fetch.mode` 只允许以下组合，Schema 在任何网络请求前拒绝其他笛卡尔组合：

| `kind` | 允许 parser | 允许 fetch mode | `terms_watch` 附加约束 |
| --- | --- | --- | --- |
| `vod_site` | `maccms_json/maccms_xml` | `direct_url` | 为空或仅含 `type: url` |
| `vod_config` | `tvbox_json/tvbox_json5` | `direct_url/github_tracked_file` | direct 为空或仅 `url`；tracked 至少一个 `github_path`，可附加 `url` |
| `live_playlist` | `m3u/txt_live` | `direct_url/github_tracked_file` | direct 为空或仅 `url`；tracked 至少一个 `github_path`，可附加 `url` |
| `repository_catalog` | `repository_catalog` | `github_repository` | 至少一个 `github_path`，可附加 `url` |

`terms_watch.type: github_path` 仅在两种 GitHub mode 合法，且 `path` 非空、`url: null`；`type: url` 在任意 mode 合法，且 `url` 为允许 host 上的固定 HTTPS URL、`path: null`。例如 `direct_url + github_path`、`vod_site + tvbox_json`、`repository_catalog + direct_url` 都是 Schema 反例，不能留给运行时代偿。

`reviewed_*` 是人工审查信任锚，`resolved_*` 是某轮运行值，两者不能共用字段或互相覆盖。选择 `github_tracked_file/github_repository` 就表示维护者授权同一仓库、同一跟踪 ref、同一解析器和限定路径内的**数据更新**自动进入验效，不代表自动接受许可证或条款变化。两种跟踪模式的 `terms_watch` 不得为空，至少覆盖 `LICENSE` 和仓库中实际承载使用限制的 README/条款文件；没有可监控条款文件时不得自动跟踪，只能改用固定 `direct_url`。每轮先在 `resolved_revision` 读取全部 `terms_watch`：`type: github_path` 使用该 commit 下的 `path`，其 `url` 必须为 `null`；`type: url` 使用固定 `url`，其 `path` 必须为 `null`。`reviewed_sha256` 计算 HTTP 内容解码后的原始响应字节，不做字符集或换行归一化，单个条款文件最大 `1 MiB`。任一 SHA-256 不符时，来源失败原因为 `terms_changed`，技术状态保留上次值（无历史则 `unknown`），发布状态设为 `withheld`，直到人工更新审查锚。实际 `resolved_revision/resolved_fetch_url` 只写 manifest 和报告，不回写 registry。

### 9.2 权利状态

权利状态与技术健康状态相互独立：

| 状态 | 定义 | V1 发布策略 |
| --- | --- | --- |
| `verified` | 自有、明确授权、公共领域，或配置与内容访问权利证据清晰。 | 按下方唯一发布矩阵计算。 |
| `open_license` | 配置有明确开源/开放数据许可证，但内容权利仍需单独记录。 | 按下方唯一发布矩阵计算。 |
| `public_unverified` | 无需凭证即可公开访问，但许可证或媒体内容授权未核实。 | 按已确认决策允许发布，但必须按下方唯一发布矩阵计算并显式提示风险。 |
| `restricted` | 上游明确禁止复制、再分发或当前使用方式。 | 拒绝发布。 |
| `takedown` | 权利人、平台或维护者要求下架。 | 立即进入 denylist，并从下一发布版本移除。 |
| `unknown` | 无法确认是否为真正公开入口，或需要进一步人工调查。 | 只进入候选报告。 |

V1 的唯一发布矩阵如下；其他章节和实现不得另设更宽松分支：

| 权利状态 | 实体与技术状态 | `publication_status` | 公开产物 |
| --- | --- | --- | --- |
| `verified/open_license` | `type: 0/1` 点播完整链路 `healthy` | `stable` | `configs/stable.json` 及独立线路 |
| `verified/open_license` | `type: 4` 或直接点播仅达到 `partial` | `experimental` | 仅公共实验仓中的独立线路，不进入稳定聚合 |
| 任意 | `type: 3` JAR/JS/Python Spider | `rejected` | V1 只写候选报告，不进入任何客户端可见产物 |
| `verified/open_license` | 直播 URL 完成 manifest 和分片验证为 `healthy` | `stable` | 经频道择优后进入 `live/stable.m3u` |
| `public_unverified` | `type: 0/1` 点播完整链路 `healthy` | `stable` | 名称加 `⚠️` 前缀，进入稳定聚合及风险标记独立线路 |
| `public_unverified` | `type: 4` 或直接点播仅达到 `partial` | `experimental` | 名称加 `⚠️` 前缀，仅进入公共实验仓中的独立线路 |
| `public_unverified` | 无凭证直播 URL 完成 manifest 和分片验证为 `healthy` | `stable` | 频道名称加 `⚠️` 前缀，经择优后进入 `live/stable.m3u` |
| `verified/open_license/public_unverified` | `unknown` | `withheld` | 不进入客户端可见配置，等待本轮检查 |
| `verified/open_license/public_unverified` | `suspect/dead/unsupported_environment`，或直播未完成分片验证 | `withheld` | 不进入任何客户端可见配置，下轮复检 |
| `unknown` | 任意 | `withheld` | 仅候选报告 |
| `restricted/takedown` | 任意 | `rejected` | 拒绝公开并按需加入 denylist |

矩阵中的“稳定”只描述技术发布级别，不是版权或内容授权背书。一个频道只要所选 URL 为 `public_unverified`，即使同名候选中还有其他状态未知的 URL，公开名称也必须保留 `⚠️` 前缀。

即使选择允许 `public_unverified`，以下硬性拒绝条件仍然有效：

- 上游明确写明禁止再分发、禁止当前地区或当前用途；
- 需要登录、Cookie、Token、邀请码、付费账号或私有 App Key；
- 包含绕过 DRM、付费墙、验证码或地域限制的步骤；
- 需要解密私有配置或执行来源不明二进制；
- URL、Header 或 Cookie 中携带任何访问凭证或签名参数；即使值看起来是公开测试值也不例外；
- 已列入 `sources/denylist.yaml`；
- 收到有效下架请求。

### 9.3 候选发现

- 用户星标项目用于协议和候选发现参考，不等于自动授权清单。
- `ngo5/IPTV` 等无 LICENSE 的目录只能提供候选线索；下游每个入口单独登记和验效。
- `Guovin/iptv-api` 可作为直播聚合与验效行为参考；若复制或修改其代码，必须遵守 AGPL-3.0。
- `mlabalabala/box` 用作多仓协议兼容参考，不直接复制其数据源。
- 单轮递归发现最大深度为 `2`，最大候选配置 `100` 个，最大直播地址 `1000` 个。
- 聚合仓的许可证结论不得自动继承给它引用的下游配置或媒体入口。

### 9.4 真实种子基线

V1 以以下两个真实候选作为实现和首次上线基线。它们只证明在 `2026-07-22` 调研时公开可访问并满足技术协议，不构成内容授权背书；首次发布仍必须重新执行全部验效。

#### 点播种子 `ikun-vod`

```yaml
id: ikun-vod
kind: vod_site
parser: maccms_json
enabled: true
fetch:
  mode: direct_url
  reviewed_url: https://ikunzyapi.com/api.php/provide/vod
  repository_url: null
  track_ref: null
  config_path: null
  reviewed_revision: null
terms_watch:
  - type: url
    url: https://www.ikunzy.com/ikun/help.html
    path: null
    reviewed_sha256: 6e56ee8d95be04050c02f39f423a3d19b02115f4accb6927db2dd763068cc35b
discovery_evidence:
  repository_url: https://github.com/miru-project/repo
  revision: 651821e2ea4213b48040e84c5e851f7c2c1082db
  config_path: repo/vod.api.json.collection.js
  selector:
    key_equals: ikun
    api_equals: https://ikunzyapi.com/api.php/provide/vod
rights_status: public_unverified
config_license_status: unknown
content_rights_status: unverified
license_spdx: NOASSERTION
license_url: null
rights_evidence:
  summary: The provider publishes a no-login collection endpoint and collection tutorials; API and media rights are not covered by an explicit license.
  urls:
    - https://www.ikunzy.com/ikun/help.html
    - https://github.com/miru-project/repo/blob/651821e2ea4213b48040e84c5e851f7c2c1082db/repo/vod.api.json.collection.js#L29
allowed_hosts:
  - ikunzyapi.com
  - www.ikunzy.com
allow_discovered_media_hosts: true
http_exceptions: []
category_policy:
  deny_names:
    - 伦理片
    - 里番动漫
client_site:
  key: ikun_vod
  name: iKun 资源
  searchable: 1
  quickSearch: 1
  filterable: 1
  changeable: 1
catalog: null
reviewed_at: 2026-07-22
```

调研时已验证：无参数首页返回 JSON 分类和列表；使用客户端同构参数可以按已知标题搜索；`ids` 详情包含播放线路和播放地址；抽样 HLS manifest 可读取，首个媒体分片返回 `206`。发现目录本身采用 MIT 许可证，iKun 帮助页公开提供采集入口和教程，但这两项都不能证明 API 配置或媒体内容已获得开放许可证，因此 `config_license_status` 仍为 `unknown`，综合状态必须是 `public_unverified`。生成器把首页分类中除 `伦理片`、`里番动漫` 外的名称写入 `sites[].categories`，尽量避免客户端在浏览页展示这些分类；由于客户端仍会直接请求上游搜索接口，V1 不得把这一字段宣传为完整成人内容过滤。

#### 直播种子 `iptv-org-cn-cctv`

```yaml
id: iptv-org-cn-cctv
kind: live_playlist
parser: m3u
enabled: true
fetch:
  mode: github_tracked_file
  reviewed_url: https://raw.githubusercontent.com/iptv-org/iptv/061e61103edeab1ecd1bc4d99cb38992f003653e/streams/cn_cctv.m3u
  repository_url: https://github.com/iptv-org/iptv
  track_ref: master
  config_path: streams/cn_cctv.m3u
  reviewed_revision: 061e61103edeab1ecd1bc4d99cb38992f003653e
terms_watch:
  - type: github_path
    url: null
    path: LICENSE
    reviewed_sha256: 640514163b17f977adc997cb16f51871122cfb0555ebad1a3f01e167b7ba8857
  - type: github_path
    url: null
    path: README.md
    reviewed_sha256: cf46be9e40b9ee97e120b0c2461b8316b1d7872682b91ccada76e69e88b72563
discovery_evidence: null
rights_status: public_unverified
config_license_status: verified
content_rights_status: unverified
license_spdx: Unlicense
license_url: https://github.com/iptv-org/iptv/blob/061e61103edeab1ecd1bc4d99cb38992f003653e/LICENSE
rights_evidence:
  summary: Playlist data is Unlicense, while the referenced broadcast content rights remain unverified.
  urls:
    - https://github.com/iptv-org/iptv/blob/061e61103edeab1ecd1bc4d99cb38992f003653e/LICENSE
allowed_hosts:
  - api.github.com
  - raw.githubusercontent.com
allow_discovered_media_hosts: true
http_exceptions: []
category_policy:
  deny_names: []
client_site: null
catalog: null
reviewed_at: 2026-07-22
```

Action 每轮先把 `fetch.track_ref` 解析为不可变 `resolved_revision`，在该 commit 核对 LICENSE 和 README 哈希，再用同一 commit 与 `fetch.config_path` 构造 `resolved_fetch_url`，避免检查与获取跨快照。条款哈希不符时暂停发布；相符时才允许数据列表自动更新。实际解析值写入 manifest，不回写 `main` 上的人工审查锚。调研基线包含 `15` 条记录，其中后 `12` 条 URL 带 `auth=testpub`，按第 10.2 节的无凭证规则逐 URL 拒绝，不能原样发布整份列表。剩余前 `3` 条无凭证候选仍需各自完成 HLS manifest 和媒体分片验证，只有本轮 `healthy` 的频道才能发布；调研时抽样第一条已完成该链路。播放域名由列表动态产生，只有通过网络安全和无凭证检查的公网 HTTP(S) 目标才可临时加入本轮探测范围。

若任一种子在首次实现时无法通过完整验效，Phase 1 只能标记为“框架完成”，不得声称产品验收完成；必须用满足相同准入契约的新真实来源替换并记录证据。

### 9.5 `repository_catalog` 发现契约

`repository_catalog` 必须使用 `fetch.mode: github_repository`，并提供以下固定形态的 `catalog`；数字字段均为必填正整数，不使用隐式默认值：

```yaml
catalog:
  max_depth: 2
  path_globs:
    - configs/**/*.json
    - playlists/**/*.m3u
  parsers_by_glob:
    - glob: configs/**/*.json
      parser: tvbox_json
    - glob: playlists/**/*.m3u
      parser: m3u
  selectors:
    sites_arrays:
      - /sites
    depot_arrays:
      - /urls
    storehouse_arrays:
      - /storeHouse
    live_arrays:
      - /lives
  max_files: 200
  max_candidates: 100
  max_live_urls: 1000
  allowed_downstream_hosts:
    - media.example.test
  auto_onboard_public_unverified: false
```

`path_globs` 使用 POSIX `/` 分隔、相对仓库根目录，不允许 `..`；每个匹配文件必须且只能命中一条 `parsers_by_glob`。`selectors` 使用 RFC 6901 JSON Pointer，只定位数组容器，不支持通配符或任意脚本表达式；对应数组内字段再按第 8 节 TVBox 固定结构解析。`allowed_downstream_hosts` 只接受精确小写主机名，不接受 `*`，候选 URL 的每个 host 都必须命中；动态媒体 host 仍另受来源级 `allow_discovered_media_hosts` 和第 10.2 节约束。超过任一上限则整个 catalog 本轮为 `inconclusive`，不得截断后假装扫描完成。

每轮流程为：

1. 按 9.1 把跟踪分支解析为 `resolved_revision`，先核对非空 `terms_watch`，只读取该快照。
2. 按允许路径提取 `sites[]`、`urls[]` 或直播 URL，不执行仓库代码。
3. 为每个候选生成确定性 ID、仓库、commit、路径、JSON Pointer、发现时间和候选级权利状态。候选 ID 固定为 `candidate:<catalog_source_id>:<hash16>`，其中 `hash16 = sha256(candidate_kind + "\\0" + normalized_target_identity)` 前 16 位；点播 target identity 使用 `(type, normalized_api)`，直播使用规范化原始 URL，嵌套配置使用规范化配置 URL。相同 target 的多条发现路径合并为一个候选，证据位置稳定排序。
4. 新候选先写入 Action artifact 中的 `reports/candidates.json`，该文件不进入 `generated` 分支。Catalog 来源级 `rights_status/config_license_status/content_rights_status` 只描述父仓，不得继承给候选；候选没有独立证据时固定为 `unknown/withheld`，只能进入人工 review 队列，任何 `auto_onboard_public_unverified`、技术健康或父仓开源状态都不能把它送入 stable/experimental。
5. `auto_onboard_public_unverified: true` 只允许无凭证且未命中硬拒绝的 `unknown` 候选自动进入技术验效，便于生成报告，不改变其 `withheld` 发布状态。只有维护者把该 target 作为独立 `sources[]` 项登记、补齐候选自身的 `rights_evidence/client_site` 并通过 9.1 Schema 后，下一轮才按新来源自己的权利矩阵计算发布；父 catalog 仅保留 discovery provenance。
6. 仓库 LICENSE、README 或来源条款的内容哈希发生变化时，暂停该 catalog 的新增自动发布，标记 `terms_changed`，等待重新审核。

Catalog 递归只跟随同一 GitHub 仓库、同一 `resolved_revision` 内可还原为仓库相对路径的配置引用；匹配文件本身深度为 `0`，每经过一条 `urls/storeHouse` 仓内引用边深度加 `1`，超过 `max_depth` 不读取但记录 `catalog_depth_exceeded`。外部仓库或普通互联网配置 URL 只形成候选，不递归获取。访问集合键为 `(repository_url, resolved_revision, normalized_path)`；重复键立即停止该边以打破循环。每个文件必须恰好命中一个 parser glob，同一 target 的候选按上项 ID 去重；任一文件、候选或直播 URL 上限超出时整轮 catalog 为 `inconclusive`，不截断后继续发布。

## 10. 抓取、规范化与去重

### 10.1 获取限制

- 默认只允许 HTTPS。明文 HTTP 请求必须同时命中来源 `http_exceptions` 的精确 `host`、`port` 和规范化 `path_prefix`，且该项有非空理由与审核日期；初始 URL和每次重定向分别判断，未命中即拒绝。例外不豁免凭证、Header、SSRF 或允许 host 检查。
- `http_exceptions` 只授权生成器获取不会原样交给客户端的上游容器、目录或证据，不能授权客户端可见 URL。最终 JSON 中的仓地址、`api/ext/jar/playUrl/lives/epg/logo` URL 以及 M3U 播放 URL 一律必须是 HTTPS；无法无损替换为上游明确提供的 HTTPS 地址时，不做协议升级猜测，该实体记录 `client_http_disallowed` 并不发布。因而 direct `vod_site` 的 `fetch.reviewed_url` 若为 HTTP，只能进报告，不能进入 stable/experimental。
- 配置响应最大 `5 MiB`，直播列表最大 `10 MiB`，HLS manifest 最大 `2 MiB`。
- 单次连接超时 `5s`，读取超时 `15s`，最大重定向 `5` 次。
- 暂时错误最多请求 `3` 次，使用指数退避并遵守 `Retry-After`。
- 全局网络并发默认 `20`，单主机并发默认 `4`。
- 单轮最多读取 `200 MiB` 外部数据；媒体探测不得下载完整视频。
- 使用明确 User-Agent，并提供项目地址和联系入口。

### 10.2 不可信输入安全

每次初始请求和重定向都必须：

1. 解析目标主机；
2. 拒绝 loopback、private、link-local、multicast、reserved 和云元数据地址；
3. 建立连接后核对实际目标 IP，防止 DNS rebinding；
4. 重新校验重定向后的 scheme、host 和 IP；
5. 拒绝 `file:`、`javascript:`、`data:`、`ftp:`、`clan:` 及未知 scheme。

无凭证规则同时适用于来源入口和每个动态发现的点播/直播媒体 URL：

- 拒绝 URL userinfo、`Authorization`、Cookie，以及 query key 归一化后匹配 `auth`、`authorization`、`token`、`access_token`、`api_key`、`apikey`、`key`、`sign`、`signature`、`secret`、`expires` 或 `expiry` 的地址；匹配大小写不敏感，并忽略 `_`、`-` 差异。
- 例如 `?auth=testpub` 仍属于凭证化 URL，必须拒绝，不能因值固定或可公开看到而豁免。
- 来源入口命中时拒绝整个来源；子站点或媒体 URL 命中时只拒绝该实体，除非过滤后触发来源归零或批量失败闸门。
- 报告只记录命中的参数名和脱敏原因，不保存或输出参数值。

凭证 query 的键名判定算法固定为：先按 RFC 3986 切分原始 query，每个键只执行一次严格 percent-decoding（`+` 不转换为空格），非法 `%` 编码或解码后非 UTF-8 直接以 `credential_query_rejected` 拒绝；随后执行 Unicode NFKC、ASCII 小写并移除 `_`、`-`。任一重复键只要规范化后命中敏感集合即拒绝，不使用“最后一个覆盖前一个”；空键可保留但不能影响其他键判定。值不参与键名判断且绝不写日志，也不得递归多次解码来创造另一套语义。

V1 对来源提供的 HTTP Header 使用“白名单外全部拒绝”，不能仅靠敏感名称黑名单：

- 唯一允许的 Header 名为 `User-Agent`、`Referer`、`Origin`、`Accept`、`Accept-Language`，名称大小写不敏感；`Range` 只能由健康探测器内部生成，来源不能提供。
- `Authorization`、`Proxy-Authorization`、`Cookie`、`Set-Cookie`、`X-API-Key`、`X-Auth-Token` 以及所有其他 `X-*`/非白名单名称都拒绝对应实体。
- Header 名和值均不得包含 CR/LF/NUL；单个值 UTF-8 编码后最大 `1024` 字节，总 Header 最大 `4096` 字节。`Referer` 必须是无 userinfo、无凭证 query 的公网 HTTP(S) URL；`Origin` 只能是 `scheme://host[:port]`。
- 统一解析 TVBox `sites[].header`、M3U `#EXTHTTP` JSON、`#EXTVLCOPT:http-referrer`、`#EXTVLCOPT:http-user-agent` 和 URL 行尾 `|Header=Value&...`。合法项规范化为独立 Header 映射后再发请求，不能把行尾语法原样当 URL。
- URL query 中出现 `header`、`headers` 或 `http_header` 时一律拒绝，不能把其值二次解码后注入请求；`#EXTHTTP` 非法 JSON、未知 `#EXTVLCOPT` Header 或畸形行尾 Header 同样拒绝对应实体。
- 白名单 Header 只允许生成器做受控技术探测，不构成客户端输出支持。对声明了合法自定义 Header 的实体必须执行两条独立探针：先完全移除来源 Header、仅使用生成器固定 User-Agent 执行正常全链路；该无 Header 链路完整通过时，丢弃来源 Header 后可按其他规则发布。只有无 Header 链路失败时，才允许再用规范化白名单 Header 做一次诊断探针；若带 Header 才通过，技术结果可记录，但发布状态必须为 `withheld`、原因为 `client_header_unsupported`。两条结果不得合并凑成一次成功。最终 VOD JSON 一律不输出 `header`，最终 M3U 一律不输出 `#EXTHTTP/#EXTVLCOPT` 或 `|Header=...`。无来源 Header 的请求只使用生成器自己的固定 User-Agent，不受此限制。

来源名称、URL 和响应字段不得拼接成 shell 命令。JSON5 只能通过不执行代码的解析器读取，禁止 `eval`、`source` 或导入下载内容。

### 10.3 规范化和去重

- URL scheme 和 host 转小写，移除默认端口及无意义 fragment。
- 保留可能影响签名或资源定位的 query，不随意排序；日志中对敏感 query 值做脱敏。
- V1 可发布点播站点按规范化后的 `(type, api)` 去重；含 `ext/jar` 的实体先按 8.3 隔离，不能靠把扩展字段纳入去重键后继续发布。
- 直播地址按最终重定向后的规范化媒体 URL 去重。
- 同一来源重复 `key` 视为 Schema 错误；跨来源冲突使用来源前缀重写。
- 生成物使用稳定排序，保证相同输入得到相同主体内容。

## 11. 点播验效

### 11.1 支持级别

| 类型 | V1 检查方式 | 可声明状态 |
| --- | --- | --- |
| `type: 0` XML API | 首页/分类、搜索、详情和播放地址全链路 | `healthy` 或相应失败状态 |
| `type: 1` JSON API | 首页/分类、搜索、详情和播放地址全链路 | `healthy` 或相应失败状态 |
| `type: 4` 扩展 HTTP | V1 只做字段和端点静态校验，不实现通用运行时适配 | 最高 `partial`，只进实验仓 |
| `type: 3` JAR/JS/Python Spider | 只记录依赖 URL、大小、哈希和静态字段；V1 不下载后执行，也不转发给客户端 | 技术状态最高 `partial`，发布状态固定 `rejected`，只进报告 |

`type: 4` 只有同时满足以下条件才可判为 `partial/experimental`：站点字段通过 8.3 白名单；`api` 是无凭证的绝对 HTTPS URL；不存在 `ext/header/parser` 或可执行依赖；无 Header 探针实际 GET 经允许重定向后返回 `2xx`、正文非空且在配置大小上限内；正文不是登录页、验证码页或通用 HTML 错误页。V1 不解析其业务协议、不宣称搜索或播放健康；任一条件失败按对应稳定失败原因 `withheld/rejected`。

### 11.2 `type: 0/1` Adapter 契约

V1 只把与锁定客户端行为一致的 MacCMS XML/JSON 直接 API 认定为可完整验效。请求从 `sites[].api` 开始，保留原 URL 中不冲突的固定 query；操作参数使用 URL query 合并，禁止字符串直接拼接。

| 操作 | `type: 0` XML | `type: 1` JSON |
| --- | --- | --- |
| 首页 | 原样 GET `api` | 原样 GET `api` |
| 分类 | `ac=videolist&t=<type_id>&pg=<page>` | `ac=detail&t=<type_id>&pg=<page>`；有筛选时增加 `f=<JSON>` |
| 搜索 | `wd=<keyword>&quick=false&extend=`，第 2 页起增加 `pg` | 同左；不额外臆造 `ac`，但保留 `api` 自带的固定 `ac` |
| 详情 | `ac=videolist&ids=<vod_id>` | `ac=detail&ids=<vod_id>` |

参数冲突时，本次操作的 `ac`、`t`、`pg`、`ids`、`wd`、`quick`、`extend` 和 `f` 覆盖同名旧值；其他固定 query 原样保留。包含凭证类 query 的来源仍按 9.2 拒绝。

可接受响应的最小结构：

- JSON 首页/分类/搜索：顶层 `class[]` 或 `list[]` 至少一个非空；分类项有 `type_id/type_name`，影片项有非空 `vod_id/vod_name`。
- JSON 详情：`list[]` 恰好能定位请求 ID，且 `vod_play_from` 与 `vod_play_url` 非空并能按线路对应。
- XML：根为 `rss`；分类位于 `class/ty` 且有 `id`；影片位于 `list/video`，至少包含非空 `id/name`；详情播放线路位于 `video/dl/dd`。
- 播放字段按线路 `$$$`、剧集 `#`、标题与 URL 分隔符 `$` 解析；允许忽略线路末尾由一个或多个 `#` 形成的空终止 token，但线路中间的空剧集、线路数不一致、空 URL 或非法 scheme 均为契约失败。
- 直接播放仅接受通过安全校验的 HTTP(S) 媒体 URL。需要嗅探、VIP 解析、客户端私有 parser 或登录态时只能标记 `partial`。

`tests/fixtures/` 必须分别提供最小合法和缺字段的 XML、JSON 响应，并用同一个 adapter 同时驱动健康检查与集成测试，禁止为真实源写没有登记的站点特判。

### 11.3 全链路检查

直接 API 必须完成：

1. 基础端点可访问，返回协议预期的 XML/JSON，而非登录页、验证码页或通用 HTML 错误页。
2. 首页或分类响应具有至少一个合法条目。
3. 从该条目提取已知标题，再调用搜索接口反查；搜索为空不直接视为网络错误，但反查必须返回兼容结构。
4. 使用结果 ID 请求详情，验证标题、ID 和播放字段。
5. 解析至少一个剧集/线路播放地址。
6. 对直接 HLS/媒体地址执行有限字节探测；需要嗅探、专有解析器或会员授权时标记 `partial`，不得伪称完整可播放。

健康检查只证明 GitHub Runner 所在网络在检查时刻可用，不代表所有用户网络都可用。

### 11.4 失败原因枚举

以下枚举供来源、点播站点、频道和直播 URL 共用；Schema 不接受列表外字符串：

```text
fetch_timeout
dns_failure
tls_failure
http_404
http_410
rate_limited
upstream_5xx
invalid_json
invalid_xml
schema_incompatible
home_contract_failed
search_contract_failed
detail_contract_failed
play_contract_failed
media_probe_failed
credential_required
credential_query_rejected
credential_header_rejected
invalid_header_syntax
private_address_rejected
dangerous_scheme_rejected
response_too_large
client_http_disallowed
client_header_unsupported
client_extension_unsupported
unsupported_spider
unsupported_environment
blocked_by_source
missing_upstream
terms_changed
rights_restricted
takedown
catalog_depth_exceeded
catalog_limit_exceeded
```

同一实体同时命中多个原因时只用以下优先级最高的一项作为 `failure_reason`，其余写入脱敏 `secondary_reasons[]`：`takedown` → `rights_restricted/terms_changed` → 凭证、私网、危险协议、非法 Header 与客户端不可发布依赖 → `http_404/http_410/dns_failure/tls_failure` → JSON/XML/Schema/首页/搜索/详情/播放硬契约错误 → `rate_limited/upstream_5xx/fetch_timeout` → `unsupported_environment` → `blocked_by_source/missing_upstream`。权利/安全原因按 9.2 决定 `rejected/withheld` 且不改写最后技术事实；硬网络或协议错误为 `dead`，暂时错误为 `suspect`，环境限制为 `unsupported_environment`，客户端扩展限制最高为 `partial`。Catalog 深度命中只跳过该边并记录 `catalog_depth_exceeded`；上限命中整个 catalog 为 `inconclusive/catalog_limit_exceeded`。

## 12. 直播验效

每条直播地址依次检查：

1. 实际 GET 播放列表，不能只依赖 HEAD。
2. M3U/TXT 可以解析，频道名称和播放 URL 完整。
3. HLS 主列表或媒体列表包含有效结构。
4. 在大小和时间上限内读取一个小媒体分片；最多读取 `1 MiB`。
5. 必须记录响应时间、协议、最终 URL，以及可解析时的 `RESOLUTION/BANDWIDTH`；无法取得的分辨率和带宽显式写 `null`，不能省略后由不同实现猜测。
6. EPG 或 Logo 失效时只删除对应附属字段；频道仍可播放则保留。
7. 仅 IPv6、地域限制或 Runner 环境不支持时标记 `unsupported_environment`，不能直接认定永久死亡。

同一频道候选线路的发布择优顺序：

1. `consecutive_successes` 降序；
2. 媒体链路分：成功读取主列表 → 子媒体列表 → 分片为 `2`，直接媒体列表 → 分片为 `1`，其他不具备发布资格；
3. 成功探测总耗时 `response_ms` 升序，多次成功取中位数；
4. 质量分降序：同时已知分辨率和 `BANDWIDTH` 时，`>=1920×1080 && >=3,000,000` 为 `4`，`>=1280×720 && >=1,500,000` 为 `3`，`>=640×360 && >=500,000` 为 `2`，已知且低于该阈值为 `0`；任一元数据缺失为 `1`。质量只用于排序，不把未知元数据误判为失效；
5. 规范化原始 URL 按 UTF-8 字节字典序升序，作为最终 tie-breaker。

频道先做全局身份归并再择优：有非空 `tvg-id` 时，Unicode NFKC、trim、ASCII 小写后的 ID 相同即跨来源合并；没有 `tvg-id` 时不跨来源猜测合并，身份使用 `source_id + NFKC/trim/连续空白折叠后的频道名`。因此同一全局 `tvg-id` 最终只发布一条最佳 URL，而两个仅同名但无 ID 的不同来源保持独立频道。若合并候选的显示名或 group 冲突，选中 URL 所属记录决定显示字段，并继续应用 `⚠️` 权利前缀。

## 13. 技术状态机与过滤

### 13.1 状态

| `technical_status` | 含义 |
| --- | --- |
| `unknown` | 新发现或尚未完成本轮检查。 |
| `healthy` | 核心能力完整通过。 |
| `partial` | 核心配置可读，但部分能力无法或不允许在 CI 中验证。 |
| `suspect` | 重试后仍出现超时、429、5xx 等暂时错误。 |
| `dead` | 404/410、NXDOMAIN、格式损坏或核心契约明确失败。 |
| `unsupported_environment` | Runner 网络不具备 IPv6、地区或协议条件。 |

发布资格单独记录为 `publication_status`：

| `publication_status` | 含义 |
| --- | --- |
| `stable` | 进入技术稳定聚合。 |
| `experimental` | 只进入公共实验仓或独立线路。 |
| `withheld` | 因技术状态暂不发布，但下轮复检。 |
| `rejected` | 因安全、明确限制或下架策略拒绝发布。 |

### 13.2 状态转换

```text
unknown ──完整通过──> healthy
unknown ──部分可验──> partial
healthy ──暂时失败──> suspect
suspect ──恢复通过──> healthy
suspect ──明确硬失败──> dead
dead ──后续完整恢复──> healthy
```

技术硬失败立即进入 `dead`：

- 404、410、NXDOMAIN；
- TLS 证书验证失败且上游未修复；
- 非法协议或重定向到私网；
- 响应损坏、Schema 不兼容；
- 核心搜索/详情协议明确不成立。

权利状态变为 `restricted` 或 `takedown` 时，仅把 `publication_status` 设为 `rejected`，保留最后一次 `technical_status`，以便报告准确区分“不可用”和“不可发布”。

超时、429、5xx 和 Runner 公共网络异常属于暂时错误。完成三次退避重试后进入 `suspect`，不永久删除来源登记。

### 13.3 逐项健康模型

健康状态不得只停留在聚合来源层。实现必须保存以下四类实体；点播是来源→站点树，直播是来源→URL 与全局频道→URL 的可追踪关系图：

| 层级 | 稳定 ID | 过滤粒度 |
| --- | --- | --- |
| 来源 | `source:<source_id>` | 上游配置整体是否可读取和解析。 |
| 点播站点 | `vod:<source_id>:<fingerprint16>` | 每个 `sites[]` 项独立验效和发布。 |
| 直播频道 | `channel:<channel_key16>` | 全局频道是否至少有一条可发布 URL。 |
| 直播 URL | `live-url:<source_id>:<url_hash16>` | 每条候选播放 URL 独立媒体探测。 |

ID 计算规则：

- `fingerprint16` 为 `sha256(type + normalized_api)` 的前 16 个十六进制字符；上游显示名或 `key` 改名不改变历史身份。含 `ext/jar` 的拒绝实体只在候选报告使用独立 `candidate_id`，不创建可发布站点 ID。
- `channel_key16` 按第 12 节身份规则计算：有 `tvg-id` 时为 `sha256("tvg:" + normalized_tvg_id)` 前 16 位，可跨来源；缺少时为 `sha256("local:" + source_id + ":" + normalized_name)` 前 16 位，不跨来源。名称也缺失则拒绝该频道。
- `url_hash16` 基于配置中规范化后的原始 URL，不使用可能每次变化的最终重定向 URL。
- 只有稳定 ID 完全一致时才继承连续成功次数、失败次数和 `last_success_at`；地址或指纹变化按新实体从 `unknown` 开始。

聚合与过滤规则：

1. 每个点播站点独立计算 `technical_status/publication_status`；失效站点从输出 `sites[]` 单独移除，不连带删除同一上游的其他健康站点。
2. 每条直播 URL 独立检查。频道有至少一条 `healthy` URL 时保留，并按第 12 节选择一条最佳 URL 写入 V1 M3U；没有健康 URL 时频道不发布。
3. 上游配置发生硬解析错误时，来源进入 `dead`；本轮无法枚举的既有子项进入 `dead/withheld` 并记录 `blocked_by_source`，不能伪造为每个子项分别 404。若来源只是暂时获取失败，则来源和既有子项均为 `suspect/withheld`，子项同样记录 `blocked_by_source`。
4. 可枚举子项时，来源聚合状态严格按以下首个命中顺序计算：任一 `healthy` → `healthy`；否则任一 `partial` → `partial`；否则任一 `suspect` → `suspect`；否则任一 `unknown` → `unknown`；否则任一 `unsupported_environment` → `unsupported_environment`；否则全部为硬失败 → `dead`。来源自身硬解析失败优先于该顺序，直接为 `dead`。
5. `publication_status` 由技术状态、9.2 权利矩阵和安全策略共同计算，不能反向改写技术状态。

对直播来源，以上“子项”指该来源局部 `live_url`，不把跨来源全局频道重复挂到多个来源下；全局 `channel` 状态根据其 `candidate_url_ids` 另行聚合。

上游成功解析但上一成功版本中的既有子项本轮不再出现时，该子项为 `dead/withheld`，失败原因为 `missing_upstream`。安全、权利或 Spider 规则可在保留上述技术状态的同时把 `publication_status` 改为 `rejected`。

历史差异和批量失败闸门的精确定义：

- 点播基线集合是上一成功版本中实际进入客户端配置的 `vod_site` 稳定 ID；分母为该集合大小，分子是其中本轮变为非发布状态或从上游消失的数量。
- 直播基线集合是上一成功版本 `health.json` 中隶属于已发布频道、且当时为 `healthy` 的全部 `live_url` 稳定 ID，包括 M3U 所选 URL 和保留的健康备选 URL；分母为该集合大小，分子是其中本轮不再 `healthy` 或消失的数量。未曾健康、因凭证/安全被拒绝或仅新发现的 URL 不计入。
- 新发现项不进入失败分母；频道归零闸门另按本轮可发布 `live_channel` 数计算。

## 14. 批量失败保护

为了避免 GitHub Runner、DNS 或上游公共故障导致大量误删，满足任一条件时，本轮结论为 `inconclusive`：

- 上一成功版本某一基线集合至少有 `5` 个实体，且该类别本轮失败分子除以对应基线分母大于 `20%`；点播使用已发布 `vod_site` 集合，直播使用第 13.3 节定义的已发布频道健康 URL 集合，两者分别计算、不能相互稀释；
- 本轮点播站点或直播频道任一核心类别归零；
- 第 14.1 节定义的四个公共网络探针组中至少两个在同一观察窗内失败；
- 产物数量低于 `config/policy.yaml` 的最低基线；
- 非首次发布时状态库无法读取，或上次成功版本无法确定。

`inconclusive` 时：

- 不更新 `generated` 分支线上产物；
- 不更新 `last_success_at`；
- 上传本轮诊断报告；
- 下一天调度继续重试。

V1 默认基线：

```yaml
minimums:
  vod_sites: 1
  live_channels: 1
failure_gate:
  minimum_previous_items: 5
  max_new_failure_ratio: 0.20
network_outage_gate:
  observation_window_seconds: 60
  attempts_per_probe: 2
  retry_delay_seconds: 2
  connect_timeout_seconds: 3
  total_timeout_seconds: 5
  max_redirects: 2
  failed_groups_to_abort: 2
```

### 14.1 公共网络探针契约

四个探针组必须在首个请求开始后的同一 `60s` 观察窗内运行。每个 HTTP 探针执行 `2` 次无凭证 GET，两次都不满足预期才算该组失败；DNS 组按下述组内规则判断。探针只用于区分 Runner 公共网络故障，不能替代来源自身健康检查。

| 组 | 请求 | 通过条件 |
| --- | --- | --- |
| `github_raw` | GET `https://raw.githubusercontent.com/FongMi/TV/5fdff00a602dc56e8ba756174daef20edab024f2/docs/CONFIG.md` | HTTP `200`，响应体 `1..1048576` 字节 |
| `dns_public` | 分别解析 `one.one.one.one` 和 `dns.google` 的 A/AAAA | 至少一个名称在 `5s` 内解析出至少一个通过第 10.2 节检查的公网 IP |
| `cloudflare_http` | GET `https://www.cloudflare.com/cdn-cgi/trace` | HTTP `200`，响应体小于等于 `64 KiB` 且匹配 `(?m)^ip=.+$` |
| `google_http` | GET `https://connectivitycheck.gstatic.com/generate_204` | HTTP `204`，响应体为空 |

HTTP 探针沿用第 10.2 节的逐跳 SSRF 检查，使用表中超时和重定向上限，不发送来源自定义 Header。只有至少 `2` 个组在该观察窗内失败才触发网络型 `inconclusive`；单组失败只写报告。探针定义受 `main` 分支代码评审保护，若服务永久变更，必须通过普通代码变更更新本表和 fixture，运行时不得由实现者临时任选替代地址。

### 14.2 安全移除优先级

批量失败保护只能阻止技术误删，不能把已禁止的旧来源继续留在线上。以下任一变化命中当前激活 release 时形成 `mandatory_removal_set`：denylist 新命中、`restricted/takedown`、`terms_changed`，或发现当前已发布实体携带凭证、私网目标、危险协议等安全违规。

存在强制移除集时，本轮进入 `release_kind: safety`：

1. 以当前成功 release 为基线，只删除强制集合及依赖它的站点、频道和 URL，不借机加入未经完整验效的新来源。
2. 由这些强制删除直接造成的失败比例、类别归零和最低数量不足，不触发 `inconclusive`；Schema、路径闭包、无凭证和发布事务检查仍不可绕过。公共探针失败不触发“保留违规旧版”的回退，但 GitHub Git/Raw 交付面必须可写且可完成最终回读，否则任务只能失败报警。
3. 删除后允许发布 `sites: []`、`lives: []` 和只含 `#EXTM3U` 的空安全配置，报告状态为 `safety_degraded`，不得谎称满足 V1 正常可用性基线。
4. 若来源网络或非 GitHub 公共探针失败，生成器不得重新抓取来源，而是从上一成功不可变 release 确定性派生删除结果。若 GitHub 交付面本身不可用，自动任务无法完成发布，必须高优先级报警并在下一次调度重试，不能声称已经下架。
5. 成功发布时更新 `last_publish_at`，保留旧 `last_success_at`，使下一天继续尝试正常恢复。确认限制的 `restricted/takedown` 和凭证/安全违规还必须按第 18 节清理历史 release；仅因条款哈希变化产生的 `terms_changed` 先停止当前入口引用，历史清理由人工复审决定。

“只删除”约束作用于来源、实体和其静态/探测事实，不要求保留已经失真的派生索引。Safety 新代只允许以下机械变化：本代 `generation/release_id/generated_at`、source 的 `last_checked_at`、release-local URL 中的 release ID，以及由保留 items/URLs 唯一确定的 source/channel 聚合、候选集合和选择结果；其余 retained item、upstream、rights 和客户端配置事实必须与精确上一 `generated` HEAD 一致。Artifact 负责本代独立安全、Schema、闭包和配置子集校验；只有持有精确上一 HEAD 的 Publisher 可以证明跨代纯减法，当前 `main` Registry 的普通 endpoint、显示名或 client 字段变化不得改写或阻塞另一个来源的紧急 safety 下架。

若上一成功 release 或强制删除 ID 无法可靠读取，自动任务不得猜测性重建；应失败报警并要求维护者执行人工安全处置。该异常不能回退为继续发布已知违规入口。

## 15. GitHub Actions 更新机制

### 15.1 调度语义

GitHub cron 的月内日期字段 `*/10` 不是持续每 10 天，跨月间隔不固定。V1 使用每日 due-check：

```yaml
on:
  schedule:
    - cron: "17 19 * * *"
  workflow_dispatch:
    inputs:
      force:
        description: Ignore the 240-hour gate
        required: false
        type: boolean
        default: false
      bootstrap:
        description: Create the first generated release
        required: false
        type: boolean
        default: false

concurrency:
  group: refresh-tvbox-sources
  cancel-in-progress: false
```

`19:17 UTC` 对应新加坡时间次日 `03:17`。避开整点以降低 GitHub 高峰调度延迟。

执行规则：

1. 通过 Git ref 解析远端 `generated` HEAD，并从该精确 commit checkout `state/release.json`，不得用可能受 CDN 缓存影响的浮动 Raw URL 做 due-check。ref 不存在时，定时任务输出 `bootstrap_required` 并结束；只有人工 `workflow_dispatch(bootstrap=true)` 可以首次创建。ref 已存在却没有合法 `status: success` 时属于 `invalid_release_state`，任何模式都不得把它当作空仓覆盖，必须先人工修复或删除该错误 ref。
2. 从最终成功状态读取 `release_kind/last_success_at`。当前 kind 为 `safety` 或 `rollback` 时设置 `recovery_due=true`，下一次每日计划任务必须绕过 240 小时门槛执行完整恢复尝试；恢复尝试未发布 `regular` 成功版前，该状态持续生效。
3. `force == true`、`recovery_due == true` 或距离上次完整成功 `>= 240h` 时执行完整刷新；其他情况下以成功状态跳过。`bootstrap=true` 隐含 `force=true`。
4. 候选内容和最终状态按第 16 节在临时候选 ref 完成两阶段提交与回读，最后一次性提升到 `generated`；稳定分支不得暴露 `pending` 状态。
5. 只有 `generated` 提升后同时通过最终 commit SHA 回读和第 16.1 节规定的浮动分支 URL 回读，本次运行才成功。
6. 失败不推进时间，下一次每日调度继续尝试。
7. 即使资源主体没有变化，成功完成全量复检后也要提交新的状态和报告，以记录本次成功时间；不得仅因 `dist/index.json` 无差异而跳过 `last_success_at` 更新。

计划任务仅从默认分支运行，可能延迟或被丢弃；公共仓库长期无活动时也可能被 GitHub 禁用。因此 SLA 是“满 240 小时后的下一次有效调度”，通常约 10～11 天，不承诺精确 240 小时。

### 15.2 权限隔离

工作流顶层：

```yaml
permissions: {}
```

至少拆成两个 Job：

1. `collect-and-validate`
   - `permissions: { contents: read }`
   - 不持有 PAT 或发布密钥；
   - 下载、解析、探测并生成普通数据文件；
   - 不执行第三方内容。
2. `publish`
   - 仅在所有闸门通过后运行；
   - `permissions: { contents: write }`；
   - 重新验证 artifact 路径、大小、Schema 和 SHA-256；
   - 不执行 artifact 中的任何文件。

其他要求：

- 默认使用短期 `GITHUB_TOKEN`，不配置 PAT。
- 第三方 Action 固定完整 commit SHA，不使用浮动 tag。
- 禁止 `pull_request_target` 触发发布。
- 不把不可信文本直接插入 `run:` shell。
- 日志不输出凭证或完整敏感 query。
- `refresh.yml` 设置合理 `timeout-minutes`，V1 建议 `90` 分钟。

### 15.3 首次发布与状态契约

首次发布必须由维护者手动触发 `bootstrap=true`，并满足：

- `generated` ref 在运行开始和最终创建前两次检查均不存在；保护通过预先配置的名称匹配 ruleset 生效，而不是预建空分支；
- 两个真实种子均重新验效，或已有符合相同准入契约的替代种子；
- 点播和直播绝对最低数量闸门通过；
- 网络安全、Schema、权限和 Raw 回读检查全部通过；
- 因不存在上一版本，只跳过“相对失败比例”闸门，不跳过任何绝对闸门。

最终成功状态 `state/release.json` 的完整最小结构：

```json
{
  "schema_version": "1.0.0",
  "status": "success",
  "release_kind": "bootstrap",
  "generation": 1,
  "active_release_id": "g00000001",
  "last_publish_at": "2026-07-22T12:00:00Z",
  "last_success_at": "2026-07-22T12:00:00Z",
  "content_commit_sha": "1111111111111111111111111111111111111111",
  "previous_release_head_sha": null,
  "workflow_run_id": "123456789",
  "workflow_run_attempt": 1
}
```

状态和报告的 `workflow_run_id` 是 GitHub `run_id` 字符串，`workflow_run_attempt` 是从 `1` 开始的整数；两者共同构成本次发布事件身份，并必须与候选 ref 后缀一致。manifest 使用第 8.5 节定义的独立内容来源身份：`bootstrap/regular/safety` 时两种身份相等，`rollback` 时内容身份保留目标旧值、事件身份使用本轮新值。`release_kind` 仅允许 `bootstrap/regular/safety/rollback`。候选提交中的同一文件使用 `status: pending`，预先写入本轮 kind、递增后的 `generation` 和新 `active_release_id`，保留旧的 `last_success_at`，并写入 `previous_release_head_sha`、`workflow_run_id` 和 `workflow_run_attempt`；候选提交不尝试记录自身 commit SHA，避免自引用。最终状态提交保持同一 generation/active release，再写入工作流已经获得的 `content_commit_sha` 和 `last_publish_at`。只有完整通过正常最低数量闸门的 `bootstrap/regular` 才把 `last_success_at` 更新为本轮时间；`safety/rollback` 保留此前完整成功时间，使次日 due-check 能继续尝试恢复。`pending` 只能存在于临时候选 ref，不能出现在稳定 `generated` 分支。

状态中的 `generation` 是分支发布事件序号；当前 release manifest/health 中的 `generation` 是该内容 release 首次创建时的序号。正常发布二者相等，回滚后允许状态序号更大，但必须满足 `state.active_release_id == root_manifest.active_release_id == release_manifest.release_id == health.release_id`。

## 16. 生成、发布与回滚

### 16.1 两阶段发布

1. bootstrap 固定 `next_generation = 1`；其他内容发布从当前成功状态计算 `next_generation = previous.generation + 1`。据此得到唯一 `new_release_id = g<next_generation:08d>` 并断言目录不存在；在临时目录保留所有历史 `dist/releases/`，新增当前 release，把状态的 `generation/active_release_id` 分别设为该序号和新 ID，并生成根级三个入口/别名。普通刷新不得修改已有 release 目录。
2. 运行 JSON Schema、M3U、重复 key、危险 URL、数量、差异、release 路径闭包和历史目录不可变检查。三个根入口的所有仓内 URL 必须只落入 `state.active_release_id`。
3. 按第 8.5 节先计算 release-local `artifacts` 并生成不可变 release manifest，再计算它的 SHA-256 和根级 `aliases` 生成根 manifest；两种 manifest 都排除自身。根 manifest 记录当前 `generated` HEAD 为 `previous_commit_sha/expected_previous_head`，首次发布时为 `null`；状态和确认阶段报告不参与哈希集合。
4. 从 `expected_previous_head` 创建临时分支 `candidate/run-<workflow_run_id>-attempt-<workflow_run_attempt>`；首次发布使用 orphan 候选分支。GitHub 重跑会复用 `run_id` 但递增 `run_attempt`，因此上一 attempt 遗留候选可以保留供诊断而不会阻塞新 attempt；同一二元组的 ref 已存在时则失败报警，禁止覆盖。
5. 生成候选状态 `status: pending`，创建“候选内容提交”并以 exact-OID lease push 到临时分支；创建时期望 ref 不存在，更新时先证明新提交是期望旧 SHA 的后代。`--force-with-lease` 在这里只承载 compare-and-swap，不授权非快进更新；记录 `content_commit_sha`，任何 ancestry 失败都必须在发起网络写入前拒绝。
6. 使用 `content_commit_sha` 的 Raw URL 回读根 manifest、它指向的 release manifest、全部 release-local `artifacts` 和根级 `aliases`，逐项核对路径和 SHA-256；两个 manifest 自身 SHA 写入 Job 输出，不回写自身。
7. 回读成功后在候选分支创建“发布确认提交”，只更新 `state/release.json`、`dist/reports/latest.json` 和 `dist/reports/latest.md`：状态改为 `success`，写入 `content_commit_sha` 和新 `last_publish_at`；`bootstrap/regular` 同时更新 `last_success_at`，`safety/rollback` 保留旧值。不得再次递增候选提交已经确定的 `generation`。
8. 使用确认提交 SHA 回读状态、报告、两个 manifest 和核心客户端入口；同时断言根 manifest、release manifest、全部 `artifacts/aliases` 与内容提交逐字节一致。全部成功后得到 `final_candidate_sha`。
9. 再次核对远端 `generated` 状态。常规发布要求 HEAD 仍等于非空 `expected_previous_head`，并证明 `final_candidate_sha` 是它的后代，再以绑定该旧 SHA 的 exact-OID lease 完成语义上的 fast-forward；首次 bootstrap 要求 ref 仍不存在，再以期望空 ref 的 exact-OID lease 新建并指向 orphan `final_candidate_sha`。任何竞态或非快进关系都必须失败并重新运行，禁止覆盖现有 ref。
10. 先按远端 `generated` 最终 SHA 回读稳定入口、两个 manifest 和成功状态，并确认 ref SHA 等于 `final_candidate_sha`。随后轮询用户实际配置的三个**完全不带 query/fragment 的裸浮动分支 URL**（`dist/index.json`、`dist/warehouse.json`、`dist/configs/stable.json`），解析并以同样的裸 URL 语义跟随它们引用的全部当前 release URL；同时裸回读浮动根 manifest、其指向的 release manifest 和 `state/release.json`。每 `5s` 重试一次、总计最多 `120s`，必须满足四方 active/release ID 等式、全部哈希匹配、状态/报告事件身份与候选 ref 一致、两个 manifest 的内容来源身份一致且状态为 `success`；`bootstrap/regular/safety` 还要求内容身份等于事件身份，`rollback` 则要求内容身份等于目标 release 的旧身份。满足这些条件后本次发布才算完成。带 `?run=...` 等 cache-busting URL 只能作为 CDN 诊断信息，绝不能替代任一裸 URL 或满足发布成功条件。
11. 删除临时候选分支。失败时删除或保留供诊断都不能改变 `generated`；遗留候选 ref 超过 24 小时后可由清理步骤删除。
12. 只有在第 9 步之后分支 URL 验证仍超时失败时才补偿：`regular` 按 16.2 回滚；首次 `bootstrap` 按 16.3 撤销新建 ref；`safety` 不得恢复含 `mandatory_removal_set` 的旧版，保持 Git ref 指向已按 commit SHA 验证无违规项的 safety 候选，在 Action 结论/告警中标记 `delivery_unverified` 并让后续任务继续分支 URL 验证（不得为写该标记再修改已验证 ref）。第 9 步之前的任何失败都保持线上上一成功版本不变或 ref 仍不存在；若这是 safety 任务，则必须报警“尚未完成下架”，不能报告成功。

候选内容和成功状态都在临时 ref 上完成验证，`generated` 最后通过一次 ref 更新对外生效。Git ref 更新是原子的，但 CDN 缓存不是跨文件事务；本方案通过“单个根入口只引用不可变 release”保证客户端即使拿到旧根文件也继续使用完整旧 generation，不会跨代拼接。`concurrency`、exact-OID lease、更新前 ancestry 证明和服务端 ruleset 共同保证不覆盖人工或并发修改；除精确删除外，所有成功更新在提交图上都必须是快进。

自动提交信息使用中文：

```text
chore: 自动更新 TVBox 资源（2026-07-22）
```

### 16.2 回滚

- 当前成功状态的 `previous_release_head_sha` 是回滚目标 `target_head`；正常内容发布时它与当前根 manifest 的 `previous_commit_sha` 相等，回滚状态下只以前者为准。执行前必须确认目标 commit 的 `state/release.json` 为 `success`，其 release 目录存在且全部哈希通过，并重新对目标根入口执行当前 denylist/`mandatory_removal_set` 扫描。目标仍含任一强制移除项时禁止回滚；安全发布失败按第 16.1 第 12 步保留安全 ref，而不是恢复违规旧版。
- 一次发布包含内容提交和确认提交，禁止只 `git revert HEAD`，否则可能留下新内容并恢复出 `pending` 状态。回滚从当前 `bad_head` 创建临时候选：把目标 commit 的根级 `index/warehouse/configs/stable/live/manifest/health` 逐字节恢复，同时保留当前树中所有历史 `dist/releases/`；若目标 release 目录缺失则从 `target_head` 补回。
- 回滚不逐字节恢复旧状态或旧报告。它创建新的 `success` 状态：`release_kind = rollback`、`generation = bad_state.generation + 1`、`active_release_id = target_state.active_release_id`、`content_commit_sha = target_state.content_commit_sha`、`previous_release_head_sha = bad_head`、`last_publish_at = now`、`last_success_at = target_state.last_success_at`，并用本轮 `workflow_run_id/workflow_run_attempt` 生成回滚报告。根 manifest 与 release manifest 逐字节保留目标内容的 `content_workflow_run_id/content_workflow_run_attempt`；两组身份不同是合法且必须保留的映射。保留目标旧成功时间，使因技术失败触发的回滚可在下一次日调度继续重试，同时单调 generation 防止目录名碰撞。
- 验证恢复后的三个根入口只引用目标 `active_release_id`、新状态为 `success`、不存在任何客户端入口引用 `pending` generation，再创建一个父提交为 `bad_head` 的普通补偿提交，提交信息为 `revert: 回滚 TVBox 自动发布（<workflow_run_id>/<workflow_run_attempt>）`。该提交不改写历史。
- 推送前再次确认远端 HEAD 仍为 `bad_head`，证明补偿提交是它的后代，再以绑定 `bad_head` 的 exact-OID lease 完成快进；随后执行第 16.1 第 10 步同样的 commit URL 与浮动分支 URL 回读。CAS、ancestry 不符或回读失败时停止自动处置并报警，不得进行非快进更新。
- 普通技术回滚保留已生成但不再引用的 release，使已经缓存新根入口的客户端仍能完成同代读取。因安全事件或第 18 节下架而回滚时，必须额外从当前分支删除所有引用受影响来源的 release；这会优先停止服务而非维持旧缓存兼容。若还需清理 Git 历史，作为人工应急流程处理，不由 Action 自动改写历史。
- 稳定用户 URL 始终不变，因此回滚后无需重新配置地址。

### 16.3 首次 bootstrap 撤销

首次 bootstrap 没有 `previous_commit_sha`，不能套用 16.2。若新建 `generated` ref 后第 10 步仍失败，必须：

1. 验证本轮 `expected_previous_head == null`，候选状态为 `release_kind: bootstrap`、`generation: 1`、`workflow_run_id/workflow_run_attempt` 二元组等于本轮，且远端 ref 仍精确等于 `final_candidate_sha`。
2. 使用带精确期望 SHA 的原子 lease 删除新 ref；等价 Git 命令为 `git push --force-with-lease=refs/heads/generated:<final_candidate_sha> origin :refs/heads/generated`。这是 V1 唯一允许的非快进破坏性操作，只能删除本轮刚创建且尚未通过验收的首版 ref。其他 `--force-with-lease` 调用仅用于 exact-OID CAS，必须先证明提交图快进，不能更新或改写任何已有成功历史。
3. 再次从 Git ref API/`ls-remote` 确认 `generated` 不存在，保留候选 ref 和 Action artifact 供诊断，并报告 bootstrap 失败；下一次仍只能由人工 `bootstrap=true` 重试。

若远端 ref 已不等于本轮 SHA，或 ruleset 拒绝精确 lease 删除，自动任务不得删除、覆盖或创建替代提交，必须高优先级报警。仓库上线前必须配置同一 active ruleset 同时覆盖 `generated` 与 `candidate/run-preflight-*`，再用测试 ref 验证 workflow 身份具备“创建 → exact-OID 快进更新 → 非快进强推被 GitHub 明确拒绝 → 精确 lease 删除”的完整权限。

## 17. 报告与可观测性

### 17.1 `dist/health.json`

`health.json` 必须保存第 13.3 节的来源、站点、全局频道和 URL 关系，不能只给每个仓库一个总状态。`sources[].items` 保存来源局部站点/URL，顶层 `channels[]` 通过 URL ID 建立跨来源频道聚合，任何实体 ID 只能定义一次。最小示例：

```json
{
  "schema_version": "1.0.0",
  "generated_at": "2026-07-22T12:00:00Z",
  "generation": 1,
  "release_id": "g00000001",
  "sources": [
    {
      "entity_id": "source:ikun-vod",
      "source_id": "ikun-vod",
      "technical_status": "healthy",
      "publication_status": "stable",
      "rights_status": "public_unverified",
      "last_checked_at": "2026-07-22T12:00:00Z",
      "upstream_revision": "sha256:<hex>",
      "items": [
        {
          "entity_type": "vod_site",
          "entity_id": "vod:ikun-vod:0123456789abcdef",
          "technical_status": "healthy",
          "publication_status": "stable",
          "last_success_at": "2026-07-22T12:00:00Z",
          "consecutive_successes": 1,
          "consecutive_failures": 0,
          "capabilities": {
            "home": true,
            "search": true,
            "detail": true,
            "play": true,
            "media_probe": true
          },
          "failure_reason": null
        }
      ]
    },
    {
      "entity_id": "source:iptv-org-cn-cctv",
      "source_id": "iptv-org-cn-cctv",
      "technical_status": "healthy",
      "publication_status": "stable",
      "rights_status": "public_unverified",
      "last_checked_at": "2026-07-22T12:00:00Z",
      "upstream_revision": "061e61103edeab1ecd1bc4d99cb38992f003653e",
      "items": [
        {
          "entity_type": "live_url",
          "entity_id": "live-url:iptv-org-cn-cctv:0011223344556677",
          "channel_id": "channel:fedcba9876543210",
          "technical_status": "healthy",
          "publication_status": "stable",
          "last_success_at": "2026-07-22T12:00:00Z",
          "consecutive_successes": 1,
          "media_path_score": 2,
          "response_ms": 320,
          "width": 1920,
          "height": 1080,
          "bandwidth": 5000000,
          "failure_reason": null
        }
      ]
    }
  ],
  "channels": [
    {
      "entity_id": "channel:fedcba9876543210",
      "identity_basis": "tvg_id",
      "normalized_identity": "cctv1.cn",
      "technical_status": "healthy",
      "publication_status": "stable",
      "rights_status": "public_unverified",
      "selected_url_id": "live-url:iptv-org-cn-cctv:0011223344556677",
      "candidate_url_ids": [
        "live-url:iptv-org-cn-cctv:0011223344556677"
      ]
    }
  ]
}
```

已知来源在新一轮中消失时仍要保留一条历史记录，并把 `failure_reason` 标记为 `missing_upstream`，否则差异闸门无法区分“删除”和“从未存在”。

### 17.2 `reports/latest.md`

必须展示：

- 本轮是否 due、是否强制执行、开始与结束时间；
- 本轮 `release_kind`、状态事件 `generation`、`active_release_id` 与 `recovery_due`；
- 本轮 `workflow_run_id/workflow_run_attempt` 二元组和候选 ref；
- 当前两个 manifest 的 `content_workflow_run_id/content_workflow_run_attempt` 内容来源二元组；回滚报告必须同时展示事件身份和复用的目标内容身份；
- 新增、恢复、降级、隔离、死亡和拒绝来源；
- 点播站点和直播频道数量变化；
- `verified/open_license/public_unverified` 数量；
- 未核实来源风险提示；
- 批量失败闸门结果；
- 上一版本 `generated` HEAD、新版本 `content_commit_sha`；本轮最终 `generated` HEAD 由分支 ref 和 Action 运行摘要记录，不能自引用写入同一确认提交；
- 失败原因统计，但不得泄露敏感 URL 参数。

Action artifact 保存机器可读报告和调试日志 `30` 天；长期历史由 Git 提交与 `latest` 报告承担。

同目录 `dist/reports/latest.json` 是 Markdown 报告的机器可读真源，必须通过 `schemas/report.schema.json`，字段至少包含 `schema_version/status/release_kind/generation/active_release_id/workflow_run_id/workflow_run_attempt/started_at/finished_at/due/forced/recovery_due/previous_release_head_sha/content_commit_sha/candidate_ref/content_identity/counts/gate/failures/sources`。`counts` 固定包含点播站点、直播频道和各权利状态的前后数量；`gate` 固定包含每个闸门的输入、阈值与结论；`failures` 只按原因枚举计数，不含 URL 参数值；`sources[]` 按 `source_id` 排序并保存技术/发布/权利状态及脱敏变化摘要。确认提交只能补齐允许变化的发布字段，不能重算健康结论。

`reports/candidates.json` 只存在于 `collect-and-validate` 的 Action artifact，不提交到 `generated`。它至少包含本轮事件身份、父 catalog 的 reviewed/resolved revision，以及按 `candidate_id` 排序的候选数组；每个候选保存 `candidate_id/kind/normalized_target_hash/technical_status/rights_status/publication_status/evidence_locations[]/failure_reason/secondary_reasons[]`。公开 URL 只在已经通过无凭证检查时保存；否则仅保存哈希和命中的参数名。`rights_status: unknown` 的候选必须由 Schema/发布 Bundle 验证器双重断言不出现在任何客户端产物。

## 18. 下架与来源维护

- README 提供明确的 Issue 下架入口。
- `sources/denylist.yaml` 记录来源 ID、匹配域名/URL、原因、请求日期和可公开证据。
- denylist 固定为 `version: 1` 与 `entries[]`；每项必须显式包含
  `id/source_ids/hosts/urls/reason/requested_at/evidence_urls`，三个匹配数组中至少一个非空。
  `reason` 仅允许 `takedown/rights_restricted/security`，主机名使用精确小写 DNS 名，URL
  只允许无凭证 HTTPS。命中来源 ID、来源允许主机、登记 URL，或上一成功健康图中直播 URL 的
  任一项时，整项来源进入强制移除集；已从 registry 删除但仍存在于历史健康图的 source ID
  仍然有效，不能因当前无法抓取而忽略。
- 收到有效请求后，维护者可执行 `workflow_dispatch(force=true)` 立即刷新，不等待 10 天。
- denylist 优先于 registry；命中项不得被自动发现重新加入。
- 被移除来源仍可在脱敏后的历史报告中保留状态，但根入口和当前 release 不得继续引用其入口。
- 普通刷新保留历史 release；有效安全/下架请求是例外，必须扫描当前分支全部 `dist/releases/**`，删除所有仍引用被下架来源、域名或 URL 的 release，再按第 16 节发布并验证旧路径已无法通过浮动 `generated` ref 读取。Git 历史中的旧 blob 不会被普通提交擦除；若请求范围要求历史清除，维护者必须暂停 Action，另走 GitHub 支持/历史重写的人工法律处置，本规格不让自动任务 force push。
- 若上游从“未知”变为明确禁止再分发，应从 `public_unverified` 转为 `restricted` 并移除。

## 19. 测试策略

### 19.1 单元测试

- `urls`、`storeHouse`、标准 TVBox config、M3U/TXT 解析；
- 相对 URL 绝对化；
- URL 去重、key 冲突改写和稳定排序；
- 私网、危险 scheme、重定向和 DNS rebinding 拒绝；
- URL/Header/Cookie 凭证识别与逐实体拒绝，包括公开可见的 `auth=testpub`；
- `category_policy.deny_names` 到 `sites[].categories` 浏览白名单的确定性生成；
- 客户端产物字段白名单与递归可执行依赖扫描，确保顶层 `spider`、站点 `jar/ext/header` 和嵌套脚本 URL 不残留；
- 权利状态与技术状态发布矩阵；
- reviewed/resolved snapshot 分离、条款哈希变化转 `terms_changed`；
- `kind × parser × fetch.mode × terms_watch.type` 合法组合及三个明确反例；
- 240 小时 due-check 边界；
- 批量失败比例和最低数量闸门；
- release manifest 与根 manifest 的独立哈希闭包、自身排除、稳定排序，以及根入口切换后旧 release manifest 仍可独立验真；
- `generation` 单调递增、`active_release_id` 可安全回指旧版、根入口单代闭包、历史 release 不可变，以及普通刷新/安全删除两种保留策略；
- `tvg-id` 的 NFKC/trim/ASCII 小写归一化、无 `tvg-id` 时的来源隔离，以及直播候选完整排序键的确定性；

### 19.2 集成测试

使用本地 fixture HTTP 服务，不依赖真实影视源，覆盖：

- 正常 XML/JSON 点播链路；
- 搜索为空、详情缺字段和播放地址损坏；
- 429、5xx、超时、404/410、重定向循环；
- HTML 验证码冒充 200；
- 跟踪 ref 前移但条款不变时读取同一 resolved commit，以及条款变化时禁止读取混合快照；
- 正常 M3U、损坏 M3U、HLS 主/媒体列表和有限分片；
- TVBox `header`、M3U `#EXTHTTP/#EXTVLCOPT`、URL 行尾 Header 的白名单解析，以及 `X-API-Key`、Cookie、`header=` query 的逐实体拒绝；
- 声明合法自定义 Header 的实体执行独立双探针：无 Header 全链路通过时去掉 Header 后发布；只有带 Header 才通过时只进入报告，且所有客户端 JSON/M3U 均不残留 Header 字段或指令；
- 生成器 HTTP 例外可获取上游容器，但任何客户端可见 HTTP URL 均以 `client_http_disallowed` 拒绝且不猜测升级为 HTTPS；
- 私网重定向和超大响应；
- 一次大规模失败触发 `inconclusive`；
- 强制安全移除在类别归零时仍发布空安全配置，且不推进 `last_success_at`；
- safety 发布失败时禁止回到仍命中当前 `mandatory_removal_set` 的目标；已提升的安全 SHA 遇到浮动 Raw 超时仍保持在线并标记 `delivery_unverified`；
- publish 前后远端分支发生变化。
- 两代 release 切换时旧根、新根分别只加载本代子文件；
- 两提交发布后的补偿回滚恢复成功状态且不暴露 `pending`。
- `generated` 不存在时的 orphan bootstrap、提升后精确 lease 删除，以及 ref 已变化时禁止删除；
- 相同 `run_id` 的下一 `run_attempt` 在上一 attempt 候选 ref 仍保留时可使用独立候选 ref 完成发布；状态/报告/ref 的事件 attempt 必须一致，新内容发布的 manifest 内容 attempt 还必须等于该事件 attempt；
- 新内容发布时内容来源身份等于事件身份；回滚时 manifest 保留目标旧内容身份、状态/报告/ref 使用本轮事件身份，交换或混用任一字段都会失败；

### 19.3 兼容验收

每次更改输出协议时，至少使用兼容基线验证：

- FongMi 能导入 `index.json`、保存线路并加载第一条；
- TVBoxOS 能导入 `index.json` 并在稳定配置内聚合搜索；
- 支持多仓的客户端能展开 `warehouse.json`；
- 旧客户端能直接导入 `configs/stable.json`；
- 三类客户端均能读取外部 M3U 直播。

## 20. 验收标准

### AC-001 多线路入口

给定合法生成物，当目标 TVBoxOS 或 FongMi 导入 `dist/index.json`，则能看到至少一个线路，且第一条“DS 稳定聚合”加载成功。

### AC-002 多仓兼容

给定 `dist/warehouse.json`，当支持 `storeHouse` 的客户端导入时，则可以展开稳定仓和公共实验仓，并继续加载各自 `urls[]`。

### AC-003 当前配置内聚合搜索

给定稳定配置包含至少一个健康且 `searchable: 1` 的站点，当用户搜索健康检查使用的已知标题时，则客户端返回结果，并可继续取得详情和播放地址。

### AC-004 不虚构跨仓搜索

当用户切换到某一独立配置线路时，搜索范围只承诺覆盖该线路的 `sites[]`；文档不得声称 `urls` 或 `storeHouse` 会自动跨所有仓搜索。

### AC-005 直播播放

给定 `dist/live/stable.m3u`，至少一个频道可从频道 URL 读取 HLS manifest 和有限媒体分片；无效地址不出现在新 M3U 中。

### AC-006 点播失效过滤

给定一个 404 API、一个返回 HTML 验证码的 API 和一个详情协议损坏的 API，当刷新运行时，三者均不进入候选稳定产物，报告分别记录稳定失败原因。

### AC-007 Spider 隔离

给定任意远程 JAR/JS/Python Spider，当刷新运行时，工作流不执行它，也不把其配置写入 index、depot、稳定配置或独立实验线路；该来源技术状态最高为 `partial`、发布状态为 `rejected`，只出现在脱敏报告中。生成物递归断言不存在顶层 `spider`、站点 `jar/ext/header`、`type: 3` 或嵌套可执行依赖 URL。未来若要支持可信 Spider，必须另立版本规格和安全评审，不属于 V1。

### AC-008 权利状态

给定一个公开可访问但无许可证证据的来源，当 `type: 0/1` 全链路验效达到 `healthy` 时，按 D-006 自动进入技术稳定聚合、名称带 `⚠️` 前缀并标记 `public_unverified`；只达到 `partial` 时只进入公共实验仓。两者均不得展示为 `verified` 或 `open_license`。

### AC-009 硬性拒绝

给定明确禁止再分发、需要凭证、绕过 DRM 或已在 denylist 的来源，即使技术可访问，也不得进入任何公开产物。给定一份混合 M3U，其中部分 URL 带 `auth=testpub` 或仅有 HTTP 播放地址，则这些 URL 逐项拒绝，其他无凭证 HTTPS URL 继续验效；报告不得泄露参数值，也不得把 HTTP 猜测改写为 HTTPS。

### AC-010 十天调度

给定当前 kind 为 `bootstrap/regular` 且距离上次完整成功 `239h59m`，计划任务只执行 due-check 并成功跳过；给定 `>=240h`，执行完整刷新；给定刷新失败，`last_success_at` 保持不变。给定当前 kind 为 `safety/rollback`，无论该时间差多小，下一次每日调度都设置 `recovery_due` 并尝试完整恢复，直至发布 `regular` 成功版。

### AC-011 手动刷新

给定 `workflow_dispatch(force=true)`，无论是否满 240 小时都执行完整刷新；仍必须通过全部发布闸门。

### AC-012 批量失败保护

给定上一成功版本的点播基线或直播基线至少有 5 个实体，当本轮对应集合失败比例超过 20% 时，本轮标记 `inconclusive`，`generated` ref SHA 不变。点播基线只含实际发布站点；直播基线只含已发布频道在上一成功 health 中的健康所选/备选 URL，未健康或安全拒绝 URL 不得进入分母。

### AC-013 权限最小化

除 `publish` Job 外，任何 Job 都没有 `contents: write`；第三方 Action 均固定完整 commit SHA；不使用 PAT。

### AC-014 两阶段发布与回读

给定 `bootstrap/regular/safety` 新内容发布全部检查通过，候选内容提交先以 `pending` 通过 exact-OID lease 推送，并按 `content_commit_sha` 回读根 manifest、release manifest、完整 release-local `artifacts` 和根 `aliases`；哈希一致后才创建只改状态和报告的 `success` 确认提交。确认提交回读成功后仍不得结束：常规发布必须再次确认远端 `generated` 等于 `expected_previous_head`、证明新提交为其后代，再通过绑定旧 SHA 的 lease 完成快进，bootstrap 按 AC-016 新建 ref；随后先按 commit SHA、再按三个用户实际配置且完全不带 query/fragment 的裸浮动分支 URL 回读，并以裸 URL 跟随全部当前 release 引用。120 秒窗口内必须满足四方 active/release ID 等式、manifest 内容身份与状态/报告事件身份一致、哈希匹配且状态成功；cache-busting URL 只能诊断，不能满足验收。提升前失败保持旧 SHA/ref 不存在；提升后 regular 失败走安全目标检查后的补偿回滚，safety 保持安全 SHA，bootstrap 走精确 lease 撤销。

### AC-015 可回滚

给定一次包含内容提交和确认提交的已发布版本，当执行回滚测试时，以当前 `bad_head` 为父提交创建一个普通补偿提交：恢复当前成功状态 `previous_release_head_sha` 对应版本的根入口和内容别名，创建使用本轮事件身份的 `rollback/success` 状态与报告，`generation` 继续递增而 `active_release_id` 指回目标；两个 manifest 逐字节保留目标 release 的旧内容身份，且根/release 内容身份相等。流程保留正常历史 release，不产生或引用 `pending`。远端 HEAD CAS 仍为 `bad_head` 且补偿提交是其后代时，以 exact-OID lease 完成快进，commit URL 与裸浮动分支 URL 回读均按上述两类身份映射通过；稳定 Raw URL 不改变且客户端加载目标 release。仅 revert 确认提交、恢复旧 generation 状态，或把旧 manifest 改写成本轮事件身份均不满足本验收。

### AC-016 首次发布

给定 `generated` ref 不存在，定时任务不得擅自创建首版；人工 `bootstrap=true` 在开始与提升前均确认 ref 仍不存在，重新验证真实点播和直播种子，通过所有绝对闸门后以期望空 ref 的 exact-OID lease 新建该 ref，并生成 `generation: 1`、`active_release_id: g00000001`。首次发布跳过相对失败比例闸门，但不降低最低数量、安全或回读要求。给定新 ref 提升后浮动 URL 验证失败且 ref 仍精确等于本轮 SHA，任务使用精确 `--force-with-lease` 删除该未验收首版并确认 ref 不存在；SHA 已变化时不得删除。给定 ref 已存在但状态非法，bootstrap 必须失败而非覆盖。

### AC-017 Header 与凭证注入防护

给定仅声明白名单 `User-Agent/Referer` 的 TVBox 或 M3U 项，解析器先完全移除来源 Header 做正常全链路探针：若通过，则丢弃 Header 后按其他准入规则发布；若失败，再使用规范化白名单 Header 做独立诊断探针，只有带 Header 才通过时实体必须为 `withheld/client_header_unsupported`。两种情况下生成 JSON/M3U 都不存在任何 Header 字段或指令。给定 `X-API-Key`、`X-Auth-Token`、Cookie、未知 `#EXTVLCOPT`、畸形 `#EXTHTTP` 或 `header=` query，解析器直接拒绝对应实体并记录脱敏原因，不发出携带该 Header 的网络请求。若命中的是来源入口，则拒绝整个来源。

### AC-018 跨文件代际一致性

给定已发布 `g00000001` 后再发布 `g00000002`，持有旧 `index.json`、旧 `warehouse.json` 或旧根级 stable 内容的客户端继续请求时，其后续 URL 全部落在仍保留的 `g00000001`；重新获取根入口的客户端全部落在 `g00000002`。任何一个入口文件都不得同时引用两个 release，也不得依赖其他根级浮动资源完成播放。

### AC-019 安全移除不被可用性闸门阻止

给定当前唯一点播源进入 denylist、`restricted/takedown/terms_changed` 或被确认含安全违规，即使删除后点播数量为零且来源网络/非 GitHub 公共探针失败，任务也必须从上一成功 release 派生 `safety` release，移除该来源并发布 Schema 合法的空安全配置；`last_publish_at` 更新、`last_success_at` 不变。技术 404/超时导致的归零仍必须走 `inconclusive`，不能滥用本例外。给定 GitHub Git/Raw 交付面不可用，则任务失败报警且不得报告已下架；若安全 commit 已提升但三个用户实际使用的裸浮动 Raw URL 回读超时，`generated` 仍必须保持在不含强制移除项的 safety SHA，不能回滚到违规目标，也不能用带 query 的回读结果宣称交付成功。

### AC-020 直播全局归并与确定择优

给定两个来源含相同规范化 `tvg-id` 的多个健康 URL，输出只含一个全局频道，并严格按连续成功、媒体链路分、响应中位数、明确质量分、URL 字典序选择唯一地址；缺失质量元数据固定得 `1` 分。给定两个来源只有同名但均无 `tvg-id`，不得跨来源猜测合并。相同输入重复运行必须选择同一 URL。

### AC-021 来源组合 Schema

给定 `direct_url + terms_watch.github_path`、`vod_site + tvbox_json`、`repository_catalog + direct_url` 三个注册项，或给定 catalog 内层 `parsers_by_glob[].parser: maccms_json`，Schema 在任何网络请求前全部拒绝；给定 9.1 矩阵中的合法组合则通过结构校验。

### AC-022 Action 重跑隔离

给定同一 `workflow_run_id` 的 attempt 1 已遗留 `candidate/run-<id>-attempt-1`，当 GitHub 以 `workflow_run_attempt: 2` 重跑新内容发布时，任务创建独立的 `candidate/run-<id>-attempt-2`，不覆盖或删除 attempt 1，并可按正常流程完成发布。状态/报告的事件身份必须等于候选 ref 的 attempt 2，两个 manifest 的内容身份必须相等；本次是新内容发布，因此内容身份还必须等于 attempt 2。给定回滚重跑，则状态/报告/候选 ref 仍使用 attempt 2，但 manifest 保留目标 release 的旧内容身份。任一不符合相应映射时发布失败。

## 21. 实施分期

### Phase 1：仓库与协议骨架

- 创建公开仓库 `azhansy/ds-tvbox` 和 `main` 分支，并预先配置同一 ruleset 覆盖 `generated` 与 `candidate/run-preflight-*`；实际 `generated` ref 只由首次人工 bootstrap 创建；
- 建立 Schema、来源注册表和 fixture；
- 生成带不可变 `release_id` 的 index、warehouse、stable、M3U 和三个根入口别名；
- 完成确定性输出和基础兼容测试。

### Phase 2：健康检查

- 实现直接点播 API 全链路检查；
- 实现直播 M3U/HLS 有限探测；
- 实现技术状态机、失败原因和批量失败闸门；
- 生成 health、manifest 和报告。

### Phase 3：自动发布

- 实现每日 due-check 和 240 小时门槛；
- 权限隔离、artifact 校验、generated 分支提交和 Raw 回读；
- 实现手动强制刷新、浮动分支 URL 回读和普通补偿提交回滚测试。

### Phase 4：来源扩展

- 按注册表逐项加入公开点播候选和直播候选；
- 权利清晰来源通过验效后进入稳定仓；`public_unverified` 严格按 9.2 唯一矩阵自动分流，不再留人工任选分叉；
- 对可信 Spider 另立版本规格和安全评审；V1 不扩大发布范围或 Job 权限。

## 22. 前置条件与上线清单

实现前必须完成：

- [x] 创建公开 GitHub 仓库 `azhansy/ds-tvbox`；
- [ ] 默认分支设置为 `main`；
- [x] 在 `generated` ref 尚不存在时，预先配置同一 active ruleset 覆盖 `generated` 与 `candidate/run-preflight-*`；
- [x] 允许 Action 使用 `GITHUB_TOKEN` 写 `generated` 分支；
- [ ] 用测试 ref 验证 workflow 可创建、exact-OID 快进更新、被 GitHub 明确拒绝非快进强推，并以精确 lease 删除未验收 ref；
- [x] 对 `.github/workflows/**` 设置 CODEOWNERS 或等价审核规则；
- [x] 在 registry 中登记至少一个点播候选和一个直播候选；
- [x] README 明确标注 `public_unverified` 含义、风险和下架入口；
- [ ] 在真实目标客户端上完成首次人工兼容验收；
- [ ] 首次发布后保存三个稳定 Raw URL 和 commit SHA 证据。

## 23. 调研依据

- [FongMi/TV 配置格式说明](https://github.com/FongMi/TV/blob/5fdff00a602dc56e8ba756174daef20edab024f2/docs/CONFIG.md)
- [FongMi/TV 直播格式说明](https://github.com/FongMi/TV/blob/5fdff00a602dc56e8ba756174daef20edab024f2/docs/LIVE.md)
- [FongMi/TV 直接 API 请求契约](https://github.com/FongMi/TV/blob/5fdff00a602dc56e8ba756174daef20edab024f2/app/src/main/java/com/fongmi/android/tv/api/SiteApi.java#L47-L209)
- [FongMi/TV XML/JSON 响应模型](https://github.com/FongMi/TV/blob/5fdff00a602dc56e8ba756174daef20edab024f2/app/src/main/java/com/fongmi/android/tv/bean/Result.java)
- [FongMi/TV `categories` 字段与客户端处理](https://github.com/FongMi/TV/blob/5fdff00a602dc56e8ba756174daef20edab024f2/app/src/main/java/com/fongmi/android/tv/api/SiteApi.java#L223-L245)
- [FongMi/TV `urls` Depot 解析](https://github.com/FongMi/TV/blob/5fdff00a602dc56e8ba756174daef20edab024f2/app/src/main/java/com/fongmi/android/tv/api/config/VodConfig.java#L123-L140)
- [TVBoxOS `urls` 解析](https://github.com/q215613905/TVBoxOS/blob/0409954033a44582b431d89934e3980900f4a265/app/src/main/java/com/github/tvbox/osc/api/ApiConfig.java#L670-L701)
- [TVBoxOS 当前配置内搜索源筛选](https://github.com/q215613905/TVBoxOS/blob/0409954033a44582b431d89934e3980900f4a265/app/src/main/java/com/github/tvbox/osc/api/ApiConfig.java#L1720-L1732)
- [TVBoxOS `categories` 配置字段](https://github.com/q215613905/TVBoxOS/blob/0409954033a44582b431d89934e3980900f4a265/app/src/main/java/com/github/tvbox/osc/bean/SourceBean.java)
- [mlabalabala/box 多仓说明](https://github.com/mlabalabala/box/blob/c5dc2b98569e829650dc680ae2588299fad79542/README.md)
- [mlabalabala/box `storeHouse` 实现](https://github.com/mlabalabala/box/blob/c5dc2b98569e829650dc680ae2588299fad79542/app/src/main/java/com/github/tvbox/osc/bbox/api/StoreApiConfig.java#L75-L188)
- [Guovin/iptv-api 直播聚合与验效能力](https://github.com/Guovin/iptv-api/blob/50efc7c0d4b39fc24d98d8bc5d8c11f14947effc/README.md)
- [点播种子发现快照](https://github.com/miru-project/repo/blob/651821e2ea4213b48040e84c5e851f7c2c1082db/repo/vod.api.json.collection.js#L29)
- [iKun 官方采集帮助](https://www.ikunzy.com/ikun/help.html)
- [直播种子快照](https://github.com/iptv-org/iptv/blob/061e61103edeab1ecd1bc4d99cb38992f003653e/streams/cn_cctv.m3u)
- [iptv-org/iptv 数据许可证](https://github.com/iptv-org/iptv/blob/061e61103edeab1ecd1bc4d99cb38992f003653e/LICENSE)
- [GitHub Actions `schedule` 语义](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub Actions 工作流权限](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions)
- [GitHub Actions 安全使用](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHub 仓库许可证说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)
