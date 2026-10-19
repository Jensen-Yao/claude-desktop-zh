# -*- coding: utf-8 -*-
"""
build_patch.py — 把 skill-display-map.json 内嵌进 skill-display-patch.js
(内嵌后补丁不再依赖 fetch, 彻底免疫 Service Worker / HTTP 缓存)

何时运行: 每次 refresh-zh.cmd 或 gen_zh_map.py 重新生成 map 之后、
运行 deploy-zh.ps1 之前。deploy-zh.ps1 部署的就是这个内嵌版补丁。
"""
import json

import os
from pathlib import Path
BASE = os.environ.get("CLAUDE_ZH_DIR", str(Path(__file__).resolve().parent.parent / "Claude" / "resources" / "ion-dist"))
MAP_JSON = BASE + r'\skill-display-map.json'
PATCH_JS = BASE + r'\skill-display-patch.js'

OBSERVER_BLOCK = '''  setTimeout(() => {
    patchTree(document.documentElement);
    patchAttrs(document.documentElement);
    // Observer for dynamic content
    const obs = new MutationObserver(muts => {
      for (const m of muts) {
        if (m.type === "characterData") patchNode(m.target);
        m.addedNodes.forEach(n => {
          if (n.nodeType === Node.TEXT_NODE) patchNode(n);
          else if (n.nodeType === Node.ELEMENT_NODE) {
            patchTree(n);
            patchAttrs(n);
          }
        });
      }
    });
    obs.observe(document.documentElement, {
      subtree: true, childList: true, characterData: true
    });
  }, 500);
})();
'''

def build():
    m = json.load(open(MAP_JSON, encoding='utf-8'))
    src = open(PATCH_JS, encoding='utf-8').read()
    # 若当前 patch 已是内嵌版, 先取其头部 (到 EMBEDDED 定义之前);
    # 若仍是 fetch 版, 取 "// Load map" 之前
    if 'const EMBEDDED' in src:
        head = src.split('  const EMBEDDED')[0]
    else:
        head = src.split('  // Load map')[0]
    body = (
        '  // Map is embedded at build time - no fetch, immune to SW/HTTP caching\n'
        '  const EMBEDDED = ' + json.dumps(m, ensure_ascii=False) + ';\n'
        '  for (const [k, v] of Object.entries(EMBEDDED.replacements || {})) {\n'
        '    if (k && v) textMap.set(normalize(k).toLowerCase(), String(v));\n'
        '  }\n'
        '  console.log("[zh-patch] Loaded " + textMap.size + " translations (embedded)");\n\n'
        + OBSERVER_BLOCK
    )
    open(PATCH_JS, 'w', encoding='utf-8').write(head + body)
    n = len(m.get('replacements', {}))
    print(f'embedded patch rebuilt: {n} replacements -> {PATCH_JS}')

if __name__ == '__main__':
    build()
