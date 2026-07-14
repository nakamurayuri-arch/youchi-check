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
    from PIL import Image
    xf, yf = deg2tile(lat, lon, z); xt, yt = int(xf), int(yf)
    out = {}
    for name, tmpl in HAZ.items():
        try:
            r = requests.get(tmpl.format(z=z, x=xt, y=yt), headers=UA, timeout=T)
            if r.status_code != 200:
                out[name] = {"hit": False}; continue
            img = Image.open(BytesIO(r.content)).convert("RGBA")
            px = min(255, int((xf-xt)*256)); py = min(255, int((yf-yt)*256))
            d = img.getpixel((px, py))
            if d[3] > 0 and (d[0]+d[1]+d[2] > 0):
                pal = DEPTH_LEGEND if name in DEPTH_LAYERS else DOSHA_LEGEND
                out[name] = {"hit": True, "level": classify_color(d[:3], pal)}
            else:
                out[name] = {"hit": False}
        except Exception:
            out[name] = {"hit": None}
        time.sleep(0.1)
    return out

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
    for f in feats:
        if point_in_geom(lon, lat, f.get("geometry", {})):
            return {"hit": True, "props": f.get("properties", {})}
    return {"hit": False}

def label_from(props, keys):
    for k in keys:
        if k in props and props[k] not in (None, ""):
            return str(props[k])
    for v in props.values():
        if isinstance(v, str) and v.strip():
            return v
    return "該当"

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

def nearest_building(lat, lon):
    js = ovp(f"[out:json][timeout:40];way(around:400,{lat},{lon})[building];out geom;")
    dmin = 1e12
    for e in js.get("elements", []):
        for p in e.get("geometry", []):
            dmin = min(dmin, haversine(lat, lon, p["lat"], p["lon"]))
    return round(dmin) if dmin < 1e12 else None

def coast_dist(lat, lon):
    js = ovp(f"[out:json][timeout:50];way(around:8000,{lat},{lon})[natural=coastline];out geom;")
    dmin = 1e12
    for e in js.get("elements", []):
        for p in e.get("geometry", []):
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

coord = st.text_input("緯度, 経度", placeholder="例: 33.0598671, 131.9332333")
if st.button("▶ チェックする", type="primary"):
    c = parse_coord(coord)
    if not c:
        st.error("緯度経度を『33.0598671, 131.9332333』の形で入力してください（度分秒も可）。")
        st.stop()
    lat, lon = c
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

    st.subheader("ハザード（該当時は浸水深/区分も表示・凡例推定）")
    for k, v in haz.items():
        if v.get("hit") is True:
            st.write(f"- {k}： **⚠ 該当（{v.get('level','')}）**")
        elif v.get("hit") is False:
            st.write(f"- {k}： **○ 非該当**")
        else:
            st.write(f"- {k}： 要確認")

    st.subheader("許認可・区域")
    if REINFOLIB_KEY:
        for name, ep, keys in REINFOLIB:
            r = rein.get(name, {})
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
    ct = "8km内に海岸なし(内陸)" if coast is None else (f"{coast} m ⚠500m未満(重塩害注意)" if coast < 500 else f"{coast} m")
    st.write(f"- 最寄り道路： **{(road['cls']+' '+road['name']+' '+str(road['d'])+'m') if road else '取得できず'}**")
    st.write(f"- 最寄り建物(住宅目安)： **{bt}**")
    st.write(f"- 海岸まで(重塩害)： **{ct}**")

    st.subheader("農地・その他（データ無し→リンク確認）")
    st.markdown(
        f"- 農地（地目・青地/白地・第何種）： [農地ナビ](https://map.maff.go.jp/)（reinfolibに農地レイヤ無し）\n"
        f"- 砂防指定地： [国土数値情報](https://nlftp.mlit.go.jp/ksj/)／都道府県の砂防GIS\n"
        f"- 埋蔵文化財包蔵地： [文化財総覧WebGIS](https://heritagemap.nabunken.go.jp/)（全国統一の可否データ無し・自治体教委）\n"
        f"- 位置： [Googleマップ](https://www.google.com/maps?q={lat},{lon})"
    )
    st.info("公開データからの一次確認です。ハザードの深さ/区分は凡例色からの推定。最終確定は各自治体・現地・公式マップで。")
