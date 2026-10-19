# 工作区目录结构 (Workspace Layout)

部署与翻译脚本假设你的工作区是如下结构 —— **脚本放在仓库根目录,
与 `Claude/` 工作副本同级**:

```
<你的工作区>/                     ← 本仓库 clone 位置
├── scripts/                      # 本仓库的脚本
├── Claude/                       # Claude 官方资源的工作副本 (核心!)
│   └── resources/
│       ├── en-US.json            # 原生菜单语言表 (会被覆盖为中文 + .en.bak 备份)
│       ├── zh-Hans.json          # 官方中文 (≥2.42304 自带, 原生层直接用)
│       ├── app.asar
│       └── ion-dist/
│           ├── i18n/
│           │   ├── en-US.json    # SPA 主语言表 (会被覆盖为中文 + .en.bak 备份)
│           │   ├── dynamic/      # v2.42304+ 动态串表
│           │   └── statsig/      # (旧版本才有, 新版已移除)
│           ├── skill-display-map.json   # 生成的显示名映射
│           └── skill-display-patch.js   # 内嵌 map 的前端补丁
├── baidu_i18n_cache.json         # 百度翻译缓存 (自动生成, 增量翻译的关键)
├── baidu_cache.json              # skill 映射翻译缓存
├── skipped_strings.txt           # 翻译失败保留英文的串 (人工复核用)
└── version.txt                   # 已汉化的 Claude 版本号
```

## 初始化工作副本

从官方安装目录(或 MSIX 包目录)把 `resources` 拷出来:

```bat
robocopy "%LOCALAPPDATA%\AnthropicClaude\app-<版本>\resources" "Claude\resources" /E
```

> 如果源目录是商店 MSIX 且带 EFS 加密,请改用 tar 管道(见 docs/pitfalls.md 第 1 条)。

## 生成 .en.bak 基线

首次运行 `gen_i18n_zh.py --run` 时会自动创建 `.en.bak`(纯净英文备份),
之后每次 Claude 更新,脚本会自动检测英文表变化并刷新基线、只翻新增串。

想还原英文?把 `.en.bak` 覆盖回同名文件即可。
