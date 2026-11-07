#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aiwf —— AI 开发工作流：上下文打包器 + 状态机 + 门禁 + 验收条件兑现检查。

只用标准库。

    python aiwf.py init "fix-login-npe" --title "修复登录空指针" --repo order-service
    python aiwf.py status [--all]
    python aiwf.py acs fix-login-npe                       # 看验收条件编号与认领情况
    python aiwf.py context fix-login-npe
    python aiwf.py record fix-login-npe --stage implementing --repo order-service \\
                    --file src/LoginService.java --criterion AC-1 --why "手机号为空的根因在这"
    python aiwf.py handoff fix-login-npe --branch fix/npe --pr 128 --todo "补回归用例"
    python aiwf.py ac-check fix-login-npe > ac-prompt.md    # 粘给新 session 的 agent
    python aiwf.py ac-record fix-login-npe --verdict warn --issue "AC-2 未兑现"
    python aiwf.py verify fix-login-npe --cmd "mvn -q test" --result pass
    python aiwf.py review fix-login-npe --independent --reviewer reviewer-a --verdict pass
    python aiwf.py gate fix-login-npe
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Windows 控制台默认不是 UTF-8，输出中文/符号会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

_env_data = os.environ.get("AIWF_DATA", "").strip()
STORY_DIR = Path(_env_data).expanduser() if _env_data else ROOT / "stories"

STAGES = ["new", "clarifying", "planned", "implementing",
          "verifying", "reviewing", "reviewed", "done"]

# 只有这两个阶段的改动需要挂验收条件
WORK_STAGES = ("implementing", "verifying")

# 自上次对账后新增多少条改动就提醒一次
AUTO_AC_HINT_EVERY = 3

# ── 明文凭据扫描 ──
SECRET_KEY = re.compile(
    r"(pass(word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|credential|密码|口令)", re.I)
SKIP_EXT = {".class", ".jar", ".war", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip",
            ".gz", ".pdf", ".docx", ".xlsx", ".pptx", ".woff", ".woff2", ".ttf",
            ".so", ".dll", ".exe", ".bin", ".lock"}
SKIP_DIRS = {".git", "node_modules", "target", "build", "out", "dist", ".gradle",
             ".idea", ".vscode", "__pycache__", ".next", "vendor"}
# 看起来就不是真实值的情况
PLACEHOLDER = re.compile(
    r"^(\$|\{\{|<|your|xxx+|change_?me|todo|tbd|none|null|placeholder|\*\*\*"
    r"|env\(|os\.environ|getenv|示例|环境变量|运行时|待填|-+$)", re.I)

NEXT_ACTION = {
    "new": "先做澄清：把 Jira 描述贴进 requirement.md，列出需要向需求方确认的问题。",
    "clarifying": "把澄清问答补齐，写出可判定的验收条件（建议编号 AC-1、AC-2……），然后进入 planned。",
    "planned": "按 plan.md 逐个 repo 实施。每改一处就 record，**并挂上它兑现的验收条件**（--criterion AC-x）。",
    "implementing": "实施中先跑 `aiwf.py acs` 看哪条验收条件还没人认领；做完用 ac-check 让独立 agent 对账，"
                    "结果用 ac-record 记回来。",
    "verifying": "跑编译/测试并用 verify 记录；之后必须换独立 session 做 review。",
    "reviewing": "把 review 提出的问题修完，再走一次独立 review；通过后进入 reviewed。",
    "reviewed": "合并，更新 handoff 的 PR 状态，然后收尾。",
    "done": "本 story 已完成。把可复用结论沉淀到 decisions.md 的「可复用」小节。",
}

META_TMPL = {
    "id": "", "slug": "", "title": "", "stage": "new",
    "created": "", "key": "", "jira": "", "repos": [],
    "type": "feature", "parents": [], "deferred": {}, "blocked": {},
    "handoff": {"branch": "", "pr": "", "commit": "", "todo": ""},
}

STORY_TYPES = ("feature", "bug", "hotfix")
DEFECT_STATUSES = ("open", "fixed", "verified", "wontfix", "duplicate")

REQ_TMPL = """# 需求：{title}

- story: `{slug}`
- 创建: {created}
- 来源:

## 原始描述

（粘贴 Jira story 描述）

## 澄清问答

<!-- 一问一答，每条一行 -->

## 验收条件

<!-- 建议编号：AC-1、AC-2……；没有编号时按顺序自动编号 -->

## 边界与非目标

-

## 依赖与影响面

-
"""

DEC_TMPL = """# 决策记录：{title}

> 每条格式：**结论** ← 被什么驱动 ← 什么条件下会被推翻

## 技术决策

-

## 被否掉的方案

| 方案 | 否决原因 |
|---|---|
|  |  |

## 可复用

-
"""

PLAN_TMPL = """# 实施方案：{title}

## 改哪些文件

| repo | 文件 | 改什么 | 为什么 |
|---|---|---|---|
|  |  |  |  |

## 执行顺序

1.

## 风险

-
"""

AC_PROMPT = """# 验收条件兑现检查（独立）

> 你【没有参与】这个 story 的实施，也不必读代码。你的任务只有一件事：
> **对账——每条验收条件，是否真的被兑现了。**

## 两件事，逐条回答

1. **逐条对账**：每条验收条件，在「改动认领」里有没有对应项？那一项的说明，**是否真的构成兑现**？
   （挂了个编号但说明对不上，算未兑现）
2. **未声明改动**：有没有改动没挂验收条件？它可能是什么性质（越界/配套/无关）？

## 事实

### 验收条件与认领情况

{acs}

### 全部改动（最近 15 条）

{changes}

### 上一次检查结论（如有）

{history}

## 只输出 JSON，不要任何解释

{{"acs": [{{"id": "AC-1", "fulfilled": true, "evidence": "哪条改动兑现的，一句话"}}],
 "undeclared": [],
 "verdict": "pass"}}

- `verdict`：`pass`（全部兑现且无越界） / `warn`（兑现但有小越界） / `block`（有验收条件未兑现）
- 判定 `block` 会阻止该 story 进入下一阶段
"""


def now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def slugify(s):
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9\u4e00-\u9fa5]+", "-", s)
    return s.strip("-") or "story"


def safe_key(k):
    """把 tracker 号（Jira / GitHub issue / 禅道…）清洗成可做目录名的一段。

    SHIP-1024 / ship_1024 / #123 -> SHIP-1024 / SHIP-1024 / 123
    返回大写；空则返回 ""。
    """
    k = (k or "").strip().lstrip("#").strip()
    k = re.sub(r"[^A-Za-z0-9._-]+", "-", k).strip("-")
    return k.upper()


def story_path(slug):
    """定位 story：支持完整目录名、目录名后缀、slug、以及 tracker 号。"""
    if not STORY_DIR.exists():
        return None
    q = slug.strip()
    for d in sorted(STORY_DIR.iterdir()):
        if not d.is_dir():
            continue
        if d.name == q or d.name.endswith("-" + q) or d.name == slugify(q):
            return d
        meta = load_meta(d) or {}
        for k in (meta.get("key"), meta.get("jira")):
            if k and str(k).lower() == q.lower():
                return d
    return None


def load_meta(d):
    """读 meta.json；带 BOM 或 JSON 损坏时不崩，返回 {} 并提示。"""
    p = d / "meta.json"
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8-sig", errors="replace").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"WARN: {p} 不是合法 JSON（{e}），按空 meta 处理", file=sys.stderr)
        return {}


def save_meta(d, meta):
    (d / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_text(d, name):
    p = d / name
    return p.read_text(encoding="utf-8-sig") if p.exists() else ""


def _section(text, header):
    if header not in text:
        return ""
    return text.split(header, 1)[1].split("\n## ", 1)[0]


def _is_placeholder(text):
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return not body.strip().strip("-").strip()


def parse_criteria(req_text):
    """解析验收条件，返回 [{id, text, hash}]。有 AC-x 编号用编号，否则按顺序自动编号。

    hash 用来识别「认领时绑的是哪一版 AC」——AC 被改写后，旧认领自动失效。
    """
    out, idx = [], 0
    for ln in _section(req_text, "## 验收条件").splitlines():
        m = re.match(r"\s*[-*]\s*\[[ xX]\]\s*(.+)$", ln)
        if not m:
            continue
        body = m.group(1).strip()
        idx += 1
        mm = re.match(r"(AC-[A-Za-z0-9]+)\s*[:：]?\s*(.*)$", body)
        if mm and mm.group(2).strip():
            cid, ctext = mm.group(1), mm.group(2).strip()
        elif mm:
            cid, ctext = mm.group(1), body
        else:
            cid, ctext = f"AC-{idx}", body
        out.append({"id": cid, "text": ctext, "hash": _ac_hash(ctext)})
    return out


def _ac_hash(text):
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:8]


def load_changes(d):
    out = []
    for line in _jsonl_lines(d / "changes.jsonl"):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _links(d):
    """事后补挂的映射：{目标改动的 ts: link 事件}"""
    return {c["target"]: c for c in load_changes(d)
            if c.get("kind") == "link" and c.get("target")}


def load_defects(d):
    """读 defects.jsonl 事件流。"""
    out = []
    for line in _jsonl_lines(d / "defects.jsonl"):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def defect_states(d):
    """把事件流折叠成 {D-1: {状态与最新字段}}。"""
    state = {}
    for e in load_defects(d):
        did = e.get("id")
        if not did:
            continue
        cur = state.setdefault(did, {"id": did, "history": []})
        cur["history"].append(e)
        ev = e.get("event")
        if ev == "open":
            cur.update(status="open", title=e.get("title", ""),
                       severity=e.get("severity", "major"), source=e.get("source", "self"),
                       related_ac=e.get("related_ac", []), repro=e.get("repro", ""),
                       found_in=e.get("found_in", ""))
        elif ev == "fix":
            cur.update(status="fixed", fix_commit=e.get("commit", ""),
                       fix_note=e.get("note", ""), fix_ts=e.get("ts", ""))
        elif ev == "verify":
            if e.get("result") == "pass":
                cur.update(status="verified", verify_by=e.get("by", ""),
                           verify_note=e.get("note", ""), verify_ts=e.get("ts", ""))
            else:
                cur.update(status="open", verify_by=e.get("by", ""),
                           verify_note=e.get("note", ""), verify_ts=e.get("ts", ""))
        elif ev in ("wontfix", "duplicate"):
            cur.update(status=ev, close_reason=e.get("note", ""))
    return state


def open_defects(d):
    return {k: v for k, v in defect_states(d).items() if v.get("status") == "open"}


def unverified_defects(d):
    return {k: v for k, v in defect_states(d).items() if v.get("status") == "fixed"}


def _defect_append(d, entry):
    entry.setdefault("ts", now())
    with (d / "defects.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _next_defect_id(d):
    ids = [k for k in defect_states(d) if k.startswith("D-")]
    nums = [int(k[2:]) for k in ids if k[2:].isdigit()]
    return f"D-{max(nums) + 1 if nums else 1}"


def _deferred(d):
    """已转交出去的 AC：{AC id: {reason, owner, at}}"""
    meta = load_meta(d) or {}
    return meta.get("deferred") or {}


def _claim_entries(d):
    """展开每条改动认领的 AC，并带上认领时的 AC 文本哈希。"""
    links = _links(d)
    out = []
    for c in load_changes(d):
        if c.get("kind") == "link":
            continue
        crit = c.get("criteria") or []
        hashes = c.get("acHashes") or {}
        src = c
        if not crit and c.get("ts") in links:
            crit = links[c["ts"]].get("criteria") or []
            hashes = links[c["ts"]].get("acHashes") or {}
            src = links[c["ts"]]
        for ac in crit:
            out.append({"ac": ac, "change": c, "hash": hashes.get(ac)})
    return out


def ac_claims(d):
    """{AC id: [改动 entry]} —— 只算「认领时的 AC 版本仍然是当前版本」的。

    返回两份：有效认领、过期认领（AC 被改写过）。
    """
    crits = {c["id"]: c["hash"] for c in parse_criteria(read_text(d, "requirement.md"))}
    claims, stale = {}, {}
    for e in _claim_entries(d):
        ac, change, h = e["ac"], e["change"], e["hash"]
        now = crits.get(ac)
        if now is None:                      # AC 已被删掉
            stale.setdefault(ac, []).append(change)
            continue
        if h and h != now:                   # 认领绑的是旧版 AC
            stale.setdefault(ac, []).append(change)
            continue
        claims.setdefault(ac, []).append(change)
    return claims, stale


def unclaimed_acs(d):
    """还没有有效认领、也没被转交出去的 AC。"""
    crits = parse_criteria(read_text(d, "requirement.md"))
    claims, _ = ac_claims(d)
    deferred = _deferred(d)
    return [c for c in crits if c["id"] not in claims and c["id"] not in deferred]


def stale_acs(d):
    """过期认领：AC 文本改过。已经用新版本重新认领过的不再报。"""
    claims, stale = ac_claims(d)
    return {k: v for k, v in stale.items() if k not in claims}


def unlinked_changes(d):
    """工作阶段里 kind 为 code/test、且没挂（也没事后补挂）验收条件的改动。"""
    links = _links(d)
    return [c for c in load_changes(d)
            if c.get("kind") != "link"
            and c.get("stage") in WORK_STAGES
            and c.get("kind", "code") in ("code", "test")
            and not c.get("criteria")
            and c.get("ts") not in links]


def _plan_has_rows(plan_text):
    rows = [ln for ln in _section(plan_text, "## 改哪些文件").splitlines()
            if ln.strip().startswith("|")]
    for ln in rows[2:]:
        if any(c.strip() for c in ln.strip().strip("|").split("|")):
            return True
    return False


def _jsonl_lines(p):
    if not p.exists():
        return []
    return [x for x in p.read_text(encoding="utf-8-sig").splitlines() if x.strip()]


def _load_json(p):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return "INVALID"


def last_ac_check(d):
    lines = _jsonl_lines(d / "ac-check.jsonl")
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def _changes_since_ac_check(d):
    """自上次 ac-check 之后新增了几条改动。"""
    ac = last_ac_check(d)
    last_ts = (ac or {}).get("ts", "") if ac else ""
    return len([c for c in load_changes(d)
                if c.get("kind") != "link" and (not last_ts or str(c.get("ts", "")) > last_ts)])


def _need_story(slug):
    d = story_path(slug)
    if not d:
        print(f"找不到 story: {slug}", file=sys.stderr)
        print(f"  当前数据目录：{STORY_DIR}", file=sys.stderr)
        print("  看所有 story：python aiwf.py where", file=sys.stderr)
    return d


# ---------------- 命令 ----------------

def cmd_init(args):
    STORY_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now().strftime("%Y-%m-%d")
    slug = slugify(args.slug)
    key = safe_key(getattr(args, "key", "") or "")

    # 同一个 tracker 号只能有一个 story
    if key:
        for d0 in STORY_DIR.iterdir():
            if not d0.is_dir():
                continue
            m0 = load_meta(d0) or {}
            if str(m0.get("key", "")).upper() == key:
                print(f"ERROR：{key} 已经有一个 story 了：{d0.name}", file=sys.stderr)
                print("  同一个单号不要建第二个；需要继续做就打开原来那个。", file=sys.stderr)
                return 6

    # 目录名：<日期>-<KEY>-<slug>；没有 key 时退回当日序号
    if key:
        sid = f"{date}-{key}"
        d = STORY_DIR / f"{sid}-{slug}"
        if d.exists():
            print(f"ERROR：目录已存在：{d}", file=sys.stderr)
            return 6
    else:
        seq = 1
        while True:
            sid = f"{date}-{seq:03d}"
            if not any(p.name.startswith(sid) for p in STORY_DIR.iterdir() if p.is_dir()):
                break
            seq += 1
        d = STORY_DIR / f"{sid}-{slug}"
    d.mkdir(parents=True)

    meta = json.loads(json.dumps(META_TMPL))
    parents = []
    for pk in (args.parent or []):
        pd = story_path(pk)
        if not pd:
            print(f"WARN：找不到父 story {pk}，仍按文本记录", file=sys.stderr)
            parents.append(str(pk))
        else:
            parents.append((load_meta(pd) or {}).get("key") or pk)
    meta.update(id=d.name, key=key, slug=slug, title=args.title or slug,
                created=now(), jira=key, type=args.type, parents=parents,
                repos=[{"name": r, "path": "", "status": "pending"} for r in (args.repo or [])])
    save_meta(d, meta)
    (d / "requirement.md").write_text(REQ_TMPL.format(**meta), encoding="utf-8")
    (d / "decisions.md").write_text(DEC_TMPL.format(title=meta["title"]), encoding="utf-8")
    (d / "plan.md").write_text(PLAN_TMPL.format(title=meta["title"]), encoding="utf-8")
    (d / "changes.jsonl").write_text("", encoding="utf-8")

    print(f"created  {d}")
    print(f"id       {d.name}")
    print(f"key      {key or '（无，用当日序号）'}")
    if args.type != "feature":
        print(f"type     {args.type}" + (f"　parent={'/'.join(parents)}" if parents else ""))
    print(f"next     {NEXT_ACTION['new']}")
    return 0


def cmd_acs(args):
    """列出验收条件、编号与认领情况。"""
    d = _need_story(args.story)
    if not d:
        return 1
    crits = parse_criteria(read_text(d, "requirement.md"))
    if not crits:
        print("没有解析到验收条件。在 requirement.md 的「## 验收条件」下写 `- [ ] ...`。")
        return 0
    claims, stale = ac_claims(d)
    deferred = _deferred(d)
    unlinked = unlinked_changes(d)

    print(f"{'ID':<8}{'认领':>5}{'转交':>5}  验收条件")
    print("-" * 78)
    for c in crits:
        n = len(claims.get(c["id"], []))
        mark = f"{n}" if n else "-"
        dfr = "→" if c["id"] in deferred else ""
        print(f"{c['id']:<8}{mark:>5}{dfr:>5}  {c['text']}")
        if c["id"] in deferred:
            info = deferred[c["id"]] or {}
            print(f"         └ 已转交：{info.get('owner') or '?'} — {info.get('reason') or ''}")

    missing = [c["id"] for c in crits
               if c["id"] not in claims and c["id"] not in deferred]
    print()
    if stale:
        print(f"⚠ 过期认领：{', '.join(sorted(stale))}  ← AC 文本改过了，旧认领失效，需要重新 record")
    if missing:
        print(f"未认领：{', '.join(missing)}  ← 这些还没有任何改动说明在做")
    else:
        print("全部认领或已转交。")
    if deferred:
        print(f"已转交 {len(deferred)} 条（不算在 gate 要求里）")
    if unlinked:
        print(f"未挂验收条件的改动：{len(unlinked)} 条  ← 可能越界")
    return 0


def _local_stories():
    """仓库内的 ./stories（历史位置）。"""
    return ROOT / "stories"


def data_dir_report(verbose=False):
    """当前数据目录、来源，以及"分裂"警告。"""
    env = os.environ.get("AIWF_DATA", "").strip()
    src = f"AIWF_DATA={env}" if env else "默认（仓库内 ./stories）"
    lines = [f"数据目录  {STORY_DIR}", f"来源      {src}"]

    local = _local_stories()
    if local.exists():
        n = len([p for p in local.iterdir() if p.is_dir()])
        same = local.resolve() == STORY_DIR.resolve()
        if n and not same:
            lines.append(f"⚠ 分裂    {local} 里还有 {n} 个 story，不在当前数据目录")
            lines.append(f"          统一：python aiwf.py adopt    # 搬到 {STORY_DIR}")
        elif verbose:
            lines.append(f"          仓库内 ./stories：{'同一个目录' if same else '空'}")
    if not env and verbose:
        lines.append('提示     想放仓库外：setx AIWF_DATA "<路径>"，然后【重开终端】')
    return lines


def _print_data_dir(verbose=False):
    for ln in data_dir_report(verbose=verbose):
        print(ln)


def cmd_selftest(args):
    """端到端自检：在临时目录里跑一遍完整流程，逐阶段验证门禁。

    改门禁/状态机之后必须先跑这个——历史上三次回归都是靠它才发现的。
    """
    import subprocess
    import uuid

    script = str(Path(__file__).resolve())
    # 建在工作区内，且用普通 mkdir：mkdtemp 建出的目录在受限环境下可能不可列举
    tmp_path = ROOT / (".selftest-" + uuid.uuid4().hex[:8])
    tmp_path.mkdir(parents=True, exist_ok=False)
    tmp = str(tmp_path)
    env = dict(os.environ)
    env["AIWF_DATA"] = tmp
    env["PYTHONIOENCODING"] = "utf-8"

    def run(*argv):
        r = subprocess.run([sys.executable, script, *argv],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=60)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    cases = []

    def case(name, ok, detail=""):
        cases.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if not ok and detail:
            line += f"\n         {detail.strip().splitlines()[0][:110] if detail.strip() else ''}"
        print(line)

    def write_story(slug, acs):
        d = Path(tmp) / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "requirement.md").write_text(
            "## 澄清问答\n- Q: a A: b\n\n## 验收条件\n" + acs + "\n", encoding="utf-8")
        (d / "plan.md").write_text(
            "## 改哪些文件\n| repo | 文件 | 改什么 | 为什么 |\n|---|---|---|---|\n"
            "| r | a.java | x | y |\n", encoding="utf-8")
        (d / "changes.jsonl").write_text("", encoding="utf-8")
        return d.name

    try:
        # ── 准备 ──
        code, out = run("init", "self", "--key", "ST-1", "--title", "自检")
        case("init 成功", code == 0, out)

        name = None
        for cand in sorted(Path(tmp).iterdir()):
            if cand.is_dir():
                name = cand.name
                break
        case("story 目录已创建", name is not None)

        write_story(name, "- [ ] AC-1 甲\n- [ ] AC-2 乙")

        code, out = run("gate", "ST-1")
        case("new 阶段门禁通过", code == 0, out)

        # ── implementing：挂 AC 检查 ──
        code, out = run("record", "ST-1", "--stage", "implementing", "--file", "a.java")
        code, out = run("gate", "ST-1")
        case("implementing 未挂 AC 被拦", code == 2 and "没挂验收条件" in out, out)

        # 把刚才那笔漏挂 AC 的改动补挂掉，否则它会一直让 gate 红（这是正确行为）
        import json as _json
        ch = (Path(tmp) / name / "changes.jsonl").read_text(encoding="utf-8").splitlines()
        stray = [_json.loads(x) for x in ch if x.strip()]
        stray_ts = [c["ts"] for c in stray if not c.get("criteria")]
        if stray_ts:
            run("link", "ST-1", "--target", stray_ts[-1], "--criterion", "AC-1")

        code, out = run("record", "ST-1", "--file", "a2.java", "--criterion", "AC-1", "--why", "x")
        code, out = run("gate", "ST-1")
        case("AC-2 未认领被拦", code == 2 and "AC-2" in out, out)

        code, out = run("record", "ST-1", "--file", "b.java", "--criterion", "AC-2", "--why", "y")
        code, out = run("gate", "ST-1")
        case("全部认领后 implementing 通过", code == 0, out)

        # ── AC 版本绑定 ──
        req = Path(tmp) / name / "requirement.md"
        req.write_text(req.read_text(encoding="utf-8").replace(
            "AC-1 甲", "AC-1 甲（改写）"), encoding="utf-8")
        code, out = run("gate", "ST-1")
        case("AC 改写后旧认领失效被拦", code == 2 and "旧认领已失效" in out, out)
        run("record", "ST-1", "--file", "a3.java", "--criterion", "AC-1", "--why", "重认领")
        code, out = run("gate", "ST-1")
        case("重新认领后恢复", code == 0, out)

        # ── verify 结果 ──
        run("ac-record", "ST-1", "--verdict", "pass", "--fulfilled", "AC-1", "--fulfilled", "AC-2")
        run("record", "ST-1", "--stage", "verifying", "--kind", "note", "--note", "s")
        run("verify", "ST-1", "--cmd", "mvn t", "--result", "fail", "--commit", "c1")
        run("handoff", "ST-1", "--commit", "c1")
        code, out = run("gate", "ST-1")
        case("verify 失败被拦", code == 2 and "不是 pass" in out, out)

        run("verify", "ST-1", "--cmd", "mvn t", "--result", "pass", "--commit", "c1")
        code, out = run("gate", "ST-1")
        case("verify 通过后 verifying 通过", code == 0, out)

        # ── commit 绑定 ──
        run("handoff", "ST-1", "--commit", "c2")
        code, out = run("gate", "ST-1")
        case("代码变了旧 verified 过期被拦", code == 2 and "代码变了" in out, out)
        run("handoff", "ST-1", "--commit", "c1")

        # ── 缺陷 ──
        run("defect", "open", "ST-1", "缺陷甲", "--severity", "major")
        code, out = run("gate", "ST-1")
        case("有 open 缺陷被拦", code == 2 and "没修" in out, out)

        run("defect", "fix", "ST-1", "D-1", "--commit", "c1")
        code, out = run("gate", "ST-1")
        case("修缺陷后对账失效被拦", code == 2 and "兑现检查" in out, out)

        run("ac-record", "ST-1", "--verdict", "pass", "--fulfilled", "AC-1", "--fulfilled", "AC-2")
        run("review", "ST-1", "--independent", "--verdict", "pass", "--commit", "c1")
        code, out = run("gate", "ST-1")
        case("缺陷修了没复测被拦", code == 2 and "没复测" in out, out)

        code, out = run("defect", "verify", "ST-1", "D-1", "--result", "pass")
        case("复测不写 --by 被拒", code == 4, out)

        run("defect", "verify", "ST-1", "D-1", "--result", "pass", "--by", "tester")
        code, out = run("gate", "ST-1")
        case("复测后 reviewed 通过", code == 0, out)

        # ── 评审后改动 ──
        run("record", "ST-1", "--file", "c.java", "--criterion", "AC-1", "--why", "评审后又改")
        code, out = run("gate", "ST-1")
        case("评审后改动被拦", code == 2 and "评审已过期" in out, out)

        # ── 阶段校验 / 阻塞 / reopen ──
        code, out = run("record", "ST-1", "--stage", "blocked")
        case("非法阶段被拒", code == 4, out)

        run("record", "ST-1", "--blocked", "等口径", "--kind", "note")
        code, out = run("gate", "ST-1")
        case("阻塞只提示不拦", "处于阻塞" in out, out)

        code, out = run("reopen", "ST-1", "--reason", "追加")
        case("reopen 回到 clarifying", code == 0 and "clarifying" in out, out)
        code, out = run("gate", "ST-1")
        case("reopen 后旧评审不再算数", code == 0, out)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [c for c in cases if not c[1]]
    print()
    print(f"自检：{len(cases) - len(failed)}/{len(cases)} 通过")
    if failed:
        print("失败项：")
        for name, _, _ in failed:
            print("  -", name)
        return 1
    print("全部通过。")
    return 0


def cmd_defect(args):
    """缺陷：open / fix / verify / wontfix / duplicate / list。"""
    d = _need_story(args.story)
    if not d:
        return 1
    act = args.action

    if act == "list":
        states = defect_states(d)
        if not states:
            print("还没有缺陷记录。")
            return 0
        print(f"{'ID':<6}{'严重度':<9}{'来源':<8}{'状态':<10}标题")
        print("-" * 78)
        for did in sorted(states, key=lambda k: int(k[2:]) if k[2:].isdigit() else 0):
            v = states[did]
            print(f"{did:<6}{v.get('severity','-'):<9}{v.get('source','-'):<8}"
                  f"{v.get('status','-'):<10}{v.get('title','')}")
        op = len(open_defects(d))
        un = len(unverified_defects(d))
        print(f"\n未修 {op} 个，修了未复测 {un} 个")
        return 0

    if act == "open":
        if not args.title:
            print("ERROR：需要缺陷标题", file=sys.stderr)
            return 4
        did = _next_defect_id(d)
        _defect_append(d, {"event": "open", "id": did, "title": args.title,
                           "severity": args.severity, "source": args.source,
                           "related_ac": args.ac or [], "repro": args.repro or "",
                           "found_in": args.found_in or ""})
        print(f"defect {did} 已登记：{args.title}")
        print(f"  严重度 {args.severity}　来源 {args.source}"
              + (f"　关联 {', '.join(args.ac)}" if args.ac else ""))
        if not args.repro:
            print("  提醒：没写复现步骤（--repro），没复现的缺陷等于没提")
        return 0

    if not args.id:
        print("ERROR：需要缺陷编号（如 D-1）", file=sys.stderr)
        return 4
    states = defect_states(d)
    if args.id not in states:
        print(f"ERROR：没有 {args.id} 这个缺陷", file=sys.stderr)
        print(f"  现有：{', '.join(states) or '（无）'}", file=sys.stderr)
        return 4

    if act == "fix":
        if states[args.id].get("status") != "open":
            print(f"ERROR：{args.id} 当前是 {states[args.id].get('status')}，不是 open",
                  file=sys.stderr)
            return 4
        _defect_append(d, {"event": "fix", "id": args.id,
                           "commit": args.commit or "", "note": args.note or ""})
        print(f"defect {args.id} 已标记修复" + (f"（{args.commit}）" if args.commit else ""))
        # 修完缺陷 = 之前的验收条件对账不再可信 → 归档，强制重做
        arch = d / "archive"
        src = d / "ac-check.jsonl"
        if src.exists():
            arch.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.move(str(src), str(arch / f"ac-check.jsonl.{stamp}"))
            print("  缺陷修复后，上一次对账已归档失效——需要重新 ac-check")
        return 0

    if act == "verify":
        if states[args.id].get("status") != "fixed":
            print(f"ERROR：{args.id} 当前是 {states[args.id].get('status')}，"
                  "只有 fixed 才能复测", file=sys.stderr)
            return 4
        if args.result == "pass" and not args.by:
            print("ERROR：复测通过必须写 --by（谁复测的）。修复者不能自证。",
                  file=sys.stderr)
            return 4
        _defect_append(d, {"event": "verify", "id": args.id,
                           "result": args.result, "by": args.by or "",
                           "note": args.note or ""})
        if args.result == "pass":
            print(f"defect {args.id} 复测通过，关闭")
        else:
            print(f"defect {args.id} 复测未通过，退回 open")
        return 0

    if act in ("wontfix", "duplicate"):
        if not (args.reason or getattr(args, "note", "")):
            print(f"ERROR：{act} 必须写 --reason 说明原因", file=sys.stderr)
            return 4
        _defect_append(d, {"event": act, "id": args.id,
                           "note": args.reason or getattr(args, "note", "")})
        print(f"defect {args.id} 标记为 {act}")
        return 0

    print(f"ERROR：未知动作 {act}", file=sys.stderr)
    return 4


def _mask(value):
    v = value.strip().strip("\"'`").strip()
    if len(v) <= 4:
        return "*" * len(v)
    return v[:2] + "*" * max(3, len(v) - 4) + v[-2:]


def scan_for_secrets(root, limit=60):
    """返回 [(path, lineno, key, masked_value)]。"""
    found = []
    root = Path(root)
    if not root.exists():
        return found
    files = [root] if root.is_file() else [
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() not in SKIP_EXT
        and not any(part in SKIP_DIRS for part in p.parts)
        and p.stat().st_size < 2 * 1024 * 1024
    ]
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > 400:
                continue
            # key = value / key: value
            m = re.search(r"([A-Za-z0-9_.\-]{2,40})\s*[:=]\s*(\S.*)$", line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            if not SECRET_KEY.search(key):
                continue
            val = val.split()[0] if val.split() else val
            if len(val) < 4 or PLACEHOLDER.match(val):
                continue
            # 排除代码/注释：函数调用、带引号包裹的代码、注释行
            if any(ch in val for ch in "(){}[];"):
                continue
            if val.endswith((":", ",", ".")) or val.startswith(("#", "//")):
                continue
            if line.lstrip().startswith(("#", "//", "*")):
                continue
            # 纯字母且短、或像标识符（无数字无符号）的，多半不是真实密钥
            if re.fullmatch(r"[A-Za-z_.]+", val) and len(val) < 12:
                continue
            found.append((str(f), i, key, _mask(val)))
            if len(found) >= limit:
                return found

        # 账号/密码写在一行的形式（登录账号：user / Passw0rd）
        for i, line in enumerate(text.splitlines(), 1):
            if not any(k in line for k in ("账号", "用户名", "登录", "password", "密码")):
                continue
            if "/" not in line:
                continue
            m2 = re.search(r"[:：]\s*(\S+)\s*/\s*(\S+)", line)
            if not m2:
                continue
            secret = m2.group(2).strip()
            if len(secret) < 4 or PLACEHOLDER.match(secret):
                continue
            found.append((str(f), i, "账号/密码", _mask(secret)))
            if len(found) >= limit:
                return found
    return found


def _local_name(path):
    """configs/x.md -> configs/x.local.md"""
    p = Path(path)
    if p.suffix:
        return p.with_name(p.stem + ".local" + p.suffix)
    return p.with_name(p.name + ".local")


def _placeholder(path):
    if path.suffix.lower() in (".md", ".txt", ".rst"):
        return "******（真实值见 " + _local_name(path).name + "）"
    return "CHANGE_ME"


def fix_file(path, hits, apply=False):
    """把命中行里的真实值换成占位，原文另存为 .local。返回 (计划行, 是否已执行)。"""
    path = Path(path)
    local = _local_name(path)
    plan = [f"  {path.name} → 脱敏为模板；真实值存入 {local.name}"]
    if not apply:
        return plan, False
    if local.exists():
        plan.append(f"    跳过：{local.name} 已存在，不覆盖")
        return plan, False
    raw = io.open(path, encoding="utf-8", errors="replace").read()
    io.open(local, "w", encoding="utf-8", newline="").write(raw)

    lineno_set = {h[1] for h in hits}
    lines = raw.splitlines(keepends=True)
    ph = _placeholder(path)
    out = []
    for i, line in enumerate(lines, 1):
        if i in lineno_set:
            eol = "\n" if line.endswith("\n") else ""
            m = re.search(r"([A-Za-z0-9_.\-]{2,40})(\s*[:=]\s*)(\S.*?)(\s*)$", line.rstrip("\n"))
            if m:
                line = line[:m.start(3)] + ph + m.group(4) + eol
            else:
                # 账号/密码写在一行：登录账号：user / Passw0rd
                m2 = re.search(r"(\s*/\s*)(\S+?)(\s*)$", line.rstrip("\n"))
                if m2 and any(k in line for k in ("账号", "用户名", "登录", "password")):
                    line = line[:m2.start(2)] + ph + m2.group(3) + eol
        out.append(line)
    io.open(path, "w", encoding="utf-8", newline="").write("".join(out))
    plan.append(f"    已脱敏 {len(lineno_set)} 行；原文件 {local.name} 保存真实值")
    return plan, True


def _ensure_gitignore(start, rules, apply=False):
    """在最近的 git 仓库根写 .gitignore 规则。返回 (路径, 新增规则列表)。"""
    d = Path(start).resolve()
    root = None
    for cand in [d] + list(d.parents):
        if (cand / ".git").exists():
            root = cand
            break
    if root is None:
        return None, []
    gi = root / ".gitignore"
    existing = io.open(gi, encoding="utf-8").read() if gi.exists() else ""
    need = [r for r in rules if r not in existing.splitlines()]
    if need and apply:
        with io.open(gi, "a", encoding="utf-8", newline="") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n# 真实配置（含凭据）不入库\n")
            for r in need:
                f.write(r + "\n")
    return str(gi), need


def cmd_scan(args):
    """扫描明文凭据（默认扫 story 目录；可指定任意目录）。"""
    if args.story:
        d = _need_story(args.story)
        if not d:
            return 1
        roots = [d]
    elif args.path:
        roots = [Path(args.path).expanduser()]
    else:
        roots = [STORY_DIR]

    total = 0
    by_file = {}
    for root in roots:
        hits = scan_for_secrets(root)
        print(f"扫描  {root}")
        if not hits:
            print("  未发现疑似明文凭据。")
            continue
        for path, lineno, key, masked in hits:
            rel = path.replace(str(root), ".") if str(root) in path else path
            print(f"  {rel}:{lineno}  {key} = {masked}")
            by_file.setdefault(path, []).append((path, lineno, key, masked))
        total += len(hits)

    if not total:
        return 0

    print(f"\n共 {total} 处疑似明文凭据。")

    if not args.fix:
        print("  处理：真实值移到环境变量或 .local 文件（不入库），文件里只留变量名；")
        print("        已经提交过的，先轮换凭据再清理历史（清历史不等于失效）。")
        print("  自动拆分：python aiwf.py scan <路径> --fix   （先预览，加 --yes 执行）")
        return 8

    # ── --fix 模式 ──
    print("\n拆分计划：")
    planned = []
    for path, hits in by_file.items():
        plan, _ = fix_file(path, hits, apply=False)
        for line in plan:
            print(line)
        planned.append((path, hits))

    rules = ["*.local.md", "*.local.*", "*.local"]
    gi, need = _ensure_gitignore(Path(list(by_file.keys())[0]).parent, rules, apply=False)
    if gi:
        print(f"\n.gitignore：{gi}")
        if need:
            for r in need:
                print(f"  + {r}")
        else:
            print("  规则已存在")
    else:
        print("\n.gitignore：未找到 git 仓库根，需手动添加：")
        for r in rules:
            print(f"  + {r}")

    if not args.yes:
        print("\n这是预览。确认后加 --yes 执行拆分。")
        return 8

    print("\n执行：")
    for path, hits in planned:
        plan, _ = fix_file(path, hits, apply=True)
        for line in plan:
            print(line)
    if gi and need:
        _ensure_gitignore(Path(list(by_file.keys())[0]).parent, rules, apply=True)
        print(f"  已写入 .gitignore：{', '.join(need)}")

    print("\n完成。接下来：")
    print("  ① git status 确认模板文件已改、.local 文件未被跟踪")
    print("  ② 若这些凭据曾提交过 git，仍需轮换（清历史不等于失效）")
    return 8


def cmd_where(args):
    """打印所有路径与统计，用来排查"文件不一样"。"""
    _print_data_dir(verbose=True)
    print(f"代码目录  {ROOT}")
    print()
    for label, p in (("当前数据目录", STORY_DIR), ("仓库内 stories", _local_stories())):
        if p.exists():
            ds = [d for d in p.iterdir() if d.is_dir()]
            print(f"{label}：{p}  →  {len(ds)} 个 story")
            for d in sorted(ds):
                m = load_meta(d) or {}
                print(f"    {d.name}   key={m.get('key') or '-'}  stage={m.get('stage', '?')}")
        else:
            print(f"{label}：{p}  →  不存在")
    print()
    print("如果两个地方都有 story，说明环境变量只在部分终端生效。")
    print("统一到外部目录：setx AIWF_DATA \"<路径>\" → 重开终端 → python aiwf.py adopt")
    return 0


def cmd_adopt(args):
    """把仓库内 ./stories 的 story 搬到当前数据目录。"""
    local = _local_stories()
    if not local.exists():
        print(f"{local} 不存在，没有要搬的。")
        return 0
    if local.resolve() == STORY_DIR.resolve():
        print(f"{local} 就是当前数据目录，不需要搬。")
        return 0

    ds = sorted([d for d in local.iterdir() if d.is_dir()])
    if not ds:
        print(f"{local} 里没有 story。")
        return 0

    print(f"从 {local}")
    print(f"到 {STORY_DIR}")
    print(f"共 {len(ds)} 个：")
    for d in ds:
        m = load_meta(d) or {}
        print(f"  {d.name}   key={m.get('key') or '-'}  stage={m.get('stage', '?')}")

    if not args.yes:
        print("\n这是预览。确认后加 --yes 执行搬迁。")
        return 0

    STORY_DIR.mkdir(parents=True, exist_ok=True)
    moved = skipped = 0
    for d in ds:
        target = STORY_DIR / d.name
        if target.exists():
            print(f"  跳过（目标已存在）：{d.name}")
            skipped += 1
            continue
        shutil.move(str(d), str(target))
        moved += 1
    # 清掉空的 ./stories
    try:
        if local.exists() and not any(local.iterdir()):
            local.rmdir()
            print("  已删除空的 ./stories")
    except OSError:
        pass
    print(f"\n完成：搬了 {moved} 个，跳过 {skipped} 个")
    _print_data_dir()
    return 0


def cmd_status(args):
    if not STORY_DIR.exists():
        print(f"数据目录 {STORY_DIR} 还不存在。先跑 init。")
        _print_data_dir()
        return 0
    rows = []
    for d in sorted(STORY_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = load_meta(d) or {}
        stage = meta.get("stage", "?")
        if stage == "done" and not args.all:
            continue
        ac = last_ac_check(d)
        last = (ac or {}).get("verdict", "-") if ac else "-"
        mark = {"block": "x", "warn": "!"}.get(last, "")
        missing = len(unclaimed_acs(d))
        created = str(meta.get("created", ""))[:10] or "-"
        show_key = meta.get("key") or meta.get("jira") or "-"
        n_df = len(open_defects(d))
        if meta.get("blocked"):
            stage = stage + "⏸"
        rows.append((created, show_key, stage, last + mark,
                     f"{missing}" if missing else "-",
                     len(load_changes(d)), str(n_df) if n_df else "-",
                     meta.get("title", ""), meta.get("slug", "")))
    if not rows:
        print("没有进行中的 story（加 --all 看已完成）。")
        return 0
    print(f"{'DATE':<12}{'KEY':<13}{'STAGE':<13}{'AC':>4}{'缺':>4}{'CHG':>4}{'缺陷':>5}  TITLE")
    print("-" * 96)
    for created, key, stage, last, miss, n, ndf, title, slug in rows:
        label = title or slug
        print(f"{created:<12}{key:<13}{stage:<13}{last:>4}{miss:>4}{n:>4}{ndf:>5}  {label}")
    print("\nAC: 最近一次兑现检查 pass / warn(!) / block(x) / -（还没查）")
    print("缺: 还没有改动认领的验收条件数量")
    print("⏸ : 已标记阻塞（原因见 context）")
    print()
    _print_data_dir()
    return 0


def cmd_context(args):
    d = _need_story(args.story)
    if not d:
        return 1
    meta = load_meta(d) or {}
    h = meta.get("handoff") or {}

    out = [f"# Story：{meta.get('title')}", ""]
    out += [f"- id: `{meta.get('id')}`", f"- stage: **{meta.get('stage')}**",
            f"- jira: {meta.get('jira') or '（未填）'}"]
    repos = meta.get("repos") or []
    if repos:
        out.append("- repos: " + "、".join(r["name"] for r in repos))
    out += ["", "> 你是本项目的开发助手。下面是你需要的全部上下文。",
            "> 只基于这些信息工作；信息不足时提问，不要假设。", ""]

    if any(h.values()):
        out += ["## 交接指针（从这里接手，不要去翻历史对话）", ""]
        if h.get("branch"):
            out.append(f"- 分支：`{h['branch']}`")
        if h.get("pr"):
            out.append(f"- PR：**{h['pr']}**（用 `gh pr view {h['pr']}` 拉详情）")
        if h.get("commit"):
            out.append(f"- 最近提交：`{h['commit']}`")
        if h.get("todo"):
            out.append(f"- 下一步待办：{h['todo']}")
        out.append("")

    crits = parse_criteria(read_text(d, "requirement.md"))
    if crits:
        claims, stale_c = ac_claims(d)
        deferred_c = _deferred(d)
        out += ["## 验收条件与认领", ""]
        for c in crits:
            if c["id"] in deferred_c:
                info = deferred_c[c["id"]] or {}
                out.append(f"- **{c['id']}** {c['text']}　→ 已转交 {info.get('owner') or '?'}")
                continue
            n = len(claims.get(c["id"], []))
            out.append(f"- **{c['id']}** {c['text']}" + (f"　（已认领 {n} 处）" if n else "　**（还没有人认领）**"))
        if stale_c:
            out += ["", f"⚠ 过期认领（AC 文本改过，需重新 record）：{', '.join(sorted(stale_c))}"]
        out.append("")
        out += ["**每改一处，必须用 record 挂上它兑现的验收条件**：", "",
                "```", "python aiwf.py record <story> --file <路径> --criterion <AC-x> --why \"<一句话>\"",
                "```", ""]

    for name, label in (("decisions.md", "已定决策"), ("plan.md", "实施方案")):
        t = read_text(d, name)
        if t.strip():
            out += [f"## {label}（{name}）", "", t.strip(), ""]

    changes = load_changes(d)
    if changes:
        out += ["## 最近改动", ""]
        for c in changes[-10:]:
            acs = "/".join(c.get("criteria") or []) or "未挂"
            why = f"　why: {c['why']}" if c.get("why") else ""
            out.append(f"- {c.get('ts')} [{c.get('stage')}] {acs} {c.get('repo', '-')}: "
                       f"{', '.join(c.get('files', []))} — {c.get('note', '')}{why}")
        out.append("")

    ac = last_ac_check(d)
    if ac:
        out += ["## 最近一次兑现检查", "", f"- 判定：**{ac.get('verdict')}**"]
        for item in ac.get("acs", []):
            flag = "已兑现" if item.get("fulfilled") else "**未兑现**"
            out.append(f"- {item.get('id')}：{flag}　{item.get('evidence', '')}")
        for it in ac.get("undeclared", []):
            out.append(f"- 未声明改动：{it}")
        out.append("")

    v = _load_json(d / "verify.json")
    if isinstance(v, dict):
        out += ["## 验证结果", "", "```json",
                json.dumps(v, ensure_ascii=False, indent=2), "```", ""]

    r = _load_json(d / "review.json")
    if isinstance(r, dict):
        out += ["## 评审结论", "",
                f"- 独立评审：{'是' if r.get('independent') else '否（不合规）'}　"
                f"结论：**{r.get('verdict')}**　评审者：{r.get('reviewer') or '-'}"]
        for it in r.get("issues", []):
            out.append(f"- 问题：{it}")
        out.append("")

    if meta.get("blocked"):
        b = meta["blocked"]
        out += ["## 阻塞中", "", f"- 原因：{b.get('reason') or '-'}",
                f"- 自：{b.get('since') or '-'}",
                "- 解除：python aiwf.py record <story> --unblocked", ""]

    out += ["## 你现在要做的事", "", NEXT_ACTION.get(meta.get("stage", "new"), "")]
    print("\n".join(out))
    return 0


def cmd_record(args):
    d = _need_story(args.story)
    if not d:
        return 1
    meta = load_meta(d) or {}
    stage = args.stage or meta.get("stage")

    if args.stage and args.stage not in STAGES:
        print(f"ERROR：没有这个阶段：{args.stage}", file=sys.stderr)
        print(f"  可用：{', '.join(STAGES)}", file=sys.stderr)
        print("  被卡住（等需求方/等依赖）用 --blocked，那不是阶段。", file=sys.stderr)
        return 4

    crits = {c["id"]: c for c in parse_criteria(read_text(d, "requirement.md"))}
    given = args.criterion or []
    bad = [c for c in given if c not in crits]
    if bad:
        print(f"ERROR：这些验收条件不存在：{', '.join(bad)}", file=sys.stderr)
        if crits:
            print(f"  可用：{', '.join(sorted(crits))}", file=sys.stderr)
        else:
            print("  requirement.md 里还没有解析到验收条件", file=sys.stderr)
        return 4

    deferred = _deferred(d)
    dgiven = [c for c in given if c in deferred]
    if dgiven:
        print(f"ERROR：这些 AC 已经转交出去了，不能在本 story 认领：{', '.join(dgiven)}", file=sys.stderr)
        print("  需要拿回来：python aiwf.py undefer <story> " + " ".join(dgiven), file=sys.stderr)
        return 7

    # 认领时把 AC 的当前文本哈希一起记下——AC 之后被改写，这条认领就自动失效
    ac_hashes = {c: crits[c]["hash"] for c in given}

    entry = {"ts": now(), "stage": stage, "repo": args.repo or "",
             "files": args.file or [], "kind": args.kind or "code",
             "criteria": given, "acHashes": ac_hashes,
             "why": args.why or "", "note": args.note or ""}
    with (d / "changes.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if args.blocked:
        meta["blocked"] = {"reason": args.blocked, "since": now()}
        save_meta(d, meta)
        print(f"  已标记阻塞：{args.blocked}")
        print("  解除：python aiwf.py record <story> --unblocked")
    if args.unblocked and meta.get("blocked"):
        prev = meta.pop("blocked") or {}
        save_meta(d, meta)
        print(f"  已解除阻塞（原原因：{prev.get('reason') or '-'}）")

    if args.stage and args.stage in STAGES and args.stage != meta.get("stage"):
        meta["stage"] = args.stage
        save_meta(d, meta)

    acs = "/".join(given) or "未挂"
    print(f"recorded  {stage}  {acs}  {entry['repo'] or '-'}  {len(entry['files'])} file(s)")
    if stage in WORK_STAGES and entry["kind"] in ("code", "test") and not given:
        missing = [c["id"] for c in unclaimed_acs(d)]
        print("  提醒：这次改动没挂验收条件（--criterion AC-x）。"
              + (f" 目前还没人认领：{', '.join(missing)}" if missing else ""))

    # 提示该对账了（工具没法自动跑外部 agent，但可以在需要时提醒并准备好提示词）
    since = _changes_since_ac_check(d)
    if stage in WORK_STAGES and since and since % AUTO_AC_HINT_EVERY == 0:
        print(f"  提示：自上次对账以来已有 {since} 条改动，该做一次兑现检查了：")
        print(f"        python aiwf.py ac-check {meta.get('key') or args.story} > ac-prompt.md")
    return 0


def cmd_defer(args):
    """把 AC 转交出去（前端负责 / 另一条线负责），不再要求本 story 认领。"""
    d = _need_story(args.story)
    if not d:
        return 1
    crits = {c["id"] for c in parse_criteria(read_text(d, "requirement.md"))}
    ids = args.ac
    bad = [c for c in ids if c not in crits]
    if bad:
        print(f"ERROR：这些验收条件不存在：{', '.join(bad)}", file=sys.stderr)
        print(f"  可用：{', '.join(sorted(crits)) or '（无）'}", file=sys.stderr)
        return 4

    meta = load_meta(d) or {}
    deferred = meta.setdefault("deferred", {})
    for cid in ids:
        deferred[cid] = {"owner": args.owner or "", "reason": args.reason or "", "at": now()}
    save_meta(d, meta)
    print(f"deferred  {', '.join(ids)}  owner={args.owner or '?'}  reason={args.reason or ''}")
    print("  这些 AC 不再要求本 story 认领；对账时也会标为「不在范围内」。")
    return 0


def cmd_undefer(args):
    """把转交出去的 AC 拿回来。"""
    d = _need_story(args.story)
    if not d:
        return 1
    meta = load_meta(d) or {}
    deferred = meta.get("deferred") or {}
    ids = args.ac
    gone = [c for c in ids if c not in deferred]
    for cid in ids:
        deferred.pop(cid, None)
    meta["deferred"] = deferred
    save_meta(d, meta)
    print(f"undeferred  {', '.join([c for c in ids if c not in gone]) or '（无变化）'}")
    if gone:
        print(f"  这些本来就没转交：{', '.join(gone)}")
    return 0


def cmd_handoff(args):
    d = _need_story(args.story)
    if not d:
        return 1
    meta = load_meta(d) or {}
    h = meta.setdefault("handoff", {"branch": "", "pr": "", "commit": "", "todo": ""})
    for k in ("branch", "pr", "commit", "todo"):
        v = getattr(args, k, None)
        if v:
            h[k] = v
    save_meta(d, meta)
    print("handoff  " + "  ".join(f"{k}={v}" for k, v in h.items() if v))
    return 0


def cmd_link(args):
    """给一条已记录的改动补挂验收条件（事后发现漏挂时用）。"""
    d = _need_story(args.story)
    if not d:
        return 1
    crits = {c["id"] for c in parse_criteria(read_text(d, "requirement.md"))}
    bad = [c for c in (args.criterion or []) if c not in crits]
    if bad:
        print(f"ERROR：这些验收条件不存在：{', '.join(bad)}", file=sys.stderr)
        print(f"  可用：{', '.join(sorted(crits)) or '（无）'}", file=sys.stderr)
        return 4
    crit_map = {c["id"]: c["hash"] for c in parse_criteria(read_text(d, "requirement.md"))}
    deferred = _deferred(d)
    dgiven = [c for c in (args.criterion or []) if c in deferred]
    if dgiven:
        print(f"ERROR：这些 AC 已转交，不能补挂：{', '.join(dgiven)}", file=sys.stderr)
        print("  需要拿回来：python aiwf.py undefer <story> " + " ".join(dgiven), file=sys.stderr)
        return 7

    target = [c for c in load_changes(d) if c.get("ts") == args.target]
    if not target:
        print(f"ERROR：找不到 ts={args.target} 的改动记录", file=sys.stderr)
        print("  可用 ts：", file=sys.stderr)
        for c in load_changes(d):
            if c.get("kind") != "link":
                print(f"    {c.get('ts')}  {', '.join(c.get('files') or []) or '-'}", file=sys.stderr)
        return 5

    entry = {"ts": now(), "kind": "link", "target": args.target,
             "criteria": args.criterion or [],
             "acHashes": {c: crit_map[c] for c in (args.criterion or [])},
             "stage": target[0].get("stage"),
             "repo": target[0].get("repo", ""), "files": target[0].get("files") or [],
             "why": args.why or "", "note": "补挂验收条件"}
    with (d / "changes.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"linked  {args.target}  ->  {'/'.join(args.criterion or [])}")
    return 0


def cmd_ac_check(args):
    """生成「验收条件兑现检查」提示词，交给一个新 session 的 agent。"""
    d = _need_story(args.story)
    if not d:
        return 1
    meta = load_meta(d) or {}
    crits = parse_criteria(read_text(d, "requirement.md"))
    claims, stale = ac_claims(d)
    deferred = _deferred(d)

    if not crits:
        print("没有解析到验收条件，无法对账。先补 requirement.md 的「## 验收条件」。", file=sys.stderr)
        return 1

    lines = []
    for c in crits:
        if c["id"] in deferred:
            info = deferred[c["id"]] or {}
            lines.append(f"- **{c['id']}** {c['text']}")
            lines.append(f"  - 【已转交，不在本 story 范围】{info.get('owner') or '?'}：{info.get('reason') or ''}")
            continue
        lines.append(f"- **{c['id']}** {c['text']}")
        got = claims.get(c["id"], [])
        if got:
            for g in got:
                files = ", ".join(g.get("files") or []) or "-"
                why = g.get("why") or g.get("note") or ""
                lines.append(f"  - 认领：{g.get('repo', '-')} / {files} — {why}")
        else:
            lines.append("  - **【没有任何改动认领】**")
    acs = "\n".join(lines)

    changes = load_changes(d)
    if changes:
        cl = []
        for c in changes[-15:]:
            acs2 = "/".join(c.get("criteria") or []) or "未挂"
            cl.append(f"- {c.get('ts')} [{c.get('stage')}] {acs2} {c.get('repo', '-')}: "
                      f"{', '.join(c.get('files', []))} — {c.get('why') or c.get('note') or ''}")
        change_txt = "\n".join(cl)
    else:
        change_txt = "（还没有改动记录）"

    hist = last_ac_check(d)
    if hist:
        history = (f"- {hist.get('ts')} verdict={hist.get('verdict')}\n"
                   f"- 未兑现：{[i.get('id') for i in hist.get('acs', []) if not i.get('fulfilled')]}\n"
                   f"- 未声明改动：{hist.get('undeclared', [])}")
    else:
        history = "（这是第一次检查）"

    print(AC_PROMPT.format(acs=acs, changes=change_txt, history=history))
    return 0


def cmd_ac_record(args):
    d = _need_story(args.story)
    if not d:
        return 1
    fulfilled = [a.split("=")[0] for a in (args.fulfilled or [])]
    entry = {"ts": now(), "verdict": args.verdict,
             "acs": [{"id": c["id"], "fulfilled": c["id"] in fulfilled,
                      "acText": c["text"]} for c in parse_criteria(read_text(d, "requirement.md"))],
             "undeclared": args.issue or [], "reviewer": args.reviewer or "",
             "note": args.note or ""}
    # 允许用 --fulfilled AC-1 精确标注；没给则按 verdict 推断（pass=全兑现）
    if not args.fulfilled:
        for item in entry["acs"]:
            item["fulfilled"] = (args.verdict == "pass")

    with (d / "ac-check.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    miss = [i["id"] for i in entry["acs"] if not i["fulfilled"]]
    print(f"ac-check  {entry['verdict']}  未兑现={miss or '无'}  未声明={len(entry['undeclared'])}")
    if entry["verdict"] == "block":
        print("  -> 判定 block：有验收条件未兑现。gate 会拦住，先补齐再往下走。")
    return 0


def cmd_reopen(args):
    """需求追加/变更：把已有 story 退回某个阶段，并让旧的验证/评审/对账失效。

    历史文件不删，移到 archive/ 里留档。
    """
    d = _need_story(args.story)
    if not d:
        return 1
    target = args.stage or "clarifying"
    if target not in STAGES:
        print(f"ERROR：阶段不存在：{target}　可选：{', '.join(STAGES)}", file=sys.stderr)
        return 4

    meta = load_meta(d) or {}
    before = meta.get("stage", "?")

    arch = d / "archive"
    arch.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    moved = []
    for fname in ("verify.json", "review.json", "ac-check.jsonl"):
        src = d / fname
        if src.exists():
            shutil.move(str(src), str(arch / f"{fname}.{stamp}"))
            moved.append(fname)

    entry = {"ts": now(), "stage": target, "repo": "", "files": [], "kind": "note",
             "criteria": [], "why": args.reason or "", "note": f"reopen: {before} -> {target}"}
    with (d / "changes.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + chr(10))

    meta["stage"] = target
    save_meta(d, meta)

    print(f"reopened  {before} -> {target}")
    print(f"  原因：{args.reason or '（未填）'}")
    if moved:
        print(f"  已归档（失效，需重做）：{', '.join(moved)}")
    print("  下一步：在 requirement.md 的「## 验收条件」追加新 AC（如 AC-11），再 record 挂上它。")
    return 0


def cmd_verify(args):
    d = _need_story(args.story)
    if not d:
        return 1
    p = d / "verify.json"
    data = _load_json(p)
    if not isinstance(data, dict):
        data = {"checks": []}
    data["commit"] = (getattr(args, "commit", "") or "").strip()
    data.setdefault("checks", []).append(
        {"ts": now(), "cmd": args.cmd, "result": args.result, "detail": args.detail or ""})
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    meta = load_meta(d) or {}
    if args.stage and args.stage in STAGES:
        meta["stage"] = args.stage
        save_meta(d, meta)
    print(f"verify  {args.result}  {args.cmd}")
    if args.result != "pass":
        print("  -> 验证未通过：修完再跑一次；gate 会拦。")
    return 0


def cmd_review(args):
    d = _need_story(args.story)
    if not d:
        return 1
    if not args.independent:
        print("REJECTED：评审必须用独立 session / 独立 agent。")
        print("  同上下文评审会被之前的推理带偏（自我确认偏差），等于没评。")
        print("  确认后加 --independent 重跑。")
        return 3
    meta = load_meta(d) or {}
    # 记下「评的是哪一份代码」——否则下次说不清最新代码有没有被评过
    commit = (args.commit or "").strip()
    data = {"ts": now(), "independent": True, "reviewer": args.reviewer or "",
            "verdict": args.verdict, "issues": args.issue or [],
            "commit": commit, "range": (args.range or "").strip()}
    (d / "review.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    meta["stage"] = "reviewed" if args.verdict == "pass" else "implementing"
    save_meta(d, meta)
    print(f"review  {args.verdict}  by {args.reviewer or '?'}  -> stage={meta['stage']}")
    if commit:
        print(f"        评审对象：{commit}" + (f"（diff {args.range}）" if args.range else ""))
    else:
        h = (meta.get("handoff") or {}).get("commit", "")
        print("  提醒：没记被评审的 commit。建议加 --commit <sha>"
              + (f"（当前 handoff 是 {h}）" if h else "")
              + "，否则以后分不清最新代码到底评过没有。")
    if args.verdict != "pass":
        print("  -> 打回 implementing：修完后要再走一次独立 review。")
    return 0


def cmd_gate(args):
    d = _need_story(args.story)
    if not d:
        return 1
    meta = load_meta(d) or {}
    stage = meta.get("stage", "new")
    rank = STAGES.index(stage) if stage in STAGES else 0
    problems = []
    hints = []

    if meta.get("blocked"):
        b = meta["blocked"]
        hints.append(f"story 处于阻塞（{b.get('since','?')}）：{b.get('reason') or '-'}")

    sec = scan_for_secrets(d, limit=5)
    if sec:
        hints.append(f"story 目录里有 {len(sec)} 处疑似明文凭据（如 {sec[0][2]}），"
                     "建议移出并轮换；详见 python aiwf.py scan --story 或 scan")

    if rank >= 1:                                   # clarifying 起
        req = read_text(d, "requirement.md")
        if _is_placeholder(_section(req, "## 澄清问答")):
            problems.append("requirement.md：澄清问答还是空的")
        if not parse_criteria(req):
            problems.append("requirement.md：验收条件没有一条实质条目（- [ ] 有内容）")

    if rank >= 2:                                   # planned 起
        if not _plan_has_rows(read_text(d, "plan.md")):
            problems.append("plan.md：「改哪些文件」表还是空模板")

    if rank >= 4:                                   # verifying 起：缺陷必须修
        op = open_defects(d)
        if op:
            names = ", ".join(f"{k}({v.get('severity')})" for k, v in op.items())
            problems.append(f"defects.jsonl：还有 {len(op)} 个缺陷没修：{names}")

    if rank >= 3:                                   # implementing 起：改动与认领
        changes = load_changes(d)
        if not changes:
            problems.append("changes.jsonl：还没有任何改动记录")

        unl = unlinked_changes(d)
        if unl:
            problems.append(f"changes.jsonl：{len(unl)} 条代码/测试改动没挂验收条件"
                            "（--criterion AC-x），可能越界")

        missing = unclaimed_acs(d)
        if missing:
            problems.append("requirement.md：这些验收条件没有任何改动认领："
                            + ", ".join(c["id"] for c in missing))

        nohash = [c for c in load_changes(d)
                  if c.get("kind") in ("code", "test")
                  and (c.get("criteria") or [])
                  and not (c.get("acHashes") or {})]
        if nohash:
            hints.append(f"{len(nohash)} 条认领没有版本哈希（升级前的记录），"
                         "AC 文本改动后不会自动失效；需要保护就重新 record 一次")

        stale = stale_acs(d)
        if stale:
            problems.append("AC 文本改过了，这些旧认领已失效，需要重新 record："
                            + ", ".join(sorted(stale)))

        dfr = _deferred(d)
        if dfr:
            hints.append("已转交 " + str(len(dfr)) + " 条 AC（不计入本 story 的认领要求）："
                         + ", ".join(sorted(dfr)))

        ac = last_ac_check(d)
        if ac and ac.get("verdict") == "block":
            problems.append("ac-check.jsonl：最近一次兑现检查判定 block（有验收条件未兑现）")

    if rank >= 4:                                   # verifying 起
        ac = last_ac_check(d)
        if not ac:
            problems.append("ac-check.jsonl：进入 verifying 前必须做一次兑现检查（ac-check -> ac-record）")
        elif ac.get("verdict") not in ("pass", "warn"):
            problems.append(f"ac-check.jsonl：最近一次检查是 {ac.get('verdict')}，不是 pass/warn")

        v = _load_json(d / "verify.json")
        if v == "INVALID":
            problems.append("verify.json：不是合法 JSON")
        elif not isinstance(v, dict) or not v.get("checks"):
            problems.append("verify.json：还没有验证结果（用 verify 命令记录）")
        else:
            last_v = (v.get("checks") or [{}])[-1]
            if last_v.get("result") != "pass":
                problems.append(
                    f"verify.json：最近一次验证结果是 {last_v.get('result')!r}，不是 pass"
                    f"（命令：{last_v.get('cmd') or '-'}）——修完要重跑")
            vc = str(v.get("commit") or "").strip()
            hc = str((meta.get("handoff") or {}).get("commit") or "").strip()
            if vc and hc and not (hc.startswith(vc) or vc.startswith(hc)):
                problems.append(f"verify.json：验证的是 {vc}，但当前提交是 {hc}——代码变了，要重跑验证")
            elif not vc:
                hints.append("verify.json 没记验证的 commit，建议 verify --commit <sha>")

    if rank >= 6:                                   # reviewed 起：缺陷必须复测
        un = unverified_defects(d)
        if un:
            problems.append("defects.jsonl：这些缺陷修了但没复测（等于没修）："
                            + ", ".join(un))


        r = _load_json(d / "review.json")
        if r == "INVALID":
            problems.append("review.json：不是合法 JSON")
        elif not isinstance(r, dict):
            problems.append("review.json：还没有评审结论（用 review 命令记录）")
        else:
            if not r.get("independent"):
                problems.append("review.json：评审不是独立的（必须换 session）")
            if r.get("verdict") != "pass":
                problems.append(f"review.json：评审结论是 {r.get('verdict')}，不是 pass")
            rc = (r.get("commit") or "").strip()
            hc = ((meta.get("handoff") or {}).get("commit") or "").strip()
            if rc and hc and not (hc.startswith(rc) or rc.startswith(hc)):
                problems.append(f"review.json：评审的是 {rc}，但当前提交是 {hc}"
                                "——代码变了，要重新评")
            elif not rc:
                hints.append("review.json 没记被评审的 commit，建议 review --commit <sha>")

            r_ts = str(r.get("ts") or "")
            if r_ts:
                late = [c for c in load_changes(d)
                        if c.get("kind") in ("code", "test")
                        and str(c.get("ts") or "") > r_ts]
                if late:
                    problems.append(
                        f"changes.jsonl：评审（{r_ts}）之后还有 {len(late)} 条代码/测试改动"
                        "——评审已过期，要重新评")

    if problems:
        print(f"gate FAILED  ({stage})")
        for p in problems:
            print("  x " + p)
        for h in hints:
            print("  · " + h)
        return 2
    print(f"gate PASSED  ({stage})")
    for h in hints:
        print("  · " + h)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aiwf", description="AI 开发工作流")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="新建一个 story")
    p.add_argument("slug")
    p.add_argument("--title", default="")
    p.add_argument("--repo", action="append")
    p.add_argument("--key", "--jira", dest="key", default="",
                   help="tracker 号：Jira SHIP-1024 / GitHub #123 / 禅道 等")
    p.add_argument("--type", dest="type", default="feature", choices=list(STORY_TYPES),
                   help="feature（需求）/ bug（缺陷单）/ hotfix（线上急修）")
    p.add_argument("--parent", action="append",
                   help="关联的上游 story key（bug 单常用：--parent SHIP-1024）")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("selftest", help="端到端自检（改门禁后必跑）")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("scan", help="扫描明文凭据（默认扫 story 目录，可指定目录）")
    p.add_argument("path", nargs="?", default="", help="要扫描的目录，默认 story 数据目录")
    p.add_argument("--story", default="", help="只扫某个 story 目录")
    p.add_argument("--fix", action="store_true", help="拆成 .example（脱敏）+ .local（原值）")
    p.add_argument("--yes", action="store_true", help="配合 --fix 真正执行（默认只预览）")
    p.set_defaults(func=cmd_scan)

    dp = sub.add_parser("defect", help="缺陷管理：open / fix / verify / wontfix / list")
    dsub = dp.add_subparsers(dest="action", required=True)

    q = dsub.add_parser("open", help="登记缺陷")
    q.add_argument("story")
    q.add_argument("title")
    q.add_argument("--severity", default="major", choices=["blocker", "major", "minor"])
    q.add_argument("--source", default="self", choices=["self", "SIT", "review", "prod"])
    q.add_argument("--ac", action="append", help="关联的验收条件，如 AC-1")
    q.add_argument("--repro", default="", help="复现步骤：期望 vs 实际")
    q.add_argument("--found-in", default="", help="在哪个提交上发现的")
    q.set_defaults(func=cmd_defect)

    q = dsub.add_parser("fix", help="标记已修")
    q.add_argument("story")
    q.add_argument("id")
    q.add_argument("--commit", default="")
    q.add_argument("--note", default="", help="根因")
    q.set_defaults(func=cmd_defect)

    q = dsub.add_parser("verify", help="复测（必须换人）")
    q.add_argument("story")
    q.add_argument("id")
    q.add_argument("--result", required=True, choices=["pass", "fail"])
    q.add_argument("--by", default="", help="谁复测的（pass 时必填）")
    q.add_argument("--note", default="")
    q.set_defaults(func=cmd_defect)

    q = dsub.add_parser("wontfix", help="不修（需原因）")
    q.add_argument("story")
    q.add_argument("id")
    q.add_argument("--reason", default="")
    q.set_defaults(func=cmd_defect)

    q = dsub.add_parser("duplicate", help="重复缺陷")
    q.add_argument("story")
    q.add_argument("id")
    q.add_argument("--reason", default="")
    q.set_defaults(func=cmd_defect)

    q = dsub.add_parser("list", help="缺陷清单")
    q.add_argument("story")
    q.set_defaults(func=cmd_defect)

    p = sub.add_parser("where", help="打印数据目录与统计，排查文件不一致")
    p.set_defaults(func=cmd_where)

    p = sub.add_parser("adopt", help="把仓库内 ./stories 的 story 搬到当前数据目录")
    p.add_argument("--yes", action="store_true", help="确认执行（默认只预览）")
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("status", help="所有进行中的 story")
    p.add_argument("--all", action="store_true", help="包含已完成")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("acs", help="列出验收条件与认领情况")
    p.add_argument("story")
    p.set_defaults(func=cmd_acs)

    p = sub.add_parser("context", help="打包上下文（喂给任何 agent）")
    p.add_argument("story")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("record", help="记录一次改动（挂上它兑现的验收条件）")
    p.add_argument("story")
    p.add_argument("--stage", default="")
    p.add_argument("--repo", default="")
    p.add_argument("--file", action="append")
    p.add_argument("--criterion", action="append", help="验收条件编号，可重复，如 AC-1")
    p.add_argument("--why", default="", help="为什么这处改动能兑现它")
    p.add_argument("--kind", default="code", choices=["code", "test", "doc", "config", "note"])
    p.add_argument("--note", default="")
    p.add_argument("--blocked", default="", help="标记被卡住（写原因），不进阶段机")
    p.add_argument("--unblocked", action="store_true", help="解除阻塞")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("defer", help="把部分 AC 转交给别处（前端 / 另一组件），不再要求本 story 认领")
    p.add_argument("story")
    p.add_argument("ac", nargs="+")
    p.add_argument("--owner", default="", help="谁负责，如 frontend / repo 名")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_defer)

    p = sub.add_parser("undefer", help="把转交出去的 AC 拿回来")
    p.add_argument("story")
    p.add_argument("ac", nargs="+")
    p.set_defaults(func=cmd_undefer)

    p = sub.add_parser("handoff", help="更新交接指针（分支/PR/提交/待办）")
    p.add_argument("story")
    p.add_argument("--branch", default="")
    p.add_argument("--pr", default="")
    p.add_argument("--commit", default="")
    p.add_argument("--todo", default="")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("link", help="给已记录的改动补挂验收条件")
    p.add_argument("story")
    p.add_argument("--target", required=True, help="改动的 ts（从 aiwf.py acs / record 输出里取）")
    p.add_argument("--criterion", action="append", required=True)
    p.add_argument("--why", default="")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("ac-check", help="生成验收条件兑现检查提示词（交给新 session）")
    p.add_argument("story")
    p.set_defaults(func=cmd_ac_check)

    p = sub.add_parser("ac-record", help="记录兑现检查结论")
    p.add_argument("story")
    p.add_argument("--verdict", required=True, choices=["pass", "warn", "block"])
    p.add_argument("--fulfilled", action="append",
                   help="已兑现的验收条件编号，可重复；不给则按 verdict 推断")
    p.add_argument("--issue", action="append", help="未声明改动 / 问题")
    p.add_argument("--reviewer", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_ac_record)

    p = sub.add_parser("reopen", help="需求追加/变更：退回某阶段，旧验证与评审自动失效")
    p.add_argument("story")
    p.add_argument("--stage", default="clarifying", choices=STAGES)
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("verify", help="记录一次验证结果")
    p.add_argument("story")
    p.add_argument("--cmd", required=True, help="实际执行的命令，如 mvn -q test")
    p.add_argument("--result", required=True, choices=["pass", "fail"])
    p.add_argument("--detail", default="")
    p.add_argument("--stage", default="")
    p.add_argument("--commit", default="", help="验证的是哪个提交（建议填）")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("review", help="记录独立评审结论")
    p.add_argument("story")
    p.add_argument("--independent", action="store_true",
                   help="确认评审在独立 session 中进行（必填）")
    p.add_argument("--reviewer", default="")
    p.add_argument("--verdict", required=True, choices=["pass", "fail"])
    p.add_argument("--issue", action="append")
    p.add_argument("--commit", default="", help="被评审的提交（HEAD sha），建议必填")
    p.add_argument("--range", dest="range", default="", help="评审的 diff 范围，如 200c8d49..3ef42576")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("gate", help="检查当前阶段门禁")
    p.add_argument("story")
    p.set_defaults(func=cmd_gate)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

