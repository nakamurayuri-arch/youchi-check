# -*- coding: utf-8 -*-
"""
用地チェック（緯度経度だけ）— 1ファイル完結・全項目
必要ライブラリ: streamlit, requests, Pillow
"""
import re, math, time, os
from io import BytesIO
import requests
import streamlit as st

UA = {"User-Agent": "youchi-check/1.0"}
T = 25

st.set_page_config(page_title="用地チェック（緯度経度）", layout="centered")
st.title("用地チェック（緯度経度だけ）")
st.caption("緯度経度を入れるだけで用地の各項目を自動判定します。会社情報は使いません。")

def get_key():
    try:
        if "REINFOLIB_KEY" in st.secrets:
            return st.secrets["REINFOLIB_KEY"]
    except Exception:
        pass
    return os.environ.get("REINFOLIB_KEY")
REINFOLIB_KEY = get_key()

def parse_coord(s):
    s = (s or "").strip()
    m = re.search(r"(-?\d+\.\d+)\s*[, ]\s*(-?\d+\.\d+)", s)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r'(\d+)[°](\d+)[\'′](\d+\.?\d*)"?\s*N[, ]*\s*(\d+)[°](\d+)[\'′](\d+\.?\d*)"?\s*E', s, re.I)
    if m:
        lat = int(m.group(1)) + int(m.group(2))/60 + float(m.group(3))/3600
        lon = int(m.group(4)) + int(m.group(5))/60 + float(m.group(6))/3600
        return round(lat, 7), round(lon, 7)
    return None

def haversine(a, b, c, d):
    R = 6371000.0; r = math.radians
    h = math.sin(r(c-a)/2)**2 + math.cos(r(a))*math.cos(r(c))*math.sin(r(d-b)/2)**2
    return 2*R*math.asin(math.sqrt(h))

def deg2tile(lat, lon, z):
    n = 2**z
    return (lon+180)/360*n, (1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*n

def _pip_ring(x, y, ring):
    inside = False; n = len(ring); j = n-1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]; xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj-xi)*(y-yi)/(yj-yi) + xi):
            inside = not inside
        j = i
    return inside

def point_in_geom(x, y, geom):
    t = geom.get("type"); c = geom.get("coordinates")
    if t == "Polygon" and c:
        if not _pip_ring(x, y, c[0]):
            return False
        return not any(_pip_ring(x, y, h) for h in c[1:])
    if t == "MultiPolygon" and c:
        for poly in c:
            if poly and _pip_ring(x, y, poly[0]) and not any(_pip_ring(x, y, h) for h in poly[1:]):
                return True
    return False

def elevation(lat, lon):
    r = requests.get("https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php",
                     params={"lon": lon, "lat": lat, "outtype": "JSON"}, headers=UA, timeout=T)
    return float(r.json().get("elevation"))

def slope(lat, lon, step=20.0):
    dlat = step/111000; dlon = step/(111000*math.cos(math.radians(lat)))
    z0 = elevation(lat, lon); mx = 0.0
    for la, lo in [(lat+dlat, lon), (lat-dlat, lon), (lat, lon+dlon), (lat, lon-dlon)]:
        try:
            mx = max(mx, math.degrees(math.atan2(abs(elevation(la, lo)-z0), step)))
        except Exception:
            pass
    return round(z0, 1), round(mx, 1)

HAZ = {
    "洪水浸水(想定最大規模)": "https://disaportaldata.gsi.go.jp/raster/01_flood_l2_shinsuishin_data/{z}/{x}/{y}.png",
    "津波浸水想定": "https://disaportaldata.gsi.go.jp/raster/04_tsunami_newlegend_data/{z}/{x}/{y}.png",
    "高潮浸水想定": "https://disaportaldata.gsi.go.jp/raster/03_hightide_l2_shinsuishin_data/{z}/{x}/{y}.png",
    "土砂(土石流)": "https://disaportaldata.gsi.go.jp/raster/05_dosekiryukeikaikuiki_data/{z}/{x}/{y}.png",
    "土砂(急傾斜)": "https://disaportaldata.gsi.go.jp/raster/05_kyukeishakeikaikuiki_data/{z}/{x}/{y}.png",
    "土砂(地すべり)": "https://disaportaldata.gsi.go.jp/raster/05_jisuberikeikaikuiki_data/{z}/{x}/{y}.png",
}
DEPTH_LEGEND = [
    ((247, 245, 169), "0〜0.5m未満"),
    ((255, 216, 192), "0.5〜3m未満"),
    ((255, 183, 183), "3〜5m未満"),
    ((255, 145, 145), "5〜10m未満"),
    ((242, 133, 201), "10〜20m未満"),
    ((220, 122, 220), "20m以上"),
]
DOSHA_LEGEND = [
    ((255, 237, 74), "警戒区域（イエロー）"),
    ((203, 15, 75), "特別警戒区域（レッド）"),
    ((250, 122, 122), "特別警戒区域（レッド）"),
]
DEPTH_LAYERS = {"洪水浸水(想定最大規模)", "津波浸水想定", "高潮浸水想定"}

def classify_color(rgb, palette):
    best, bd = None, 1e9
    for col, label in palette:
        d = sum((a-b)**2 for a, b in zip(rgb, col))
        if d < bd:
            bd, best = d, label
    return best

def hazard(lat, lon, z=16):
    """タイルで該当/非該当のみ判定（色→深さ推定は廃止。深さは洪水のみXKT026で正確取得）。"""
    from PIL import Image
    xf, yf = deg2tile(lat, lon, z); xt, yt = int(xf), int(yf)
    out = {}
    for name, tmpl in HAZ.items():
        try:
            r = requests.get(tmpl.format(z=z, x=xt, y=yt), headers=UA, timeout=T)
            if r.status_code != 200:
                out[name] = False; continue
            img = Image.open(BytesIO(r.content)).convert("RGBA")
            px = min(255, int((xf-xt)*256)); py = min(255, int((yf-yt)*256))
            d = img.getpixel((px, py))
            out[name] = bool(d[3] > 0 and (d[0]+d[1]+d[2] > 0))
        except Exception:
            out[name] = None
        time.sleep(0.1)
    return out

# 洪水浸水深ランク（国土数値情報A31 / reinfolib XKT026 の A31a_205）
FLOOD_RANK = {1:"0〜0.5m未満",2:"0.5〜1m未満",3:"1〜2m未満",4:"2〜3m未満",
              5:"3〜4m未満",6:"4〜5m未満",7:"5〜10m未満",8:"10〜20m未満",9:"20m以上"}

def reinfolib_flood(lat, lon, z=15):
    """XKT026(洪水/想定最大規模)。該当ポリゴン全ての(河川,ランク)を集め最大を採用。診断付き。"""
    if not REINFOLIB_KEY:
        return None
    xf, yf = deg2tile(lat, lon, z); xt, yt = int(xf), int(yf)
    url = f"https://www.reinfolib.mlit.go.jp/ex-api/external/XKT026?response_format=geojson&z={z}&x={xt}&y={yt}"
    try:
        r = requests.get(url, headers={**UA, "Ocp-Apim-Subscription-Key": REINFOLIB_KEY}, timeout=T)
        if r.status_code in (204, 404):
            return {"hit": False, "matches": []}
        if r.status_code != 200:
            return {"err": f"HTTP {r.status_code}"}
        gj = r.json()
    except Exception as e:
        return {"err": str(e)}
    matches = []
    total = len(gj.get("features", []) if isinstance(gj, dict) else [])
    for f in (gj.get("features", []) if isinstance(gj, dict) else []):
        if point_in_geom(lon, lat, f.get("geometry", {})):
            p = f.get("properties", {})
            try:
                rk = int(p.get("A31a_205") or 0)
            except Exception:
                rk = 0
            matches.append({"river": p.get("A31a_202"), "rank": rk,
                            "depth": FLOOD_RANK.get(rk, "?"), "props_keys": list(p.keys())})
    if not matches:
        return {"hit": False, "matches": [], "tile_features": total}
    mr = max(m["rank"] for m in matches)
    top = max(matches, key=lambda m: m["rank"])
    return {"hit": True, "rank": mr, "depth": FLOOD_RANK.get(mr, "不明"),
            "river": top.get("river"), "matches": matches, "tile_features": total}

def reinfolib_inundation(endpoint, lat, lon, z=14):
    """XKT027(高潮)/XKT028(津波)：浸水深は文字列区分。属性から深さ文字列を拾い最深を返す。"""
    if not REINFOLIB_KEY:
        return None
    xf, yf = deg2tile(lat, lon, z); xt, yt = int(xf), int(yf)
    url = f"https://www.reinfolib.mlit.go.jp/ex-api/external/{endpoint}?response_format=geojson&z={z}&x={xt}&y={yt}"
    try:
        r = requests.get(url, headers={**UA, "Ocp-Apim-Subscription-Key": REINFOLIB_KEY}, timeout=T)
        if r.status_code in (204, 404):
            return {"hit": False}
        if r.status_code != 200:
            return {"err": f"HTTP {r.status_code}"}
        gj = r.json()
    except Exception as e:
        return {"err": str(e)}
    hit_any = False; depths = []
    for f in (gj.get("features", []) if isinstance(gj, dict) else []):
        if point_in_geom(lon, lat, f.get("geometry", {})):
            hit_any = True
            for v in (f.get("properties", {}) or {}).values():
                if isinstance(v, str) and re.search(r"\d", v) and "m" in v and ("以上" in v or "未満" in v or "以下" in v):
                    depths.append(v)
    if not hit_any:
        return {"hit": False}
    if depths:
        def _dk(s):
            n = re.findall(r"\d+(?:\.\d+)?", s); return float(n[0]) if n else 0.0
        return {"hit": True, "depth": max(depths, key=_dk)}
    return {"hit": True, "depth": None}

REINFOLIB = [
    ("市街化区域/調整区域", "XKT001", ["kubun_id", "kubun", "区域区分"]),
    ("用途地域", "XKT002", ["use_area_ja"]),
    ("自然公園地域", "XKT019", ["ptext", "park_name", "公園名", "thema"]),
    ("地すべり防止区域", "XKT021", ["name", "区域名"]),
    ("急傾斜地崩壊危険区域", "XKT022", ["name", "区域名"]),
]

def reinfolib_hit(lat, lon, endpoint, z=15):
    xf, yf = deg2tile(lat, lon, z); xt, yt = int(xf), int(yf)
    url = f"https://www.reinfolib.mlit.go.jp/ex-api/external/{endpoint}?response_format=geojson&z={z}&x={xt}&y={yt}"
    r = requests.get(url, headers={**UA, "Ocp-Apim-Subscription-Key": REINFOLIB_KEY}, timeout=T)
    if r.status_code != 200:
        return {"err": f"HTTP {r.status_code}"}
    gj = r.json()
    feats = gj.get("features", []) if isinstance(gj, dict) else []
    matched = [f.get("properties", {}) for f in feats if point_in_geom(lon, lat, f.get("geometry", {}))]
    if matched:
        return {"hit": True, "props": matched[0], "all_props": matched}
    return {"hit": False}

def _kubun_from_feats(all_props):
    """XKT001の該当フィーチャ群から 市街化区域/市街化調整区域/非線引き を判定。"""
    vals = [str(p.get("area_classification_ja") or "") for p in (all_props or [])]
    if any("市街化調整区域" in v for v in vals):
        return "市街化調整区域"
    if any("市街化区域" in v for v in vals):
        return "市街化区域"
    if any("都市計画区域" in v for v in vals):
        return "非線引き都市計画区域（区域区分なし）"
    return None

def label_from(props, keys):
    for k in keys:
        if k in props and props[k] not in (None, ""):
            return str(props[k])
    for v in props.values():
        if isinstance(v, str) and v.strip():
            return v
    return "該当"


# ---------- 農地：青地/白地（国土数値情報 A12 を都道府県別に取得→点内包） ----------
PREF_CODE = {
 "北海道":"01","青森県":"02","岩手県":"03","宮城県":"04","秋田県":"05","山形県":"06","福島県":"07",
 "茨城県":"08","栃木県":"09","群馬県":"10","埼玉県":"11","千葉県":"12","東京都":"13","神奈川県":"14",
 "新潟県":"15","富山県":"16","石川県":"17","福井県":"18","山梨県":"19","長野県":"20","岐阜県":"21",
 "静岡県":"22","愛知県":"23","三重県":"24","滋賀県":"25","京都府":"26","大阪府":"27","兵庫県":"28",
 "奈良県":"29","和歌山県":"30","鳥取県":"31","島根県":"32","岡山県":"33","広島県":"34","山口県":"35",
 "徳島県":"36","香川県":"37","愛媛県":"38","高知県":"39","福岡県":"40","佐賀県":"41","長崎県":"42",
 "熊本県":"43","大分県":"44","宮崎県":"45","鹿児島県":"46","沖縄県":"47",
}

def pref_code_from_addr(addr):
    if not addr:
        return None
    for name, code in PREF_CODE.items():
        if name in addr:
            return code, name
    return None

@st.cache_data(show_spinner=False)
def download_a12(code):
    import io, zipfile, tempfile, glob
    url = f"https://nlftp.mlit.go.jp/ksj/gml/data/A12/A12-15/A12-15_{code}_GML.zip"
    r = requests.get(url, headers=UA, timeout=120)
    r.raise_for_status()
    d = tempfile.mkdtemp()
    zipfile.ZipFile(io.BytesIO(r.content)).extractall(d)
    shps = glob.glob(d + "/**/*.shp", recursive=True)
    if not shps:
        raise RuntimeError("A12のSHPが見つかりません")
    return shps

# 近隣県（県境の取り違え対策）: 主要な隣接関係のみ簡易に
NEIGHBORS = {
 "25":["26","21","24","18","23"], "26":["25","27","28","29","24","18"],
 "27":["26","28","29","30"], "28":["27","26","31","33","36","24"],
 "13":["11","12","14","19"], "14":["13","11","19","22"],
}

def _pref_candidates(lat, lon, addr):
    cands = []
    pc = pref_code_from_addr(addr)
    if pc:
        cands.append(pc)
        for nb in NEIGHBORS.get(pc[0], []):
            nm = [k for k, v in PREF_CODE.items() if v == nb]
            if nm:
                cands.append((nb, nm[0]))
    return cands

def _rings_from_shape(shape):
    pts = shape.points
    parts = list(shape.parts) + [len(pts)]
    return [pts[parts[i]:parts[i+1]] for i in range(len(parts)-1)]

def _point_in_rings(lon, lat, rings):
    # even-odd rule：内包するリング数が奇数なら内部（外周/穴を自動処理）
    cnt = 0
    for ring in rings:
        if len(ring) >= 3 and _pip_ring(lon, lat, ring):
            cnt += 1
    return (cnt % 2) == 1

def _hit_in_shps(shps, lat, lon):
    import shapefile
    for shp in shps:
        try:
            r = shapefile.Reader(shp, encoding="cp932")
        except Exception:
            r = shapefile.Reader(shp)
        fields = [f[0] for f in r.fields[1:]]
        for sr in r.iterShapeRecords():
            bb = sr.shape.bbox
            if not (bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]):
                continue
            if _point_in_rings(lon, lat, _rings_from_shape(sr.shape)):
                return {"attrs": dict(zip(fields, list(sr.record))), "file": shp.split("/")[-1]}
    return None

def _diag_shps(shps, lat, lon):
    """SHPを読み、件数・全体bbox・点がbbox内か等の診断を返す。"""
    import shapefile
    total = 0; inbbox = 0
    gxmin = gymin = 1e18; gxmax = gymax = -1e18
    sample_fields = []
    for shp in shps:
        try:
            r = shapefile.Reader(shp, encoding="cp932")
        except Exception:
            r = shapefile.Reader(shp)
        sample_fields = [f[0] for f in r.fields[1:]]
        b = r.bbox  # [xmin,ymin,xmax,ymax]
        gxmin = min(gxmin, b[0]); gymin = min(gymin, b[1])
        gxmax = max(gxmax, b[2]); gymax = max(gymax, b[3])
        for sh in r.iterShapes():
            total += 1
            bb = sh.bbox
            if bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]:
                inbbox += 1
    return {"files": [s.split("/")[-1] for s in shps], "polygons": total,
            "data_bbox": [round(gxmin,4), round(gymin,4), round(gxmax,4), round(gymax,4)],
            "point": [round(lon,6), round(lat,6)],
            "point_in_data_bbox": (gxmin <= lon <= gxmax and gymin <= lat <= gymax),
            "bbox_hit_polygons": inbbox, "fields": sample_fields}

def _a12_layers_hit(shps, lat, lon):
    """点を含むポリゴンの LAYER_NO 集合（6=農用地区域/5=農業地域）と代表属性を返す。"""
    import shapefile
    layers = set(); sample = None; sfile = None
    for shp in shps:
        try:
            r = shapefile.Reader(shp, encoding="cp932")
        except Exception:
            r = shapefile.Reader(shp)
        fields = [f[0] for f in r.fields[1:]]
        li = fields.index("LAYER_NO") if "LAYER_NO" in fields else None
        for sr in r.iterShapeRecords():
            bb = sr.shape.bbox
            if not (bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]):
                continue
            if _point_in_rings(lon, lat, _rings_from_shape(sr.shape)):
                ln = None
                if li is not None:
                    try: ln = int(sr.record[li])
                    except Exception: ln = sr.record[li]
                layers.add(ln)
                if sample is None:
                    sample = dict(zip(fields, list(sr.record))); sfile = shp.split("/")[-1]
    return layers, sample, sfile

def a12_status(lat, lon, addr):
    cands = _pref_candidates(lat, lon, addr)
    if not cands:
        return {"err": "都道府県を特定できず"}
    tried = []; diag = None
    for code, pname in cands:
        try:
            shps = download_a12(code)
        except Exception as e:
            tried.append(f"{pname}(コード{code}):取得失敗 {e}"); continue
        if diag is None:
            try:
                diag = {"pref": pname, "code": code, **_diag_shps(shps, lat, lon)}
            except Exception:
                pass
        layers, sample, sfile = _a12_layers_hit(shps, lat, lon)
        if 6 in layers:
            return {"status": "青地", "pref": pname, "attrs": sample, "file": sfile, "diag": diag}
        if layers:  # 5（農業地域）にのみ該当
            return {"status": "白地", "pref": pname, "attrs": sample, "file": sfile, "diag": diag}
    return {"status": "非農地", "pref": cands[0][1], "note": "／".join(tried), "diag": diag}

OVP = "https://overpass-api.de/api/interpreter"
ROADCLS = {"trunk": "国道相当", "primary": "国道/主要地方道", "secondary": "県道相当",
           "tertiary": "主要市町村道", "unclassified": "一般道", "residential": "生活道路",
           "service": "私道/構内", "track": "農道/林道"}

def ovp(q):
    return requests.get(OVP, params={"data": q}, headers=UA, timeout=50).json()

def nearest_road(lat, lon):
    js = ovp(f"[out:json][timeout:40];way(around:250,{lat},{lon})[highway];out tags geom;")
    best = None
    for e in js.get("elements", []):
        dmin = min((haversine(lat, lon, p["lat"], p["lon"]) for p in e.get("geometry", [])), default=1e12)
        c = {"d": round(dmin), "cls": ROADCLS.get(e["tags"].get("highway"), e["tags"].get("highway")),
             "name": e["tags"].get("name") or e["tags"].get("ref") or ""}
        if best is None or c["d"] < best["d"]:
            best = c
    return best

def _osm_building(lat, lon):
    js = ovp(f"[out:json][timeout:40];way(around:400,{lat},{lon})[building];out geom;")
    dmin = 1e12
    for e in js.get("elements", []):
        for p in e.get("geometry", []):
            dmin = min(dmin, haversine(lat, lon, p["lat"], p["lon"]))
    return round(dmin) if dmin < 1e12 else None

def _extent_to_latlon(px, py, X, Y, Z, extent=4096, y_down=False):
    fx = px/extent; fy = py/extent
    gx = X + fx
    gy = (Y + (1-fy)) if not y_down else (Y + fy)
    n = 2**Z
    lon = gx/n*360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi*(1 - 2*gy/n))))
    return lat, lon

def _iter_rings(geom):
    t = geom.get("type"); c = geom.get("coordinates")
    if t == "Polygon":
        for ring in c: yield ring
    elif t == "MultiPolygon":
        for poly in c:
            for ring in poly: yield ring
    elif t == "LineString":
        yield c
    elif t == "Point":
        yield [c]

def _gsi_building(lat, lon, z=16):
    """国土地理院ベクトルタイル(建物)から最寄り建物距離。OSMの穴（工業地/埋立地/農村）を補う。"""
    try:
        import mapbox_vector_tile as mvt
    except Exception:
        return None
    xf, yf = deg2tile(lat, lon, z); X, Y = int(xf), int(yf)
    best = 1e12
    for dx in (0, -1, 1):
        for dy in (0, -1, 1):
            tx, ty = X+dx, Y+dy
            try:
                r = requests.get(f"https://cyberjapandata.gsi.go.jp/xyz/experimental_bvmap/{z}/{tx}/{ty}.pbf",
                                 headers=UA, timeout=T)
                if r.status_code != 200 or not r.content:
                    continue
                data = mvt.decode(r.content)
            except Exception:
                continue
            layer = data.get("building") or data.get("建物")
            if not layer:
                continue
            extent = layer.get("extent", 4096)
            for f in layer.get("features", []):
                for ring in _iter_rings(f.get("geometry", {})):
                    for pt in ring:
                        blat, blon = _extent_to_latlon(pt[0], pt[1], tx, ty, z, extent)
                        d = haversine(lat, lon, blat, blon)
                        if d < best:
                            best = d
    return round(best) if best < 1e12 else None

def nearest_building(lat, lon):
    vals = []
    try:
        v = _osm_building(lat, lon)
        if v is not None: vals.append(v)
    except Exception:
        pass
    try:
        v = _gsi_building(lat, lon)
        if v is not None: vals.append(v)
    except Exception:
        pass
    return min(vals) if vals else None

def coast_dist(lat, lon):
    """海（海岸線・海の面）までの最短距離。out geom(bbox)で地点周辺だけ切り抜き、
    開けた海岸でも応答を軽くしてタイムアウトを防ぐ。池・川は塩害無関係で除外。"""
    dlat, dlon = 0.12, 0.15
    s, w, n, e = lat-dlat, lon-dlon, lat+dlat, lon+dlon
    q = (f"[out:json][timeout:50];("
         f"way(around:12000,{lat},{lon})[natural=coastline];"
         f"way(around:8000,{lat},{lon})[natural=water][water~\"sea|bay|strait|lagoon|harbour\"];"
         f"way(around:8000,{lat},{lon})[natural=bay];"
         f"way(around:8000,{lat},{lon})[natural=strait];"
         f"way(around:5000,{lat},{lon})[landuse=harbour];"
         f"way(around:5000,{lat},{lon})[harbour=yes];"
         f");out geom({s},{w},{n},{e});")
    try:
        js = ovp(q)
    except Exception:
        return None
    dmin = 1e12
    for el in js.get("elements", []):
        for p in el.get("geometry", []) or []:
            if p and "lat" in p:
                dmin = min(dmin, haversine(lat, lon, p["lat"], p["lon"]))
    return round(dmin) if dmin < 1e12 else None

def revgeo(lat, lon):
    try:
        return requests.get("https://nominatim.openstreetmap.org/reverse",
                            params={"format": "json", "accept-language": "ja", "lat": lat, "lon": lon},
                            headers=UA, timeout=T).json().get("display_name")
    except Exception:
        return None

if not REINFOLIB_KEY:
    st.warning("市街化調整・用途・自然公園・地すべり・急傾斜を自動判定するには、無料のreinfolib APIキーが必要です（申請 → Streamlitの Secrets に REINFOLIB_KEY を設定）。未設定の間はリンク表示になります。", icon="🔑")

addr_in = st.text_input("住所（記録用。緯度経度が空のときはここから判定）",
                        placeholder="例: 滋賀県野洲市堤字ノ爪740-1")
coord = st.text_input("緯度経度（あればこちらを優先。正確）",
                      placeholder="例: 33.0598671, 131.9332333")
if st.button("▶ チェックする", type="primary"):
    lat = lon = None
    c = parse_coord(coord)
    if c:
        lat, lon = c
        if addr_in.strip():
            st.caption(f"判定は緯度経度を使用（住所『{addr_in.strip()}』は記録用）")
    elif addr_in.strip():
        g = geocode(addr_in.strip())
        if not g:
            st.error("住所から場所を特定できませんでした。番地まで入れるか、緯度経度を入力してください。")
            st.stop()
        lat, lon, _ = g
        st.caption(f"住所を緯度経度に変換： {lat:.6f}, {lon:.6f}")
    else:
        st.error("住所か緯度経度のどちらかを入力してください。")
        st.stop()
    with st.spinner("判定中…（10〜40秒）"):
        addr = revgeo(lat, lon)
        try: elev, slp = slope(lat, lon)
        except Exception: elev = slp = None
        haz = hazard(lat, lon)
        rein = {}
        if REINFOLIB_KEY:
            for name, ep, keys in REINFOLIB:
                try:
                    rein[name] = reinfolib_hit(lat, lon, ep)
                except Exception as e:
                    rein[name] = {"err": str(e)}
                time.sleep(0.3)
        try: road = nearest_road(lat, lon)
        except Exception: road = None
        try: bldg = nearest_building(lat, lon)
        except Exception: bldg = None
        try: coast = coast_dist(lat, lon)
        except Exception: coast = None

    st.subheader("基本")
    st.write({"緯度経度": f"{lat:.6f}, {lon:.6f}", "住所(推定)": addr or "取得できず",
              "標高": f"{elev} m" if elev is not None else "取得できず",
              "傾斜(推定)": f"{slp}°" if slp is not None else "取得できず"})

    st.subheader("ハザード")
    fl  = reinfolib_flood(lat, lon) if REINFOLIB_KEY else None
    tsu = reinfolib_inundation("XKT028", lat, lon, z=14) if REINFOLIB_KEY else None
    hig = reinfolib_inundation("XKT027", lat, lon, z=14) if REINFOLIB_KEY else None

    def _line_flood(k):
        if isinstance(fl, dict) and not fl.get("err"):
            if fl.get("hit"):
                rv = f"（{fl.get('river','')}）" if fl.get("river") else ""
                return f"- {k}： **⚠ 該当（{fl.get('depth','')}／最大ランク{fl.get('rank')}）**{rv} 〈XKT026〉"
            return f"- {k}： **○ 非該当** 〈XKT026〉"
        return None

    def _line_str(k, res, ep):
        if isinstance(res, dict) and not res.get("err"):
            if res.get("hit"):
                d = f"（{res['depth']}）" if res.get("depth") else "（深さ区分の記載なし）"
                return f"- {k}： **⚠ 該当{d}** 〈{ep}〉"
            return f"- {k}： **○ 非該当** 〈{ep}〉"
        return None

    for k, v in haz.items():
        line = None
        if k.startswith("洪水"):
            line = _line_flood(k)
        elif k.startswith("津波"):
            line = _line_str(k, tsu, "XKT028")
        elif k.startswith("高潮"):
            line = _line_str(k, hig, "XKT027")
        if line:
            st.write(line); continue
        # フォールバック（キー無し等）：タイルの該当/非該当のみ
        if v is True:
            extra = "（深さは公式マップで確認）" if k.startswith(("洪水", "津波", "高潮")) else ""
            st.write(f"- {k}： **⚠ 該当**{extra}")
        elif v is False:
            st.write(f"- {k}： **○ 非該当**")
        else:
            st.write(f"- {k}： 要確認")
    if isinstance(fl, dict) and fl.get("hit"):
        with st.expander("洪水の内訳（XKT026が返した該当ポリゴン）"):
            st.json({"tile_features": fl.get("tile_features"), "matches": fl.get("matches")})
    st.caption(f"洪水深の権威確認：[重ねるハザードマップ（想定最大規模）で開く](https://disaportal.gsi.go.jp/maps/?ll={lat},{lon}&z=17&vs=c1j0l0u0t0&d=l)")
    for nm, res in [("洪水", fl), ("津波", tsu), ("高潮", hig)]:
        if isinstance(res, dict) and res.get("err"):
            st.caption(f"（{nm}の属性取得でエラー: {res['err']}。タイル判定にフォールバック）")

    st.subheader("許認可・区域")
    if REINFOLIB_KEY:
        for name, ep, keys in REINFOLIB:
            r = rein.get(name, {})
            if name == "市街化区域/調整区域":
                if r.get("err"):
                    st.write(f"- 市街化調整区域： 取得エラー（{r['err']}）")
                elif r.get("hit"):
                    lab = _kubun_from_feats(r.get("all_props", []))
                    if lab == "市街化調整区域":
                        st.write("- 市街化調整区域： **⚠ 該当（市街化調整区域）**")
                    elif lab == "市街化区域":
                        st.write("- 市街化調整区域： **○ 非該当（市街化区域）**")
                    elif lab:
                        st.write(f"- 市街化調整区域： **○ 非該当**（{lab}）")
                    else:
                        st.write("- 市街化調整区域： 都市計画区域内だが区分不明 → 下の属性を確認")
                    with st.expander("XKT001の該当属性（全ポリゴン）"):
                        st.json(r.get("all_props", []))
                else:
                    st.write("- 市街化調整区域： **○ 非該当**（都市計画区域外の可能性）")
                continue
            if r.get("err"):
                st.write(f"- {name}： 取得エラー（{r['err']}）")
            elif r.get("hit"):
                st.write(f"- {name}： **⚠ 該当**（{label_from(r.get('props', {}), keys)}）")
            else:
                st.write(f"- {name}： **○ 非該当**")
    else:
        st.markdown(f"- 市街化調整・用途・自然公園・地すべり・急傾斜： APIキー設定後に自動判定。今は[国交省/環境省マップ](https://disaportal.gsi.go.jp/maps/?ll={lat},{lon}&z=16)で確認")

    st.subheader("周辺")
    bt = "周辺に建物なし" if bldg is None else (f"{bldg} m ⚠100m未満" if bldg < 100 else f"{bldg} m")
    ct = "海岸を検出できず(内陸の可能性)" if coast is None else (f"{coast} m ⚠500m未満(重塩害注意)" if coast < 500 else f"{coast} m")
    st.write(f"- 最寄り道路： **{(road['cls']+' '+road['name']+' '+str(road['d'])+'m') if road else '取得できず'}**")
    st.write(f"- 最寄り建物(住宅目安)： **{bt}**")
    st.write(f"- 海岸まで(重塩害)： **{ct}**")

    st.subheader("農地（青地/白地：国土数値情報A12・2015年度）")
    try:
        a12 = a12_status(lat, lon, addr)
    except Exception as e:
        a12 = {"err": str(e)}
    if a12.get("err"):
        st.write(f"- 農地区分： 取得エラー（{a12['err']}）")
    else:
        stt = a12.get("status")
        if stt == "青地":
            st.write(f"- 農地区分： **⚠ 青地（農用地区域内）**（判定県：{a12.get('pref','')}）")
            st.caption("→ 農地転用には原則、農振除外（農振法13条2）が先行して必要。")
        elif stt == "白地":
            st.write(f"- 農地区分： **△ 白地（農業地域内・農用地区域外）**（判定県：{a12.get('pref','')}）")
            st.caption("→ 農地転用の対象（第何種は農業委員会確認）。")
        else:
            st.write(f"- 農地区分： **○ 非農地／農業地域外**（判定県：{a12.get('pref','')}）")
        if a12.get("attrs"):
            with st.expander("A12の属性（生データ）"):
                st.json({"file": a12.get("file"), "attrs": a12.get("attrs")})
        if a12.get("note"):
            st.warning(f"データ取得の注意: {a12['note']}")
    with st.expander("A12 判定の診断"):
        st.json(a12.get("diag") or {"注意": "診断情報なし"})
    st.caption("※A12は2015年度データ。農用地区域（青地）は参考表示で精度保証なし。第何種は農業委員会、地目は登記簿で確認。")

    st.subheader("その他（データ無し→リンク確認）")
    st.markdown(
        f"- 農地の地目・地番・第何種： [農地ナビ](https://map.maff.go.jp/)\n"
        f"- 砂防指定地： [国土数値情報](https://nlftp.mlit.go.jp/ksj/)／都道府県の砂防GIS\n"
        f"- 埋蔵文化財包蔵地： [文化財総覧WebGIS](https://heritagemap.nabunken.go.jp/)（全国統一の可否データ無し・自治体教委）\n"
        f"- 位置： [Googleマップ](https://www.google.com/maps?q={lat},{lon})"
    )
    st.info("公開データからの一次確認です。最終確定は各自治体・現地・公式マップで。")
    if REINFOLIB_KEY:
        st.caption("このサービスは、国土交通省不動産情報ライブラリのAPI機能を使用していますが、提供情報の最新性、正確性、完全性等が保証されたものではありません。")
