# -*- coding: utf-8 -*-
"""
用地チェック（緯度経度だけ）— 1ファイル完結版
必要ライブラリ: streamlit, requests, Pillow  （geopandas不要）
起動: streamlit run app.py
"""
import re, math, time
from io import BytesIO
import requests
import streamlit as st

UA = {"User-Agent": "youchi-check/1.0"}
T = 20

st.set_page_config(page_title="用地チェック（緯度経度）", layout="centered")
st.title("用地チェック（緯度経度だけ）")
st.caption("緯度経度を入れるだけ。ハザード・傾斜・道路・民家距離・海岸距離を自動判定し、市街化調整・自然公園・農地・埋蔵文化財は公式マップのリンクで確認します。会社情報は使いません。")


# ---------- 座標 ----------
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


# ---------- 標高・傾斜 ----------
def elevation(lat, lon):
    r = requests.get("https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php",
                     params={"lon": lon, "lat": lat, "outtype": "JSON"}, headers=UA, timeout=T)
    return float(r.json().get("elevation"))

def slope(lat, lon, step=20.0):
    dlat = step/111000; dlon = step/(111000*math.cos(math.radians(lat)))
    z0 = elevation(lat, lon)
    mx = 0.0
    for la, lo in [(lat+dlat, lon), (lat-dlat, lon), (lat, lon+dlon), (lat, lon-dlon)]:
        try:
            mx = max(mx, math.degrees(math.atan2(abs(elevation(la, lo)-z0), step)))
        except Exception:
            pass
    return round(z0, 1), round(mx, 1)


# ---------- ハザード（タイル画素判定） ----------
HAZ = {
    "洪水浸水(想定最大規模)": "https://disaportaldata.gsi.go.jp/raster/01_flood_l2_shinsuishin_data/{z}/{x}/{y}.png",
    "津波浸水想定": "https://disaportaldata.gsi.go.jp/raster/04_tsunami_newlegend_data/{z}/{x}/{y}.png",
    "高潮浸水想定": "https://disaportaldata.gsi.go.jp/raster/03_hightide_l2_shinsuishin_data/{z}/{x}/{y}.png",
    "土砂(土石流)": "https://disaportaldata.gsi.go.jp/raster/05_dosekiryukeikaikuiki_data/{z}/{x}/{y}.png",
    "土砂(急傾斜)": "https://disaportaldata.gsi.go.jp/raster/05_kyukeishakeikaikuiki_data/{z}/{x}/{y}.png",
    "土砂(地すべり)": "https://disaportaldata.gsi.go.jp/raster/05_jisuberikeikaikuiki_data/{z}/{x}/{y}.png",
}

def hazard(lat, lon, z=16):
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


# ---------- 道路・建物・海岸（OSM/Overpass） ----------
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


# ---------- UI ----------
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
    for k, v in haz.items():
        mark = "⚠ 該当" if v is True else ("○ 非該当" if v is False else "要確認")
        st.write(f"- {k}： **{mark}**")

    st.subheader("周辺")
    bt = "周辺に建物なし" if bldg is None else (f"{bldg} m ⚠100m未満" if bldg < 100 else f"{bldg} m")
    ct = "8km内に海岸なし(内陸)" if coast is None else (f"{coast} m ⚠500m未満(重塩害注意)" if coast < 500 else f"{coast} m")
    st.write(f"- 最寄り道路： **{(road['cls']+' '+road['name']+' '+str(road['d'])+'m') if road else '取得できず'}**")
    st.write(f"- 最寄り建物(住宅目安)： **{bt}**")
    st.write(f"- 海岸まで(重塩害)： **{ct}**")

    st.subheader("許認可・区域・農地（公式マップで確認）")
    st.markdown(
        f"- 市街化調整/用途・自然公園・砂防： [国土交通省・環境省の地図で確認]"
        f"(https://disaportal.gsi.go.jp/maps/?ll={lat},{lon}&z=16)\n"
        f"- 農地（地目・青地/白地）： [農地ナビ](https://map.maff.go.jp/)\n"
        f"- 埋蔵文化財： 全国統一データなし → 自治体教委に確認\n"
        f"- 地図で位置確認： [Googleマップ](https://www.google.com/maps?q={lat},{lon})"
    )
    st.info("これは公開データからの一次確認です。市街化調整・農地・埋蔵文化財などの確定は、リンク先や各自治体・現地でご確認ください。")

# 共有リンク用: ?lat=..&lon=.. で自動チェック
try:
    qp = st.query_params
    if "lat" in qp and "lon" in qp and not coord:
        st.session_state.setdefault("_auto", True)
except Exception:
    pass
