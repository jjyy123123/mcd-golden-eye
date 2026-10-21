#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦麦限定黄金眼 · MCD Golden Eye —— 售罄寻货 / 周边情报表

用法：
  python golden_eye.py find  --city 苏州市 --near 永旺梦乐城 --item 泷泷星愿
  python golden_eye.py radar --city 苏州市 --near 永旺梦乐城
  python golden_eye.py watch --add --city 苏州市 --near 永旺梦乐城 --item 泷泷星愿
  python golden_eye.py watch --check
  python golden_eye.py watch --list

核心原理：麦当劳 MCP 的 query-meals 返回"当前可售菜单"，售罄商品会从列表中消失。
         逐店比对即可定位"哪家还有货"。
"""
import argparse
import difflib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_client import McdMCP, extract_json, polite_sleep  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 周边类商品识别关键词（标签 + 商品名）
MERCH_TAG_KEYWORDS = ["周边", "限定周边", "联名", "限量", "玩具", "徽章", "手办"]
MERCH_NAME_KEYWORDS = ["帽", "徽章", "手办", "玩具", "毛绒", "贴纸", "挂件", "音乐盒",
                       "帆布包", "按摩", "痒痒挠", "钥匙扣", "徽章", "公仔", "周边"]
# 明显是餐食的关键词（用于 radar 过滤误命中）
FOOD_KEYWORDS = ["堡", "鸡", "薯条", "薯饼", "咖啡", "饮", "雪冰", "麦旋风", "派",
                 "冰淇淋", "圆筒", "卷", "翅", "块", "杯装", "奶", "可乐", "橙"]


# ---------------------------------------------------------------- 数据获取

def fetch_stores(mcp, city, near, limit=5):
    payload = mcp.call_json("query-nearby-stores", {
        "searchType": 2, "city": city, "keyword": near, "beType": 1,
    })
    stores = (payload or {}).get("data") or []
    if not isinstance(stores, list):
        stores = []
    return stores[:limit]


def fetch_menu(mcp, store_code):
    """返回 {code: {name, price, original, tags, with_order}}"""
    payload = mcp.call_json("query-meals", {
        "storeCode": store_code, "orderType": 1, "beType": 1,
    })
    data = (payload or {}).get("data") or {}
    meals = data.get("meals") or {}
    out = {}
    for code, info in meals.items():
        if not isinstance(info, dict):
            continue
        out[code] = {
            "name": info.get("name", ""),
            "price": info.get("currentPrice"),
            "original": info.get("originalPrice"),
            "tags": list(info.get("tags") or []),
            "with_order": bool(info.get("withOrder")),
        }
    for cat in data.get("categories") or []:
        for m in cat.get("meals") or []:
            code = m.get("code")
            if code in out and m.get("tags"):
                merged = out[code]["tags"] + [t for t in m["tags"] if t not in out[code]["tags"]]
                out[code]["tags"] = merged
    return out


def scan_menus(mcp, city, near, limit=5, quiet=False):
    stores = fetch_stores(mcp, city, near, limit)
    result = []
    for i, st in enumerate(stores, 1):
        code = st.get("storeCode")
        if not quiet:
            print("  [%d/%d] 扫描 %s ..." % (i, len(stores), st.get("storeName", code)),
                  file=sys.stderr)
        try:
            menu = fetch_menu(mcp, code)
        except Exception as e:
            if not quiet:
                print("        ! 读取失败：%s" % e, file=sys.stderr)
            menu = {}
        result.append({"store": st, "menu": menu})
        polite_sleep()
    return result


# ---------------------------------------------------------------- 匹配

def match_item(all_names, query, threshold=0.5):
    """在商品名集合里模糊匹配目标商品，返回 (code, name, score) 或 None"""
    if not all_names:
        return None
    if query in all_names:
        return (all_names[query], query, 1.0)
    # 商品名 → code 反向
    name2code = {}
    for code, name in all_names.items():
        name2code.setdefault(name, code)
    best = None
    for code, name in all_names.items():
        # 子串优先
        if query and query in name:
            score = 0.95
        else:
            score = difflib.SequenceMatcher(None, query, name).ratio()
        if best is None or score > best[2]:
            best = (code, name, score)
    if best and best[2] >= threshold:
        return best
    return None


def collect_names(scan):
    names = {}
    for item in scan:
        for code, info in item["menu"].items():
            names.setdefault(code, info["name"])
    return names


# ---------------------------------------------------------------- 命令：find

def cmd_find(mcp, city, near, item_query, limit, as_json, html_path):
    print("🔍 麦麦限定黄金眼 · 售罄寻货\n", file=sys.stderr)
    scan = scan_menus(mcp, city, near, limit)
    if not scan:
        print("未找到任何门店，请检查 --city / --near。")
        return 1

    names = collect_names(scan)
    hit = match_item(names, item_query)
    if not hit:
        print("❌ 在附近 %d 家门店的菜单里都没有找到与「%s」匹配的商品。" % (len(scan), item_query))
        print("   可能原因：本期已结束 / 该商品不在这些门店上架 / 关键词需要调整。")
        return 1
    code, matched_name, score = hit

    available, missing = [], []
    for item in scan:
        st, menu = item["store"], item["menu"]
        row = {
            "storeCode": st.get("storeCode"),
            "storeName": st.get("storeName"),
            "address": st.get("address"),
            "distance": st.get("distance"),
            "hours": "%s-%s" % (st.get("businessStartTime", "?"), st.get("businessEndTime", "?")),
        }
        if code in menu:
            row.update(menu[code])
            available.append(row)
        else:
            missing.append(row)

    # 置信度
    if available:
        confidence = "中高（同批次有门店在售，缺失门店大概率是售罄）"
    else:
        confidence = "中（所有扫描门店均无此商品，可能是全城售罄或本期已结束，建议到官方App复核）"

    available.sort(key=lambda r: r.get("distance") or 0)

    if as_json:
        print(json.dumps({
            "item": {"code": code, "name": matched_name, "match_score": round(score, 3)},
            "available": available, "missing": missing,
            "confidence": confidence,
        }, ensure_ascii=False, indent=2))
        return 0

    print("\n目标：%s（%s）  匹配度 %.0f%%" % (matched_name, code, score * 100))
    print("范围：%s · %s 附近 %d 家门店\n" % (city, near, len(scan)))

    print("✅ 有货门店（%d 家）" % len(available))
    if available:
        for i, r in enumerate(available, 1):
            extra = " · 需随单购" if r.get("with_order") else ""
            print("  %d. %-28s %s · %s · ¥%s%s"
                  % (i, r["storeName"], _km(r["distance"]), r["hours"], r["price"], extra))
            print("     %s" % r["address"])
    else:
        print("  （无）")

    print("\n❌ 疑似售罄/未上架（%d 家）" % len(missing))
    for r in missing:
        print("  · %-28s %s · 菜单中无此商品" % (r["storeName"], _km(r["distance"])))

    print("\n判定置信度：%s" % confidence)
    print("\n下一步：选一家有货门店，例如「就选 %s，帮我下单」——将调用 calculate-price 与 create-order。"
          % (available[0]["storeName"] if available else "有货门店"))

    if html_path:
        write_html_report(html_path, matched_name, code, city, near, available, missing, confidence)
        print("\n📄 HTML 报告已生成：%s" % html_path)
    return 0


def _km(distance):
    """距离格式化：米 / 公里，带单位。"""
    try:
        d = float(distance)
    except Exception:
        return "-"
    return "%.1fkm" % (d / 1000.0) if d >= 1000 else "%.0fm" % d


# ---------------------------------------------------------------- 命令：radar

def is_merch(name, tags):
    joined = " ".join(tags or [])
    if any(k in joined for k in MERCH_TAG_KEYWORDS):
        return True
    if any(k in name for k in MERCH_NAME_KEYWORDS) and not any(k in name for k in FOOD_KEYWORDS):
        return True
    return False


def cmd_radar(mcp, city, near, limit, as_json):
    print("🎯 麦麦限定黄金眼 · 周边情报表\n", file=sys.stderr)
    scan = scan_menus(mcp, city, near, limit)
    if not scan:
        print("未找到任何门店。")
        return 1

    matrix = {}   # code -> {name, price, stores:[storeName], tags}
    store_total = len(scan)
    for item in scan:
        for code, info in item["menu"].items():
            if not is_merch(info["name"], info["tags"]):
                continue
            row = matrix.setdefault(code, {
                "name": info["name"], "price": info["price"], "tags": info["tags"],
                "with_order": info["with_order"], "stores": [],
            })
            row["stores"].append(item["store"].get("storeName"))

    rows = sorted(matrix.values(), key=lambda r: (-len(r["stores"]), r["name"]))
    if as_json:
        print(json.dumps({"city": city, "near": near, "store_count": store_total,
                          "items": rows}, ensure_ascii=False, indent=2))
        return 0

    print("范围：%s · %s 附近 %d 家门店\n" % (city, near, store_total))
    if not rows:
        print("本次扫描未发现周边类商品。")
        return 0
    print("%-30s %-8s %-8s %s" % ("商品", "价格", "覆盖", "在售门店"))
    print("-" * 100)
    for r in rows:
        diff_flag = "⭐差异款" if len(r["stores"]) < store_total else ""
        print("%-30s %-8s %-8s %s %s" % (
            _trunc(r["name"], 28), "¥%s" % r["price"],
            "%d/%d" % (len(r["stores"]), store_total),
            "、".join(r["stores"]), diff_flag))
    print("\n⭐ 差异款 = 并非所有门店都在售，想买得挑对门店。")
    return 0


def _trunc(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


# ---------------------------------------------------------------- HTML 报告

def write_html_report(path, item_name, item_code, city, near, available, missing, confidence):
    def card(r, ok):
        color = "#0a7d32" if ok else "#9aa0a6"
        badge = "有货" if ok else "疑似售罄"
        extra = "<span class='tag'>需随单购</span>" if r.get("with_order") else ""
        price = "¥%s" % r.get("price") if ok else "—"
        return ("""<div class="store" style="border-left-color:%s">
  <div class="row1"><b>%s</b><span class="badge" style="background:%s">%s</span>%s</div>
  <div class="row2">%s · %s · 营业 %s · %s</div>
</div>""" % (color, r["storeName"], color, badge, extra,
             r["address"], _km(r["distance"]), r["hours"], price))

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>麦麦限定黄金眼 · %s</title>
<style>
body{font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#faf7f2;color:#1f1f1f;margin:0;padding:28px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#6b6b6b;font-size:13px;margin-bottom:18px}
.store{background:#fff;border-radius:10px;padding:12px 14px;margin:8px 0;border-left:5px solid #ccc;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.row1{display:flex;align-items:center;gap:8px;font-size:15px}
.badge{color:#fff;font-size:11px;border-radius:10px;padding:2px 8px}
.tag{background:#fff3cd;color:#8a6d3b;font-size:11px;border-radius:8px;padding:2px 6px}
.row2{color:#6b6b6b;font-size:12px;margin-top:4px}
.note{background:#fff;border-radius:10px;padding:12px 14px;margin-top:16px;font-size:12px;color:#6b6b6b;line-height:1.7}
</style></head><body>
<h1>🔍 麦麦限定黄金眼 · 售罄寻货</h1>
<div class="sub">目标：%s（%s） · 范围：%s · %s · %s</div>
%s
%s
<div class="note"><b>置信度：</b>%s<br>
本结果基于麦当劳官方 MCP 的门店可售菜单比对得出，售罄商品会从菜单中消失；餐品供应状态请以麦当劳官方渠道为准。</div>
</body></html>""" % (
        item_name, item_name, item_code, city, near,
        _now_str(),
        "\n".join(card(r, True) for r in available) or "<div class='store'>暂无有货门店</div>",
        "\n".join(card(r, False) for r in missing),
        confidence)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def _now_str():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- CLI

def main(argv=None):
    p = argparse.ArgumentParser(description="麦麦限定黄金眼 · 售罄寻货 + 返场蹲守")
    sub = p.add_subparsers(dest="cmd")

    f = sub.add_parser("find", help="售罄寻货：找附近还有货的门店")
    f.add_argument("--city", required=True)
    f.add_argument("--near", required=True, help="位置关键词，如 永旺梦乐城")
    f.add_argument("--item", required=True, help="商品名，支持模糊，如 泷泷星愿")
    f.add_argument("--limit", type=int, default=5, help="扫描门店数，默认 5")
    f.add_argument("--json", action="store_true")
    f.add_argument("--html", dest="html_path", help="输出 HTML 报告路径")

    r = sub.add_parser("radar", help="周边情报表：汇总附近门店限定周边分布")
    r.add_argument("--city", required=True)
    r.add_argument("--near", required=True)
    r.add_argument("--limit", type=int, default=5)
    r.add_argument("--json", action="store_true")

    w = sub.add_parser("watch", help="返场蹲守")
    w.add_argument("--add", action="store_true")
    w.add_argument("--check", action="store_true")
    w.add_argument("--list", action="store_true")
    w.add_argument("--remove", metavar="ID")
    w.add_argument("--city")
    w.add_argument("--near")
    w.add_argument("--item")
    w.add_argument("--limit", type=int, default=5)

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 1

    mcp = McdMCP(verbose=bool(os.environ.get("GOLDEN_EYE_DEBUG")))

    if args.cmd == "find":
        return cmd_find(mcp, args.city, args.near, args.item, args.limit,
                        args.json, args.html_path)
    if args.cmd == "radar":
        return cmd_radar(mcp, args.city, args.near, args.limit, args.json)
    if args.cmd == "watch":
        from watch_sentinel import watch_main
        return watch_main(mcp)
    return 1


if __name__ == "__main__":
    sys.exit(main())
