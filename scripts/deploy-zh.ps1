# =============================================================
#  deploy-zh.ps1 — 把 E:\ClaudeZhCN 的中文补丁部署到官方 Claude 安装
#  用法: powershell -ExecutionPolicy Bypass -File E:\ClaudeZhCN\deploy-zh.ps1
#
#  做的事:
#   1. 自动定位官方最新 app-<版本> 目录
#   2. 停止正在运行的 Claude (文件占用)
#   3. 拷贝翻译产物:
#        ion-dist/i18n/en-US.json          (SPA 主表, 中文覆盖)
#        ion-dist/i18n/dynamic/en-US.json  (动态表, 中文覆盖)
#        ion-dist/skill-display-map.json   (skill 显示映射)
#        ion-dist/skill-display-patch.js   (前端补丁脚本)
#   4. 向 ion-dist/*.html 注入 <script src="./skill-display-patch.js"></script>
#      (幂等: 已注入则跳过)
#   5. 不动原生 en-US.json — v2.42304+ 官方自带 zh-Hans.json,
#      在 app 设置里把语言切到 中文(简体) 即可让原生菜单用官方中文
# =============================================================
param(
  # Claude 中文工作区根目录 (与 Claude/ 文件夹同级, 即本脚本所在仓库根)
  [string]$ClaudeZhDir = "E:\ClaudeZhCN",
  # 官方 Claude 安装根目录
  [string]$AppRoot = "$env:LOCALAPPDATA\AnthropicClaude"
)
$ErrorActionPreference = 'Stop'
$src    = Join-Path $ClaudeZhDir "Claude\resources"
$root   = $AppRoot

# --- 1. 定位最新 app 目录 ---
$latest = Get-ChildItem $root -Directory -Filter "app-*" |
          Sort-Object { [version]$_.Name.Substring(4) } -Descending |
          Select-Object -First 1
if (-not $latest) { throw "未找到官方安装目录" }
$dst = Join-Path $latest.FullName "resources"
Write-Host "目标: $($latest.Name)" -ForegroundColor Cyan

# --- 2. 停止运行中的 Claude ---
$procs = Get-Process -Name "claude*" -ErrorAction SilentlyContinue
if ($procs) {
  Write-Host "停止 $($procs.Count) 个 Claude 进程..."
  $procs | Stop-Process -Force
  Start-Sleep -Seconds 2
}

# --- 3. 拷贝翻译产物 ---
$copies = @(
  "ion-dist\i18n\en-US.json",
  "ion-dist\i18n\dynamic\en-US.json",
  "ion-dist\skill-display-map.json",
  "ion-dist\skill-display-patch.js"
)
foreach ($rel in $copies) {
  $from = Join-Path $src $rel
  $to   = Join-Path $dst $rel
  if (-not (Test-Path $from)) { Write-Host "  [跳过] 源不存在: $rel" -ForegroundColor Yellow; continue }
  $toDir = Split-Path $to -Parent
  if (-not (Test-Path $toDir)) { New-Item $toDir -ItemType Directory -Force | Out-Null }
  Copy-Item $from $to -Force
  Write-Host "  [OK] $rel" -ForegroundColor Green
}

# --- 3.5 原生菜单: 官方 zh-Hans.json 覆盖 en-US.json (官方中文优先, 缺失回落英文) ---
$zhOfficial = Join-Path $dst "zh-Hans.json"
$nativeEn   = Join-Path $dst "en-US.json"
if ((Test-Path $zhOfficial) -and (Test-Path $nativeEn)) {
  python -c "
import json, collections
en = json.load(open(r'$nativeEn', encoding='utf-8'))
zh = json.load(open(r'$zhOfficial', encoding='utf-8'))
out = collections.OrderedDict()
n = 0
for k, v in en.items():
    zv = zh.get(k)
    if isinstance(zv, str) and zv.strip(): out[k] = zv; n += 1
    else: out[k] = v
json.dump(out, open(r'$nativeEn', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f'  [OK] native en-US.json: {n}/{len(en)} official Chinese')
"
}

# --- 4. 注入 patch.js 到所有 ion-dist/*.html (幂等) ---
$tag = '<script src="./skill-display-patch.js"></script>'
$rx = New-Object System.Text.RegularExpressions.Regex('</head>', 'IgnoreCase')
$injected = 0; $already = 0
Get-ChildItem (Join-Path $dst "ion-dist") -Filter "*.html" | ForEach-Object {
  $html = [System.IO.File]::ReadAllText($_.FullName)
  if ($html.Contains('skill-display-patch.js')) { $already++; return }
  if ($rx.IsMatch($html)) {
    $html = $rx.Replace($html, $tag + '</head>', 1)
    [System.IO.File]::WriteAllText($_.FullName, $html)
    $injected++
    Write-Host "  [injected] $($_.Name)" -ForegroundColor Green
  }
}
Write-Host ("注入: 新注入 {0} 个, 已有 {1} 个" -f $injected, $already)

# --- 5. 完成 ---
Write-Host ""
Write-Host "✔ 部署完成。提示:" -ForegroundColor Green
Write-Host "  - SPA 主界面中文 = 本次部署的 i18n 覆盖 (自动生效)"
Write-Host "  - 原生菜单中文   = 官方 zh-Hans, 请在 Claude 设置里把界面语言切到 中文(简体)"
$ans = Read-Host "现在启动 Claude 吗? (回车=启动, n=不启动)"
if ($ans -notmatch '^[nN]') { Start-Process (Join-Path $latest.FullName "claude.exe") }
