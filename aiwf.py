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
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

_env_data = os.environ.get("AIWF_DATA", "").strip()
STORY_DIR = Path(_env_data).expanduser() if _env_data else ROOT / "stories"

STAGES = ["new", "clarifying", "planned", "implementing",
          "verifying", "reviewing", "reviewed", "done"]

# 只有这两个阶段的改动需要挂验收条件
WORK_STAGES = ("implementing", "verifying")

# 自上次对账后新增多少条改动就提醒一次
AUTO_AC_HINT_EVERY = 3

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
    "handoff": {"branch": "", "pr": "", "commit": "", "todo": ""},
}

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
    meta.update(id=d.name, key=key, slug=slug, title=args.title or slug,
                created=now(), jira=key,
                repos=[{"name": r, "path": "", "status": "pending"} for r in (args.repo or [])])
    save_meta(d, meta)
    (d / "requirement.md").write_text(REQ_TMPL.format(**meta), encoding="utf-8")
    (d / "decisions.md").write_text(DEC_TMPL.format(title=meta["title"]), encoding="utf-8")
    (d / "plan.md").write_text(PLAN_TMPL.format(title=meta["title"]), encoding="utf-8")
    (d / "changes.jsonl").write_text("", encoding="utf-8")

    print(f"created  {d}")
    print(f"id       {d.name}")
    print(f"key      {key or '（无，用当日序号）'}")
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
        rows.append((created, show_key, stage, last + mark,
                     f"{missing}" if missing else "-",
                     len(load_changes(d)), meta.get("title", ""), meta.get("slug", "")))
    if not rows:
        print("没有进行中的 story（加 --all 看已完成）。")
        return 0
    print(f"{'DATE':<12}{'KEY':<13}{'STAGE':<13}{'AC':>4}{'缺':>4}{'CHG':>4}  TITLE")
    print("-" * 92)
    for created, key, stage, last, miss, n, title, slug in rows:
        label = title or slug
        print(f"{created:<12}{key:<13}{stage:<13}{last:>4}{miss:>4}{n:>4}  {label}")
    print("\nAC: 最近一次兑现检查 pass / warn(!) / block(x) / -（还没查）")
    print("缺: 还没有改动认领的验收条件数量")
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

    out += ["## 你现在要做的事", "", NEXT_ACTION.get(meta.get("stage", "new"), "")]
    print("\n".join(out))
    return 0


def cmd_record(args):
    d = _need_story(args.story)
    if not d:
        return 1
    meta = load_meta(d) or {}
    stage = args.stage or meta.get("stage")

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
    target = [c for c in load_changes(d) if c.get("ts") == args.target]
    if not target:
        print(f"ERROR：找不到 ts={args.target} 的改动记录", file=sys.stderr)
        print("  可用 ts：", file=sys.stderr)
        for c in load_changes(d):
            if c.get("kind") != "link":
                print(f"    {c.get('ts')}  {', '.join(c.get('files') or []) or '-'}", file=sys.stderr)
        return 5

    entry = {"ts": now(), "kind": "link", "target": args.target,
             "criteria": args.criterion or [], "stage": target[0].get("stage"),
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
                      "evidence": c["text"]} for c in parse_criteria(read_text(d, "requirement.md"))],
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

    if rank >= 1:                                   # clarifying 起
        req = read_text(d, "requirement.md")
        if _is_placeholder(_section(req, "## 澄清问答")):
            problems.append("requirement.md：澄清问答还是空的")
        if not parse_criteria(req):
            problems.append("requirement.md：验收条件没有一条实质条目（- [ ] 有内容）")

    if rank >= 2:                                   # planned 起
        if not _plan_has_rows(read_text(d, "plan.md")):
            problems.append("plan.md：「改哪些文件」表还是空模板")

    if rank >= 3:                                   # implementing 起：对账
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
        elif v.get("commit") and (meta.get("handoff") or {}).get("commit"):
            vc = str(v.get("commit")).strip()
            hc = str((meta.get("handoff") or {}).get("commit")).strip()
            if not (hc.startswith(vc) or vc.startswith(hc)):
                problems.append(f"verify.json：验证的是 {vc}，但当前提交是 {hc}——代码变了，要重跑验证")

    if rank >= 6:                                   # reviewed 起
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
    p.set_defaults(func=cmd_init)

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

