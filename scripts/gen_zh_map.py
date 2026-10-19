# -*- coding: utf-8 -*-
"""
gen_zh_map.py  —  Baidu offline translator for the Claude desktop portable copy.

Reads the app's English UI strings (resources/en-US.json), translates the
placeholder-free ones to Chinese via the Baidu Translate API, and writes an
offline replacement map (resources/ion-dist/skill-display-map.json) that the
injected front-end patch (skill-display-patch.js) loads same-origin every 5s.

Why offline: the Baidu API needs a server-side MD5 sign and sends no CORS
headers, so it cannot be called from the renderer. We pre-compute the map here.

Credentials come from a plain text file (label-based, with a digit/alnum
heuristic fallback). Results are cached so re-runs are cheap and incremental.
"""

import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- paths -----
import os
from pathlib import Path
BASE_DIR   = Path(__file__).resolve().parent
CLAUDE_DIR = Path(os.environ.get("CLAUDE_ZH_DIR", BASE_DIR / "Claude"))
CREDS_FILE = os.environ.get("BAIDU_CREDS", str(Path(r"D:\Desktop\bot\api\百度翻译api.txt")))
EN_JSON = str(CLAUDE_DIR / "resources" / "en-US.json")
OUT_JSON = str(CLAUDE_DIR / "resources" / "ion-dist" / "skill-display-map.json")
CACHE_JSON = str(BASE_DIR / "baidu_cache.json")

API_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"

# Batch sizing: Baidu caps a query at 6000 bytes; stay well under, and keep the
# line count modest so trans_result stays easy to map back 1:1.
MAX_BATCH_BYTES = 4000
MAX_BATCH_LINES = 40
SLEEP_BETWEEN = 1.3  # free tier QPS = 1, so pace requests ~1/s

HAN_RE = re.compile(r"[㐀-鿿]")          # matches the front-end's CJK test
LETTER_RE = re.compile(r"[A-Za-z]")
WS_RE = re.compile(r"\s+")

# Brand / product names Baidu (inconsistently) transliterates — restore them.
# In this app's strings these substrings ONLY ever come from the brand name, so
# a blind replace is safe.
BRAND_FIXUPS = [
    ("克劳德", "Claude"),   # "Claude" -> 克劳德
    ("协作", "Cowork"),      # the "Cowork" feature -> 协作
]

# Hand-tuned overrides where Baidu is plain wrong or awkward for a UI control.
MANUAL_OVERRIDES = {
    "Cut": "剪切",            # Baidu: 切
    "OK": "确定",            # Baidu: 好的
    "Settings...": "设置...",  # Baidu: 设置。..
    "Dismiss": "忽略",        # Baidu: 解雇 (fire an employee!)
    "Not now": "暂不",        # Baidu: 现在不行
    "Not Now": "暂不",
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

    # Heuristic fallback: a Baidu appid is all digits; the secret is alnum.
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


# --------------------------------------------------------------- filter -----
def norm(s):
    """Match the front-end's normalizeText: collapse whitespace runs + trim."""
    return WS_RE.sub(" ", s).strip()


def is_translatable(s):
    if not isinstance(s, str):
        return False
    t = s.strip()
    if len(t) < 1 or len(t) > 400:
        return False
    if "\n" in s or "\r" in s:
        return False
    # Skip ICU/format placeholders and HTML — the rendered DOM substitutes real
    # values, so these keys can never exact-match and would only mislead.
    if any(c in s for c in "{}<>"):
        return False
    if not LETTER_RE.search(s):
        return False
    if HAN_RE.search(s):
        return False
    return True


def has_han(s):
    return bool(HAN_RE.search(s))


def apply_fixups(text):
    for a, b in BRAND_FIXUPS:
        text = text.replace(a, b)
    return text


# ------------------------------------------------------------- Baidu API ----
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
        except Exception as e:  # network / transient
            last = "net:" + repr(e)
            time.sleep(2.0 + attempt * 1.5)
            continue

        if "trans_result" in data:
            return data["trans_result"]

        code = str(data.get("error_code", ""))
        last = code + ":" + str(data.get("error_msg", ""))
        # transient: rate limit / timeout / system error
        if code in ("54003", "52001", "52002", "54005"):
            time.sleep(2.5 + attempt * 2.0)
            continue
        # fatal: bad appid / sign / unauthorized / no balance
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
    """Translate a list of newline-free unique strings; return {src: dst}."""
    q = "\n".join(lines)
    res = bd_translate(q, appid, key)
    out = {}
    if len(res) == len(lines):
        for i, ln in enumerate(lines):
            out[ln] = res[i].get("dst", "")
        return out
    # Count mismatch (Baidu merged/dropped a line): map what we can by src,
    # then translate the stragglers one at a time.
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
        r = bd_translate(ln, appid, key)
        out[ln] = r[0].get("dst", "") if r else ""
        time.sleep(SLEEP_BETWEEN)
    return out


# ------------------------------------------------------------------ main ----
def main():
    appid, key = load_creds(CREDS_FILE)
    print("creds ok: appid=%s key=%s" % (appid, "*" * len(key)))

    with open(EN_JSON, encoding="utf-8") as f:
        raw = json.load(f)

    sources = set()
    skipped_placeholder = 0
    for v in raw.values():
        if isinstance(v, str) and v.strip():
            if is_translatable(v):
                sources.add(norm(v))
            elif any(c in v for c in "{}<>"):
                skipped_placeholder += 1
    sources = sorted(sources)
    print("unique translatable sources: %d  (placeholder/HTML skipped: %d)"
          % (len(sources), skipped_placeholder))

    cache = {}
    if os.path.exists(CACHE_JSON):
        try:
            with open(CACHE_JSON, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    todo = [s for s in sources if s not in cache]
    print("cached: %d   to translate: %d" % (len(sources) - len(todo), len(todo)))

    batches = make_batches(todo)
    for bi, batch in enumerate(batches, 1):
        got = translate_batch(batch, appid, key)
        cache.update(got)
        with open(CACHE_JSON, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=0)
        print("batch %d/%d  (+%d)  cache=%d"
              % (bi, len(batches), len(got), len(cache)))
        if bi < len(batches):
            time.sleep(SLEEP_BETWEEN)

    # Build replacements: keep only real Chinese results that differ from source.
    replacements = {}
    empty = 0
    for s in sources:
        if s in MANUAL_OVERRIDES:
            replacements[s] = MANUAL_OVERRIDES[s]
            continue
        dst = apply_fixups(norm(cache.get(s, "")))
        if dst and dst != s and has_han(dst):
            replacements[s] = dst
        else:
            empty += 1

    ordered = dict(sorted(replacements.items(),
                          key=lambda kv: len(kv[0]), reverse=True))

    out = {"generatedAt": int(time.time()), "replacements": ordered}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)

    print("replacements written: %d   (dropped non-Chinese/equal: %d)"
          % (len(ordered), empty))
    print("output: " + OUT_JSON)
    # ASCII-safe sample so a GBK console won't mojibake it.
    sample = list(ordered.items())[:6]
    print("sample: " + json.dumps(sample, ensure_ascii=True))


if __name__ == "__main__":
    main()
