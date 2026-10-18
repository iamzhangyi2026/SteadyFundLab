import json, urllib.request, urllib.parse, os

os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'
BASE = "http://127.0.0.1:8765"

def get(path):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))

def post(path, data):
    body = json.dumps(data).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=body, headers={"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode('utf-8')[:300], "code": e.code}

# existing pool codes
funds = get("/api/funds")["items"]
pool_codes = {f["code"] for f in funds}

tracks = {
    "宽基": ["沪深300","中证500","创业板","中证1000"],
    "黄金": ["黄金ETF","黄金联接"],
    "QDII": ["纳斯达克","标普500"],
    "AI算力": ["半导体","芯片"],
    "军工": ["军工","国防"],
    "电网": ["电力","电网","新能源"],
    "医药": ["医药","医疗","生物"],
    "红利": ["红利","高股息","纯债"],
}

added = []
for track, kws in tracks.items():
    seen = set()
    for kw in kws:
        try:
            d = get("/api/funds/search?" + urllib.parse.urlencode({"q": kw}))
        except Exception as e:
            print(f"  search err {kw}: {e}")
            continue
        items = d.get("items") or []
        for it in items[:3]:
            code = it.get("code")
            if not code or code in seen or code in pool_codes:
                continue
            seen.add(code)
            # add to watch pool
            res = post("/api/funds", {"fund_code": code, "name": it.get("name")})
            added.append({"track": track, "kw": kw, "code": code, "name": it.get("name"), "res": res.get("ok") if isinstance(res,dict) else res})

print("=== DISCOVERY ===")
print(f"existing pool: {len(pool_codes)}")
for a in added:
    print(f"  + {a['code']} {a['name']} [{a['track']}/{a['kw']}] -> {a['res']}")
if not added:
    print("  (no new candidates added this round)")
