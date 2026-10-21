#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch_sentinel.py — 返场蹲守哨兵

把一时买不到的限定商品加入蹲守清单，之后每次运行复查门店在售状态，
只在**状态发生变化**（尤其是"售罄 → 有货"的返场瞬间）时提醒。

也可单独运行（等价于 golden_eye.py watch）：
  python watch_sentinel.py --check
  python watch_sentinel.py --list
  python watch_sentinel.py --add --city 苏州市 --near 永旺梦乐城 --item 泷泷星愿
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import McdMCP  # noqa: E402
import golden_eye as ge  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHLIST = os.path.join(ROOT, "watchlist.json")


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_list():
    if not os.path.exists(WATCHLIST):
        return []
    try:
        with open(WATCHLIST, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_list(items):
    with open(WATCHLIST, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _scan_state(mcp, entry):
    """扫描并把每个门店的在售状态记成 {storeCode: 'available'|'absent'}"""
    scan = ge.scan_menus(mcp, entry["city"], entry["near"], entry.get("limit", 5), quiet=True)
    names = ge.collect_names(scan)
    code = entry.get("item_code")
    if not code:
        hit = ge.match_item(names, entry["item_query"])
        if hit:
            code, matched_name, _ = hit
            entry["item_code"] = code
            entry["item_name"] = matched_name
    state, stores = {}, {}
    for item in scan:
        st = item["store"]
        sc = st.get("storeCode")
        ok = bool(code) and (code in item["menu"])
        stores[sc] = "available" if ok else "absent"
        state[st.get("storeName", sc)] = ok
    return code, stores, state


def cmd_add(mcp, args):
    if not (args.city and args.near and args.item):
        print("用法：watch --add --city 苏州市 --near 永旺梦乐城 --item 泷泷星愿")
        return 1
    entry = {
        "id": "w_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
        "item_query": args.item, "city": args.city, "near": args.near,
        "limit": args.limit, "created_at": _now(), "last_check": _now(),
        "item_code": None, "item_name": None, "stores": {}, "state": "unknown", "history": [],
    }
    code, stores, by_name = _scan_state(mcp, entry)
    if not code:
        print("⚠️ 附近门店菜单里都没找到「%s」，仍会加入蹲守清单，之后每次复查都会再尝试匹配。" % args.item)
    available = [n for n, ok in by_name.items() if ok]
    entry["stores"] = stores
    entry["state"] = "available" if available else "sold_out"
    entry["history"].append({"at": _now(), "state": entry["state"], "available": available})

    items = load_list()
    items.append(entry)
    save_list(items)

    print("✅ 已加入蹲守清单：%s（%s）" % (entry["item_name"] or entry["item_query"], entry["id"]))
    print("   当前状态：%s" % ("有货 → " + "、".join(available) if available else "附近门店暂无货"))
    print("   之后运行 `watch --check` 复查；也可把 check 挂到定时任务里，只会在变化时提醒你。")
    return 0


def cmd_check(mcp):
    items = load_list()
    if not items:
        print("蹲守清单为空。先用 `watch --add --city ... --near ... --item ...` 添加。")
        return 0

    print("⏰ 返场哨兵复查（%d 项）\n" % len(items))
    changed, errors = [], 0
    for entry in items:
        try:
            code, stores, by_name = _scan_state(mcp, entry)
        except Exception as e:
            errors += 1
            print("  ! %s 复查失败：%s" % (entry.get("item_name") or entry["item_query"], e))
            continue

        old_state = entry.get("state", "unknown")
        old_stores = entry.get("stores", {}) or {}
        available = [n for n, ok in by_name.items() if ok]
        new_state = "available" if available else "sold_out"

        # 门店级 diff：谁从"没有"变成"有"
        revived = []
        for sc, st in stores.items():
            if st == "available" and old_stores.get(sc) == "absent":
                revived.append(sc)

        name = entry.get("item_name") or entry["item_query"]
        if new_state == "available" and old_state == "sold_out":
            print("  🎉 返场了！%s" % name)
            for r in revived:
                print("     · 新到货门店：%s" % r)
            for n in available:
                print("     · 在售门店：%s" % n)
            changed.append((entry, "revived", available))
        elif new_state != old_state:
            print("  ⚠️ 状态变化：%s → %s（%s）" % (old_state, new_state, name))
            changed.append((entry, new_state, available))
        else:
            print("  · %s：无变化（%s）" % (name, "有货" if available else "暂无货"))

        entry["state"] = new_state
        entry["stores"] = stores
        entry["last_check"] = _now()
        entry["history"].append({"at": _now(), "state": new_state, "available": available})
        entry["history"] = entry["history"][-30:]   # 只留最近 30 条

    save_list(items)
    print("\n复查完成：%d 项，%d 项有变化%s"
          % (len(items), len(changed), "，%d 项失败" % errors if errors else ""))
    if changed:
        print("建议：对有货商品尽快下单，限定品返场后往往很快再次售罄。")
    return 0


def cmd_list():
    items = load_list()
    if not items:
        print("蹲守清单为空。")
        return 0
    print("%-22s %-24s %-28s %-10s %s" % ("ID", "商品", "位置", "状态", "最近复查"))
    print("-" * 108)
    for e in items:
        print("%-22s %-24s %-28s %-10s %s" % (
            e["id"], ge._trunc(e.get("item_name") or e["item_query"], 22),
            "%s · %s" % (e["city"], e["near"]),
            "有货" if e.get("state") == "available" else "暂无货",
            e.get("last_check", "-")))
    return 0


def cmd_remove(rid):
    items = load_list()
    left = [e for e in items if e["id"] != rid]
    if len(left) == len(items):
        print("未找到 ID：%s" % rid)
        return 1
    save_list(left)
    print("已移除 %s" % rid)
    return 0


def watch_main(mcp=None, argv=None):
    p = argparse.ArgumentParser(prog="watch", description="返场蹲守哨兵")
    p.add_argument("--add", action="store_true")
    p.add_argument("--check", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--remove", metavar="ID")
    p.add_argument("--city")
    p.add_argument("--near")
    p.add_argument("--item")
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args(argv if argv is not None else sys.argv[2:])

    if args.list:
        return cmd_list()
    if args.remove:
        return cmd_remove(args.remove)
    if not mcp:
        mcp = McdMCP(verbose=bool(os.environ.get("GOLDEN_EYE_DEBUG")))
    if args.add:
        return cmd_add(mcp, args)
    return cmd_check(mcp)


if __name__ == "__main__":
    sys.exit(watch_main())
