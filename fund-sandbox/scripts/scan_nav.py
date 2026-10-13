import json, urllib.request, os, sys

os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'

BASE = "http://127.0.0.1:8765"

def fetch(path):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))

funds = fetch("/api/funds")["items"]
codes = [f["code"] for f in funds]

results = []
for code in codes:
    try:
        d = fetch(f"/api/fund/{code}/nav")
    except Exception as e:
        results.append({"code": code, "err": str(e)})
        continue
    series = d.get("series") or []
    if not series:
        results.append({"code": code, "err": "no series"})
        continue
    acc = [s["acc_nav"] for s in series if s.get("acc_nav") is not None]
    latest = acc[-1]
    total = len(acc)
    below = sum(1 for x in acc if x < latest)
    pct = below / total * 100 if total else None
    stats = d.get("stats") or {}
    results.append({
        "code": code,
        "name": d.get("name"),
        "category": next((f["category"] for f in funds if f["code"] == code), None),
        "acc_latest": latest,
        "pct": pct,
        "ret_1y": stats.get("ret_1y"),
        "ret_3m": stats.get("ret_3m"),
        "ret_1m": stats.get("ret_1m"),
        "ret_6m": stats.get("ret_6m"),
        "max_dd": stats.get("max_drawdown"),
        "annual_vol": stats.get("annual_vol"),
        "latest_date": stats.get("latest_date"),
    })

# sort by pct
results_sorted = sorted([r for r in results if r.get("pct") is not None], key=lambda r: r["pct"])
valid = [r for r in results_sorted]
import statistics
pvals = [r["pct"] for r in valid]
median_pct = statistics.median(pvals) if pvals else None

print("=== POOL NAV SCAN ===")
print(f"total funds: {len(codes)}, valid: {len(valid)}, median pct: {median_pct:.1f}" if median_pct is not None else "no valid")
print(f"{'code':<8}{'name':<26}{'pct%':>7}{'ret1y%':>8}{'ret3m%':>8}{'maxdd%':>9}{'vol%':>7}{'cat'}")
for r in valid:
    print(f"{r['code']:<8}{r['name'][:24]:<26}{r['pct']:>7.1f}{r['ret_1y']:>8.2f}{r['ret_3m']:>8.2f}{r['max_dd']:>9.2f}{r['annual_vol']:>7.2f}  {r['category']}")

# dump json for downstream use
with open("/tmp/nav_scan.json", "w", encoding="utf-8") as f:
    json.dump({"results": valid, "median_pct": median_pct}, f, ensure_ascii=False, indent=2)
print("\n[written /tmp/nav_scan.json]")
