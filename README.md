# APX-plugin-auto

自动跟踪 APX 插件上游快照，逐文件校验后打包为 GitHub Release 的独立流水线。

- 上游数据源：`https://cnb.cool/erinilis/apx-plugin-releases/-/git/raw/main`（第三方插件仓库，非 APMM 作者维护；可用仓库变量 `APX_UPSTREAM_BASE` 替换）
- 运行环境：GitHub Actions（公开仓库免费额度足够），仅用 Python 标准库
- 产物：5 个平台包 + 1 个合集包 + `apx-index.json` / `latest.json` / `SHA256SUMS.txt`
- 触发：每 2 小时轮询、手动触发、`repository_dispatch`（`apx-upstream-updated`）

## 生态与仓库角色

| 仓库 | 角色 | 与本仓库的关系 |
| --- | --- | --- |
| [a190141074/APMM-next](https://github.com/a190141074/APMM-next) | 管理器源码，权威上游；GPL-3.0，非 fork | 只作为行为参照（`apx.rs` 的读取/校验规则），不引用代码、不依赖其 Release |
| [cnb.cool/astral-weird-egg/APMM-next](https://cnb.cool/astral-weird-egg/APMM-next) | 源码镜像（CNB 侧副本） | 不参与 |
| [cnb.cool/astral-weird-egg/astral-party-mod-manager-assets](https://cnb.cool/astral-weird-egg/astral-party-mod-manager-assets) | 管理器发行/资源仓库，只含安装包、插件包与清单 | 不参与；其中没有 APX 补丁负载 |
| [a190141074/AstralPartyModManager-Previews](https://github.com/a190141074/AstralPartyModManager-Previews) | 预览图资源，`preview-manifest-v4.json` 的来源 | 不参与（与 APX 无关） |
| [cnb.cool/erinilis/apx-plugin-releases](https://cnb.cool/erinilis/apx-plugin-releases) | **第三方** APX 插件发布，不属于 APMM 作者 | 本仓库唯一的数据源 |

这张表解释了一个容易混淆的点：**管理器作者并不生产 APX 插件**。插件由第三方作者 erinilis 构建并发布在 CNB，APMM 只是消费者。因此任何“跟着 APMM 发版自动产出 APX 插件”的设想都缺少数据来源；本仓库改为跟踪插件自身的快照。

## 它解决什么问题

上游 `apx-plugin-releases` 只是一个“最新快照输出目录”：每个平台一个 `manifest.json`，加若干 Addressables 补丁、还原文件和依赖 DLL，没有版本化的 Release，也不保留历史。本仓库把这套目录变成：

1. **可判定**：读取 5 个平台的 `manifest.json`，计算快照哈希，与 `state/state.json` 比较，没变就 5 秒结束；
2. **可验证**：下载每个文件后校验 `size` 与 `sha256`，任何一个不匹配就终止，不发布半成品；
3. **可分发**：按平台打成自包含 ZIP（含 `manifest.json`、`restore/restore_manifest.json`），附索引与校验和，发布为 Release；
4. **可追溯**：Release 标题、正文与索引都记录快照哈希，同一快照只会发布一次。

```text
schedule / dispatch
        │
        ▼
  fetch 5 × manifest.json ──► 快照哈希 == state ? ──是──► 结束
        │                                     │
        │                                    否
        ▼                                     ▼
  下载 files + restore_files          逐文件 size/sha256 校验
        │                                     │
        ▼                                     ▼
  写入 mirror/<平台>/…                打包 5 个平台 ZIP + 合集
        │                                     │
        ▼                                     ▼
  发布 Release（同快照幂等）  ──► 提交 state/state.json
```

## 与 APMM-next 的关系

**零依赖。** 这个仓库不引用、不构建、不修改 `a190141074/APMM-next` 的任何代码，也不需要它的 Release 产物。

原因是生态分工决定的：

- APMM 的 `src-tauri/src/apx.rs` 只做一件事——按固定前缀 `PLUGIN_REPO_RAW_PREFIX` 拉取 `<平台>/manifest.json` 与对应文件，然后安装到游戏目录；它本身**不生成** APX 补丁。
- APMM 的 Release 资产只有管理器本体（MSI / 便携 ZIP / 更新清单），**不包含** APX 插件负载。
- APX 补丁是 Unity Addressables 资源包，由**第三方插件作者 erinilis** 从游戏本体与改动资源构建，APMM 作者不参与。GitHub 托管 Runner 没有游戏文件，也无法“从 APMM 仓库重新生成”这些包。

所以可行的自动化只有一条：**跟踪插件上游快照 → 校验 → 重新打包 → 发布**。本仓库把这条链路做成无状态、可重放、可审计的流水线，并且不绑定 APMM 的版本节奏。

如果你以后有了自己的生成器（本机或自建 Runner），把产物按同样的目录结构放到任意静态站点，设置仓库变量 `APX_UPSTREAM_BASE` 指向它即可，工作流无需改动。

## 一次运行做了什么

| 步骤 | 行为 | 失败处理 |
| --- | --- | --- |
| 检查 | GET 5 个 `manifest.json`，校验 `format_version`、平台、文件数、总体积、`sha256` 格式、路径安全 | 任一清单非法 → 失败退出，不写状态 |
| 构建 | 并发下载（默认 4 线程），逐文件校验 `size` + `sha256`，写入 `mirror/` | 摘要不符 → 删除该文件并失败，Release 不创建 |
| 打包 | 每平台一个 ZIP（`<平台>/manifest.json` + 补丁 + 还原 + 还原清单），另出合集 ZIP | 缺文件 → 失败 |
| 发布 | 创建 `apx-<版本>` Release；同版本已存在则追加 `-2`、`-3`；同快照已发布则复用 | 发布失败 → 不写状态，下轮重试 |
| 记录 | 把快照、版本、Release tag 写入 `state/state.json` 并提交 | 提交失败 → 下轮靠“同快照复用”避免重复发布 |

`runtime_dependencies.json` 是个特殊项：上游清单里的 `size`/`sha256` 会滞后于实际文件（当前 3.3.0 快照即如此）。本仓库与 APMM 的处理保持一致——**不因该文件的摘要过期而判定失败**，改为校验 JSON 合法且 `platform` 匹配，并在 Release 正文里记录告警条数。

## 产物结构

```text
APX-CN_STEAM-3.3.0.zip
├── CN_STEAM/
│   ├── manifest.json
│   ├── com.unity.addressables/AssetBundles/…/__data
│   ├── com.unity.addressables/AssetBundles/…/__info
│   ├── dependence/APXLive2DBridge.dll
│   ├── dependence/Live2DCubismCore.dll
│   ├── dependence/apx_modern_assist_ui.bundle
│   ├── runtime_dependencies.json
│   └── restore/
│       ├── restore_manifest.json
│       └── com.unity.addressables/…/__data
└── …
```

解压后的顶层目录名与上游仓库完全一致，便于比对、镜像或自行部署。APMM 当前把上游地址写死在代码里，**不能直接读取 GitHub Release 附件**；如需让 APMM 使用本仓库，需要修改 `PLUGIN_REPO_RAW_PREFIX` 或改为手动导入。

## 手动使用

1. 在 [Releases](../../releases) 下载对应平台包（不确定就下 `APX-all-*.zip`）。
2. 校验：`sha256sum -c SHA256SUMS.txt`（Windows 可用 `Get-FileHash`）。
3. 部署：按包内 `manifest.json` 的 `target_type` / `target_path` 放置；`locallow` 指游戏 LocalLow 数据目录，`game_root` 指游戏安装根目录。**替换前务必备份**，`restore/` 下是官方原文件。

## 配置

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `APX_UPSTREAM_BASE` | 仓库变量（可选） | 上游 raw 根地址，默认 CNB；换成自己的镜像即可 |
| `GITHUB_TOKEN` | 自动注入 | 工作流声明 `contents: write`，只用于建 Release 和提交状态 |
| 轮询频率 | `.github/workflows/sync.yml` | 默认 `17 */2 * * *`（每 2 小时）；可改 cron 或只保留手动触发 |

首次运行前不需要任何 Secret。若要接外部事件，向上游仓库（或你自己的生成器）加一个 `repository_dispatch` 请求：

```bash
curl -X POST https://api.github.com/repos/qwqqvq123456/APX-plugin-auto/dispatches \
  -H "Authorization: Bearer <token>" \
  -H "Accept: application/vnd.github+json" \
  -d '{"event_type":"apx-upstream-updated"}'
```

## 本地运行

```bash
python tools/apx_sync.py check                     # 只读清单，判断是否变化
python tools/apx_sync.py check --force             # 强制视为变化
python tools/apx_sync.py build --out dist --mirror mirror \
  --expect-snapshot <check 输出的 snapshot>         # 下载 + 校验 + 打包
python tools/apx_sync.py verify --dir dist         # 校验资产摘要
python -m unittest discover -s tests -v            # 17 个单元测试
```

退出码：`0` 成功，`1` 校验/网络失败，`2` 构建期间上游发生变化（重跑即可）。

## 边界与免责声明

- 仓库**不生成、不修改、不签名**插件内容，只做镜像、校验与重新打包；文件版权归第三方插件作者 erinilis 与游戏厂商所有。
- 上游当前不提供签名清单，本仓库的信任基础是“上游 raw 内容 + 清单内 SHA-256”，不构成对插件安全性的独立审计。
- 上游 404、清单结构变更、摘要不符都会让流水线红灯，这是刻意设计：宁可停下，也不发布未经验证的包。
- 若上游删除或变更内容，已发布的历史 Release 不会自动撤回；需要时手动删除。

## 许可

本仓库的脚本与文档使用 MIT。打包产物不适用 MIT，见 [NOTICE.md](NOTICE.md)。
