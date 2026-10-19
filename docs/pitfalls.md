# 踩坑实录 (Pitfalls)

> 每一条都在真实机器上踩过、诊断过、解决过。按"发现顺序"而非重要性排列。
> 如果你在做类似的多语言补丁 / Electron 资源替换 / Windows 自动化,这页能帮你省下几十个小时。

---

## 1. EFS 加密资源目录:robocopy 静默复制 0 个文件

- **场景**:新版 Claude 通过 Microsoft Store 安装后,`C:\Program Files\WindowsApps` 下的包目录带有
  EFS 加密属性(`cipher /c` 可见 "New files added to this directory will be encrypted")。
- **症状**:
  - `robocopy src dst /MIR` **不报错但复制 0 个文件**(枚举阶段静默跳过整个加密目录);
  - `Copy-Item -Recurse` 会把源目录的加密属性传播到目标盘,若目标盘无法加密则**每个文件写入失败**,
    错误信息 `The specified file could not be encrypted`。
- **解法**:tar 字节流管道(Windows 10+ 自带 bsdtar):

  ```bat
  tar -C "%SRC%" -cf - app assets | tar -C "%DEST%" -xf -
  ```

  读端透明解密、写端不传播加密属性。实测 6,126 个文件 30 秒完成。

---

## 2. 商店/Squirrel 更新接口 403:不是坏了,是区域封锁

- **症状**:Squirrel 自动更新日志里 `CheckForUpdateImpl: Failed to download ... (403)`,
  连续数周无更新;直连 `api.anthropic.com` 的 RELEASES 接口同样 403。
- **诊断**:`curl` 直连 403,`curl --proxy http://127.0.0.1:7897` 同一 URL 返回 200 ——
  纯区域封锁,与客户端指纹无关。
- **解法**:.NET 的 WebClient 默认走系统代理,所以只要系统代理开着,
  `Update.exe --update <feed>` **本身就能成功**。曾经"坏了"的那段时间,
  只是因为代理没开。
- **附带坑**:Squirrel 的 stub `claude.exe`(363KB 引导器)会**吞掉**命令行参数,
  `claude.exe --remote-debugging-port=9223` 不会传给真正的 Electron 主程序。
  需要调试参数时直接调用 `app-<version>\claude.exe`。

---

## 3. 百度翻译 52002 "毒批次":整批翻译必失败

- **症状**:批量翻译在第 N 批稳定触发 `52002:SYSTEM ERROR`,重试 5 次全灭;
  换时间重跑还是在同一批失败,但单独翻一个 "hello world" 正常。
- **定性**:批次里存在某个/某些触发服务端异常的"毒串",整批连坐。
- **解法**:整批失败后**二分拆解**——不断对半分到单条,能翻的翻,
  翻不了的记入 `skipped_strings.txt` 并保留英文。
  单条失败时把重试次数降为 2(否则每条毒串要烧 2-3 分钟的重试退避)。

---

## 4. `Start-Sleep 1200`:PowerShell 的 Start-Sleep 单位是秒

- **症状**:GUI 自动化脚本"卡死"在某一"Hno",日志不再推进,进程活着但不干活。
- **真相**:`Start-Sleep 1200` 是**睡 1200 秒(20 分钟)**,不是 1.2 秒。
  想要毫秒必须显式 `Start-Sleep -Milliseconds 1200`。
- **排查技巧**:`Set-PSDebug -Trace 1` 逐行跟踪,看最后执行的语句停在哪个 sleep。

---

## 5. Windows 计划任务没有交互桌面

- **症状**:`schtasks` 创建的任务里跑 GUI 自动化(SetForegroundWindow / SendKeys /
  PrintWindow / 鼠标点击),全部静默无效或阻塞。
- **原因**:未加 `/it`(interactive)标志的任务可能运行在非交互桌面,没有可用的窗口站。
- **解法**:
  - 建任务时加 `/it`;
  - 或者让脚本只做无桌面依赖的部分(GDI 文件写入、HTTP),GUI 部分由交互会话执行。
- **连带坑**:PowerShell 的 `Add-Type` 依赖 Roslyn 编译服务(VBCSCompiler),
  该服务卡死会让**所有后续 Add-Type 永久挂起**。杀掉 VBCSCompiler 进程即恢复。

---

## 6. Electron asar 完整性校验:改包即拒启

- **场景**:想把主程序包里的硬编码英文(如开发者调试菜单项)改成中文,
  直接对 `app.asar` 做了**等长字节替换**(中文 UTF-8 更短,用空格补齐,偏移表不动)。
- **症状**:替换本身成功,但应用启动即退出,进程直接消失。
- **定性**:新版 Electron 启用 asar 完整性校验(fuse),内容变更即拒绝加载。
- **解法**:放弃改包路线。所有本地化放到**应用外部**(语言表 / DOM 注入补丁),
  这也是本项目的核心架构决策。

---

## 7. 设置窗口的语言表缓存:新词条永不生效

- **症状**:往 `skill-display-map.json` 里新增的词条在设置窗口**永不生效**,
  而老词条一直正常。map 的 fetch 已带 `cache: "no-store"` + 时间戳。
- **定性**:设置窗口所在渲染环境的某层(协议处理器 / Service Worker)仍在返回旧快照。
- **解法**:**把整个 map 内嵌进补丁脚本**(`build_patch.py`),
  彻底移除 fetch,部署即生效,任何缓存机制都无从拦截。

---

## 8. `SetForegroundWindow` 前台锁

- **症状**:从后台进程调用 `SetForegroundWindow` 后 `SendKeys` / 鼠标点击
  全部落到了别的窗口(键发给了前台的其他应用)。
- **解法**:经典 **AttachThreadInput 线程附着**:

  ```powershell
  $fg = GetForegroundWindow()
  GetWindowThreadProcessId($fg, [ref]$fgt)
  AttachThreadInput($myThread, $fgt, $true)
  SetForegroundWindow($target)
  AttachThreadInput($myThread, $fgt, $false)
  ```

- **连带坑**:长时间运行的 GUI 截图/点击脚本会莫名超时——
  记得先确认 `Start-Sleep` 的单位(见第 4 条),再怀疑前台锁。

---

## 9. MSIX 的坑:锁、副本与退役

- **症状**:WindowsApps 目录受 TrustedInstaller 保护,应用运行中文件被锁,
  想改资源、想复制出来都要绕路。
- **本项目路线**:把包内容复制到普通目录(见第 1 条 tar 方案)→ 在副本上打补丁 →
  官方包保持原样用于自动更新 → 每次官方更新后用 tar 同步差异。
- **备选**:若不需要商店自动更新,也可以 `Remove-AppxPackage` 后纯绿色运行;
  缺点是失去自动更新通道(见第 2 条的手动更新流程)。

---

## 10. 百度免费档的现实

- QPS = 1(`SLEEP_BETWEEN 1.3s` 起步),一次大批量(1.5 万串)初翻约 1-3 小时;
- 月度字符配额有限,**增量缓存是生命线**:按字符串内容做 key,
  已翻过的永不重翻,后续每次版本更新只翻新增串(实测一次小版本 4,428 串 / 8 分钟);
- 毒串(第 3 条)保留英文并记录到 `skipped_strings.txt`,不要让它阻塞大部队。
