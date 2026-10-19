@echo off
rem ============================================================
rem  refresh-zh.cmd  -  Rebuild the Chinese UI map for the
rem  portable Claude copy (E:\ClaudeZhCN\Claude).
rem
rem  Runs gen_zh_map.py, which reads resources\en-US.json,
rem  translates via the Baidu API (cached), and writes
rem  resources\ion-dist\skill-display-map.json.
rem
rem  Run this again after a Claude update replaces en-US.json,
rem  or after you tweak the overrides in gen_zh_map.py.
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

where python >nul 2>nul
if %errorlevel%==0 (
  python gen_i18n_zh.py --run
) else (
  py -3 gen_i18n_zh.py --run
)

echo.
echo [OK] Chinese language pack regenerated (i18n/en-US.json).
echo      Fully quit and restart "Claude ZH" to see changes.
pause
