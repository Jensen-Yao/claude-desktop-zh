# -*- coding: utf-8 -*-
"""
gen_i18n_zh.py  —  Full Chinese language-pack builder for the portable Claude app.

Translates the SPA's real UI string table
    resources/ion-dist/i18n/en-US.json          (~14.6k strings)
    resources/ion-dist/i18n/statsig/en-US.json  (~46 strings)
into Chinese via the Baidu Translate API, preserving the JSON hash keys and any
ICU / HTML placeholders, then overwrites en-US.json in place (after backing it
up). Because the app defaults to and falls back to the en-US locale (zh-CN is
not in its supported list), the whole interface then renders in Chinese with no
bundle patching.

Placeholder policy (never emit a broken string):
  * plain      : no {..} and no <..>            -> translate directly
  * protected  : only simple {token} / HTML tags -> sentinel-protect, translate,
                 restore; if any sentinel is mangled, fall back to English
  * complex    : ICU plural/select, nested {..}, or a lone '#'
                 -> keep English (safe; these are dynamic count strings)

Usage:
    python gen_i18n_zh.py --analyze         # free: categorize + counts, no API
    python gen_i18n_zh.py --run             # translate everything, write files
    python gen_i18n_zh.py --run --limit 80  # smoke test: only 80 new strings
"""

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- paths -----
# 所有路径从脚本所在目录推导 (脚本应放在工作区根目录, 与 Claude/ 文件夹同级)
# 百度翻译凭据文件可用环境变量 BAIDU_CREDS 覆盖
import os
from pathlib import Path
BASE_DIR   = Path(__file__).resolve().parent
CLAUDE_DIR = Path(os.environ.get("CLAUDE_ZH_DIR", BASE_DIR / "Claude"))
CREDS_FILE = os.environ.get("BAIDU_CREDS", str(Path(r"D:\Desktop\bot\api\百度翻译api.txt")))
I18N_DIR = str(CLAUDE_DIR / "resources" / "ion-dist" / "i18n")
EN_JSON = os.path.join(I18N_DIR, "en-US.json")
STATSIG_EN = os.path.join(I18N_DIR, "statsig", "en-US.json")
# Native Electron shell locale (menu bar + right-click menus), read by the main
# process — separate from the web SPA's ion-dist/i18n table above.
# v2.42304+ ships an OFFICIAL zh-Hans.json for the native shell, so the native
# table no longer needs machine translation — the app uses it when its UI
# language is set to Chinese. Only the SPA tables (below) still need patching.
EN_NATIVE = str(CLAUDE_DIR / "resources" / "en-US.json")
# v2.42304+ added a small dynamic-strings table next to the main one; the old
# statsig table was removed in the same release.
DYNAMIC_EN = os.path.join(I18N_DIR, "dynamic", "en-US.json")

def _existing_sources():
    files = [EN_JSON]
    if os.path.exists(STATSIG_EN):
        files.append(STATSIG_EN)
    if os.path.exists(DYNAMIC_EN):
        files.append(DYNAMIC_EN)
    return tuple(files)

SRC_FILES = _existing_sources()
CACHE_JSON = str(BASE_DIR / "baidu_i18n_cache.json")
SKIP_LOG = str(BASE_DIR / "skipped_strings.txt")

API_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"

MAX_BATCH_BYTES = 4000
MAX_BATCH_LINES = 40
SLEEP_BETWEEN = 1.3  # free tier QPS = 1

HAN_RE = re.compile(r"[㐀-鿿]")
LETTER_RE = re.compile(r"[A-Za-z]")
WS_RE = re.compile(r"\s+")

# Simple placeholders / tags we can safely protect & restore.
SIMPLE_PH_RE = re.compile(r"\{[A-Za-z0-9_.]+\}")     # {name} {count} {file.ext}
HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")       # <b> </b> <a href="..">

# Brand / product names Baidu transliterates — restore them.
BRAND_FIXUPS = [
    ("克劳德", "Claude"),
    ("协作", "Cowork"),
]

# Hand-tuned overrides where Baidu is wrong/awkward for a UI control.
MANUAL_OVERRIDES = {
    "Cut": "剪切",
    "OK": "确定",
    "Settings...": "设置...",
    "Dismiss": "忽略",
    "Not now": "暂不",
    "Not Now": "暂不",
    "New session": "新会话",
    "New chat": "新对话",
    # short words Baidu mis-renders out of context
    "Back": "返回",            # Baidu: '< 返回' (stray bracket)
    "Pin": "固定",             # Baidu: 针 (a needle)
    "Unpin": "取消固定",
    "Star": "星标",            # Baidu: 星
    "Starred": "已加星标",
    "Run": "运行",             # Baidu: 跑 (jogging)
    "run": "运行",
    "Ran": "已运行",
    "ran": "已运行",
    "Export": "导出",          # Baidu: 出口 (trade export)
    "Regenerate": "重新生成",   # Baidu: 再生
    "Archive": "归档",          # Baidu: 档案
    "Unarchive": "取消归档",
    "Free": "免费",            # Baidu: 自由
    "Usage": "用量",           # Baidu: 用法
    "Set": "设定",             # Baidu: 集 (a set/collection)
    "Light": "浅色",           # theme name; Baidu: 光
    "Dark": "深色",
    "On": "开",                # toggle; Baidu: 上
    "Off": "关",
    "No": "否",
    "Yes": "是",
    "New": "新建",
    "View": "查看",            # Baidu: 视图
    "Home": "主页",            # Baidu: 家
    "Ask": "询问",
    "mo": "月",
    "Hey there": "你好",
    # feature/brand & literal tokens that must not be translated
    "Artifacts": "Artifacts",
    "Artifact": "Artifact",
    "Chrome": "Chrome",        # Baidu: 铬 (the metal)
    "Sans": "Sans",
    "YYYY": "YYYY",            # date-format template
    "true": "true",
}


# ---------------------------------------------------------- credentials -----
def load_creds(path):
    if not os.path.exists(path):
        sys.exit("credentials file not found: " + path)
    with open(path, encoding="utf-8") as f:
        raw = f.read()

    appid = key = None
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        parts = re.split(r"[:：=]\s*", s, maxsplit=1)
        if len(parts) == 2:
            label, val = parts[0].strip(), parts[1].strip()
        else:
            label, val = "", s.strip()
        if not val:
            continue
        low = label.lower()
        if "密钥" in label or "key" in low or "secret" in low:
            key = val
        elif "app" in low or "appid" in low or ("id" in low):
            appid = val

    if not appid or not key:
        tokens = re.findall(r"[A-Za-z0-9_\-]{6,}", raw)
        digits = [t for t in tokens if t.isdigit()]
        others = [t for t in tokens if not t.isdigit()]
        if not appid and digits:
            appid = digits[0]
        if not key and others:
            key = others[-1]

    if not appid or not key:
        sys.exit("could not parse Baidu credentials from " + path)
    return appid, key


# --------------------------------------------------------------- helpers ----
def norm(s):
    return WS_RE.sub(" ", s).strip()


def has_han(s):
    return bool(HAN_RE.search(s))


def apply_fixups(text):
    for a, b in BRAND_FIXUPS:
        text = text.replace(a, b)
    return text


def categorize(s):
    """Return 'skip' | 'plain' | 'protected' | 'complex' for a source string."""
    if not isinstance(s, str) or not s.strip():
        return "skip"
    if has_han(s):
        return "skip"
    if not LETTER_RE.search(s):
        return "skip"           # pure symbols/numbers — nothing to translate
    # Strip the tokens we know how to protect; anything left with braces/angles
    # is ICU/nested/unbalanced -> too risky, keep English.
    temp = HTML_TAG_RE.sub(" ", s)
    temp = SIMPLE_PH_RE.sub(" ", temp)
    if any(c in temp for c in "{}<>#"):
        return "complex"
    if SIMPLE_PH_RE.search(s) or HTML_TAG_RE.search(s):
        return "protected"
    return "plain"


def protect(s):
    """Replace simple tokens with U+E000+i sentinels. Return (query, tokens).

    Private Use Area chars carry no dictionary meaning, so Baidu echoes them
    back verbatim (unlike bracket sentinels, which it normalizes to 【】)."""
    tokens = []

    def repl(m):
        idx = len(tokens)
        tokens.append(m.group(0))
        return chr(0xE000 + idx)

    # tags first, then braces (positions are disjoint; indices stay unique)
    q = HTML_TAG_RE.sub(repl, s)
    q = SIMPLE_PH_RE.sub(repl, q)
    return q, tokens


def restore(zh, tokens):
    """Put original tokens back. Return (text, ok). ok=False if any sentinel lost."""
    ok = True
    for i, tok in enumerate(tokens):
        ch = chr(0xE000 + i)
        if ch not in zh:
            ok = False
            continue
        zh = zh.replace(ch, tok)
    # a stray PUA sentinel we can't account for means the string is unsafe
    if any(0xE000 <= ord(c) <= 0xE000 + max(len(tokens), 1) for c in zh):
        ok = False
    return zh, ok


# ------------------------------------------------------------- Baidu API ----
class SensitiveWordError(RuntimeError):
    """Baidu error 20003: the query tripped Baidu's content filter."""


def bd_translate(q, appid, key, frm="en", to="zh", retries=5):
    last = None
    for attempt in range(retries):
        salt = str(random.randint(10000, 9999999))
        sign = hashlib.md5((appid + q + salt + key).encode("utf-8")).hexdigest()
        params = {
            "q": q, "from": frm, "to": to,
            "appid": appid, "salt": salt, "sign": sign,
        }
        url = API_URL + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = "net:" + repr(e)
            time.sleep(2.0 + attempt * 1.5)
            continue

        if "trans_result" in data:
            return data["trans_result"]

        code = str(data.get("error_code", ""))
        last = code + ":" + str(data.get("error_msg", ""))
        if code == "20003":
            raise SensitiveWordError(last)
        if code in ("54003", "52001", "52002", "54005"):
            time.sleep(2.5 + attempt * 2.0)
            continue
        raise RuntimeError("Baidu fatal error " + last)

    raise RuntimeError("Baidu failed after retries, last=" + str(last))


def make_batches(strings):
    batches, cur, cur_bytes = [], [], 0
    for s in strings:
        b = len(s.encode("utf-8")) + 1
        if cur and (cur_bytes + b > MAX_BATCH_BYTES or len(cur) >= MAX_BATCH_LINES):
            batches.append(cur)
            cur, cur_bytes = [], 0
        cur.append(s)
        cur_bytes += b
    if cur:
        batches.append(cur)
    return batches


def translate_batch(lines, appid, key):
    """Translate newline-free unique query strings; return {query: dst}.

    On Baidu's sensitive-word filter (20003), bisect the batch to isolate the
    offending line(s); a single offending line maps to "" (caller keeps EN)."""
    q = "\n".join(lines)
    try:
        res = bd_translate(q, appid, key)
    except SensitiveWordError:
        if len(lines) == 1:
            return {lines[0]: ""}
        mid = len(lines) // 2
        out = translate_batch(lines[:mid], appid, key)
        time.sleep(SLEEP_BETWEEN)
        out.update(translate_batch(lines[mid:], appid, key))
        return out
    out = {}
    if len(res) == len(lines):
        for i, ln in enumerate(lines):
            out[ln] = res[i].get("dst", "")
        return out
    by_src = {}
    for item in res:
        by_src.setdefault(item.get("src", ""), item.get("dst", ""))
    missing = []
    for ln in lines:
        if ln in by_src:
            out[ln] = by_src[ln]
        else:
            missing.append(ln)
    for ln in missing:
        try:
            r = bd_translate(ln, appid, key)
            out[ln] = r[0].get("dst", "") if r else ""
        except SensitiveWordError:
            out[ln] = ""
        time.sleep(SLEEP_BETWEEN)
    return out


def bisect_batch(lines, appid, key):
    """Persistent-failure fallback (Baidu 52002 stuck on a batch): halve the
    batch; a single line that still fails after one cooldown retry maps to ""
    (keep English) and is appended to SKIP_LOG for manual review."""
    if len(lines) == 1:
        time.sleep(5)
        try:
            # fast fail: only 2 attempts — a genuinely poisoned string should
            # cost ~15s, not the full 2-3 min retry ladder
            r = bd_translate(lines[0], appid, key, retries=2)
            return {lines[0]: r[0].get("dst", "") if r else ""}
        except Exception:
            try:
                with open(SKIP_LOG, "a", encoding="utf-8") as f:
                    f.write(lines[0].replace("\n", " ") + "\n")
            except Exception:
                pass
            return {lines[0]: ""}
    mid = len(lines) // 2
    out = bisect_batch(lines[:mid], appid, key)
    time.sleep(SLEEP_BETWEEN)
    out.update(bisect_batch(lines[mid:], appid, key))
    return out


# ------------------------------------------------------------- pipeline -----
def _han_values(m):
    return sum(1 for v in m.values() if isinstance(v, str) and HAN_RE.search(v))


def load_sources():
    """Return {output_path: {hash: english}} for the two files.

    The English SOURCE is the .en.bak backup (the live file is overwritten with
    Chinese, so re-runs must not read their own output). If the live file is
    itself (mostly) English and differs from the backup, a Claude update has
    replaced it — refresh the backup so new/changed strings get picked up."""
    result = {}
    for path in SRC_FILES:
        with open(path, encoding="utf-8") as f:
            cur = json.load(f)
        bak = path + ".en.bak"
        if os.path.exists(bak):
            with open(bak, encoding="utf-8") as f:
                old = json.load(f)
            if cur != old and _han_values(cur) < max(1, len(cur) // 20):
                shutil.copy2(path, bak)     # fresh English from an app update
                print("app update detected, backup refreshed: " + bak)
                old = cur
            result[path] = old
        else:
            result[path] = cur
    return result


def collect_scope(all_maps):
    """Categorize every unique value. Return (plain_or_protected set, stats)."""
    cats = {"skip": 0, "plain": 0, "protected": 0, "complex": 0}
    uniq = {}   # source -> category (plain/protected only)
    complex_samples = []
    seen = set()
    for m in all_maps.values():
        for v in m.values():
            if v in seen:
                continue
            seen.add(v)
            c = categorize(v)
            cats[c] += 1
            if c in ("plain", "protected"):
                uniq[v] = c
            elif c == "complex" and len(complex_samples) < 8:
                complex_samples.append(v)
    return uniq, cats, complex_samples


def analyze():
    all_maps = load_sources()
    total_keys = sum(len(m) for m in all_maps.values())
    uniq, cats, samples = collect_scope(all_maps)

    cache = load_cache()
    todo = [s for s in uniq if s not in cache]

    print("=== i18n language-pack analysis ===")
    print("files: en-US.json + statsig/en-US.json")
    print("total keys           : %d" % total_keys)
    print("unique values        : %d" % (cats["skip"] + cats["plain"]
                                          + cats["protected"] + cats["complex"]))
    print("  plain (direct)     : %d" % cats["plain"])
    print("  protected (tokens) : %d" % cats["protected"])
    print("  complex (keep EN)  : %d" % cats["complex"])
    print("  skip (cjk/symbol)  : %d" % cats["skip"])
    print("translatable (uniq)  : %d" % len(uniq))
    print("already cached       : %d" % (len(uniq) - len(todo)))
    print("NEW to translate     : %d" % len(todo))
    batches = make_batches([protect(s)[0] if uniq[s] == "protected" else s
                            for s in todo])
    print("estimated API batches: %d  (~%.1f min at %.1fs/batch)"
          % (len(batches), len(batches) * SLEEP_BETWEEN / 60.0, SLEEP_BETWEEN))
    print("--- sample complex (will stay English) ---")
    for s in samples:
        print("   " + (s[:90] + "…" if len(s) > 90 else s))


# ---------------------------------------------------------------- cache -----
def load_cache():
    if os.path.exists(CACHE_JSON):
        try:
            with open(CACHE_JSON, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(cache):
    with open(CACHE_JSON, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0)


# ------------------------------------------------------------------ run -----
def run(limit=0):
    appid, key = load_creds(CREDS_FILE)
    print("creds ok: appid=%s key=%s" % (appid, "*" * len(key)))

    all_maps = load_sources()
    uniq, cats, _ = collect_scope(all_maps)
    print("unique translatable: %d  (plain=%d protected=%d, complex kept EN=%d)"
          % (len(uniq), cats["plain"], cats["protected"], cats["complex"]))

    cache = load_cache()

    # Build query per uncached source; remember token maps for protected ones.
    todo = [s for s in uniq if s not in cache]
    if limit:
        todo = todo[:limit]
        print("LIMIT: only translating first %d new strings (smoke test)" % limit)

    query_of = {}     # source -> query string sent to Baidu
    tokens_of = {}    # source -> token list (protected only)
    for s in todo:
        if uniq[s] == "protected":
            q, toks = protect(s)
            query_of[s] = q
            tokens_of[s] = toks
        else:
            query_of[s] = s

    uniq_queries = sorted(set(query_of[s] for s in todo))
    print("cached: %d   new sources: %d   unique queries: %d"
          % (len(uniq) - len([s for s in uniq if s not in cache]),
             len(todo), len(uniq_queries)))

    batches = make_batches(uniq_queries)
    qmap = {}
    for bi, batch in enumerate(batches, 1):
        try:
            got = translate_batch(batch, appid, key)
        except RuntimeError as e:
            # Poison batch: Baidu 52002 etc. persistently failing on this
            # batch's content. Bisect down to singles; singles that still fail
            # keep English and are logged for manual review.
            print("batch %d persistent failure (%s) — bisecting"
                  % (bi, str(e)[:70]))
            got = bisect_batch(batch, appid, key)
        qmap.update(got)
        # incremental cache: resolve everything we can so far, then persist
        fail = 0
        for s in todo:
            if s in cache:
                continue
            q = query_of[s]
            if q not in qmap:
                continue
            zh = norm(qmap[q])
            if not zh:
                cache[s] = s              # sensitive-word/empty: keep English
                fail += 1
                continue
            if uniq[s] == "protected":
                zh, ok = restore(zh, tokens_of[s])
                if not ok:
                    cache[s] = s          # fallback: keep English, never broken
                    fail += 1
                    continue
            zh = apply_fixups(zh)
            cache[s] = zh
        save_cache(cache)
        print("batch %d/%d  (+%d q)  cache=%d%s"
              % (bi, len(batches), len(got), len(cache),
                 ("  restore-fallback=%d" % fail) if fail else ""))
        if bi < len(batches):
            time.sleep(SLEEP_BETWEEN)

    write_outputs(all_maps, uniq, cache)


def translate_value(v, uniq, cache):
    """Final Chinese for a value, honoring overrides/fixups; '' if untouched."""
    nv = v
    if nv in MANUAL_OVERRIDES:
        return MANUAL_OVERRIDES[nv]
    if uniq.get(v) not in ("plain", "protected"):
        return ""                      # complex/skip -> leave English
    zh = cache.get(v, "")
    if not zh or zh == v:
        return ""
    zh = apply_fixups(zh)
    if not has_han(zh):
        return ""
    return zh


def write_outputs(all_maps, uniq, cache):
    total_repl = 0
    for path, m in all_maps.items():
        # back up the pristine English once
        bak = path + ".en.bak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
            print("backup written: " + bak)

        out = {}
        repl = 0
        for k, v in m.items():
            zh = translate_value(v, uniq, cache)
            if zh:
                out[k] = zh
                repl += 1
            else:
                out[k] = v
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        total_repl += repl
        print("wrote %s  (%d/%d values in Chinese)" % (path, repl, len(m)))
    print("TOTAL Chinese values written: %d" % total_repl)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze", action="store_true", help="categorize only, no API")
    ap.add_argument("--run", action="store_true", help="translate + write files")
    ap.add_argument("--limit", type=int, default=0, help="translate only N new strings")
    args = ap.parse_args()

    if args.analyze or not args.run:
        analyze()
        return
    run(limit=args.limit)


if __name__ == "__main__":
    main()
