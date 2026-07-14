# -*- coding: utf-8 -*-
"""
系統用蓄電所 用地・立地 自動チェックエンジン
=================================================
緯度経度を入れると、サイトで調べられる項目をすべてAPI/公的データで自動判定する。

このサンドボックス（Claude側）は政府ドメインへ接続できないため実行不可。
あなたのローカル環境（Claude Code / 通常のPython）で実行してください。

使うデータソース（すべて無料・キー不要。※ハザードはGSIタイル、区域はKSJ SHP）:
  - 標高/傾斜   : 国土地理院 標高API   https://maps.gsi.go.jp/development/elevation_s.html
  - ハザード     : 重ねるハザードマップ 配信タイル(PNG) https://disaportal.gsi.go.jp/hazardmapportal/hazardmap/copyright/opendata.html
  - 道路/建物/海岸: OpenStreetMap Overpass  https://overpass-api.de/
  - 逆ジオコード  : Nominatim  https://nominatim.openstreetmap.org/
  - 区域(市街化調整/用途/自然公園/砂防/農業地域): 国土数値情報SHP を点内包判定
                   https://nlftp.mlit.go.jp/ksj/  （大分県=44。必要レイヤを一度DLしてksj/へ）

任意（APIキーが要る代替）:
  - 不動産情報ライブラリ API（用途地域・ハザード等）  https://www.reinfolib.mlit.go.jp/help/apiManual/
    → 無料キー（発行最大5営業日）。SHPを落とさず用途地域/ハザードをAPIで取りたい場合に。

依存: requests, Pillow, geopandas, shapely, pyproj, PyYAML
"""
from __future__ import annotations
import math, glob, os, time, json
import requests

UA = {"User-Agent": "sustech-site-checker/1.0 (contact: you@example.com)"}
TIMEOUT = 20


# ---------------------------------------------------------------------------
# 0. 座標ユーティリティ
# ---------------------------------------------------------------------------
def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def deg2tile(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


# ---------------------------------------------------------------------------
# 1. 標高・傾斜（国土地理院 標高API）
# ---------------------------------------------------------------------------
def elevation(lat, lon):
    url = "https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php"
    r = requests.get(url, params={"lon": lon, "lat": lat, "outtype": "JSON"},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    return d.get("elevation")  # m, "-----" 等がくることもある

def slope_deg(lat, lon, step_m=20.0):
    """代表点の周囲±step_mの標高から最大傾斜(度)を推定。"""
    dlat = step_m / 111000.0
    dlon = step_m / (111000.0 * math.cos(math.radians(lat)))
    try:
        z0 = float(elevation(lat, lon))
        pts = {
            "N": float(elevation(lat + dlat, lon)),
            "S": float(elevation(lat - dlat, lon)),
            "E": float(elevation(lat, lon + dlon)),
            "W": float(elevation(lat, lon - dlon)),
        }
    except (TypeError, ValueError):
        return None
    max_slope = 0.0
    for v in pts.values():
        max_slope = max(max_slope, math.degrees(math.atan2(abs(v - z0), step_m)))
    return {"elevation_m": z0, "max_slope_deg": round(max_slope, 1)}


# ---------------------------------------------------------------------------
# 2. ハザード（重ねるハザードマップ 配信タイル：ピクセル判定・キー不要）
#    透明でない画素＝区域内。RGBは凡例（浸水深/警戒区域）に対応。
#    ※タイルパスは disaportal のオープンデータ配信仕様に準拠。稼働レイヤは
#      https://disaportal.gsi.go.jp/hazardmapportal/hazardmap/copyright/opendata.html で最新を確認。
# ---------------------------------------------------------------------------
HAZARD_TILES = {
    "洪水浸水想定(想定最大規模)": "https://disaportaldata.gsi.go.jp/raster/01_flood_l2_shinsuishin_data/{z}/{x}/{y}.png",
    "津波浸水想定":               "https://disaportaldata.gsi.go.jp/raster/04_tsunami_newlegend_data/{z}/{x}/{y}.png",
    "高潮浸水想定":               "https://disaportaldata.gsi.go.jp/raster/03_hightide_l2_shinsuishin_data/{z}/{x}/{y}.png",
    "土砂災害警戒区域(土石流)":   "https://disaportaldata.gsi.go.jp/raster/05_dosekiryukeikaikuiki_data/{z}/{x}/{y}.png",
    "土砂災害警戒区域(急傾斜)":   "https://disaportaldata.gsi.go.jp/raster/05_kyukeishakeikaikuiki_data/{z}/{x}/{y}.png",
    "土砂災害警戒区域(地すべり)": "https://disaportaldata.gsi.go.jp/raster/05_jisuberikeikaikuiki_data/{z}/{x}/{y}.png",
}

def _tile_pixel_alpha(lat, lon, url_tmpl, z=16):
    from PIL import Image
    from io import BytesIO
    xf, yf = deg2tile(lat, lon, z)
    xt, yt = int(xf), int(yf)
    url = url_tmpl.format(z=z, x=xt, y=yt)
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    if r.status_code != 200:
        return None  # タイル無し＝区域外か未整備
    img = Image.open(BytesIO(r.content)).convert("RGBA")
    px = int((xf - xt) * 256)
    py = int((yf - yt) * 256)
    px = min(max(px, 0), 255); py = min(max(py, 0), 255)
    return img.getpixel((px, py))  # (R,G,B,A)

def hazards(lat, lon, z=16):
    out = {}
    for name, tmpl in HAZARD_TILES.items():
        try:
            rgba = _tile_pixel_alpha(lat, lon, tmpl, z=z)
            if rgba is None:
                out[name] = {"該当": False, "備考": "タイル無し(区域外/未整備)"}
            else:
                inside = rgba[3] > 0 and (rgba[0] + rgba[1] + rgba[2] > 0)
                out[name] = {"該当": bool(inside), "rgba": rgba}
        except Exception as e:
            out[name] = {"該当": None, "備考": f"取得失敗: {e}"}
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------------------
# 3. 道路・建物・海岸（OpenStreetMap / Overpass）
# ---------------------------------------------------------------------------
OVERPASS = "https://overpass-api.de/api/interpreter"

# 日本の主要 highway タグ → 道路種別の概略
ROAD_CLASS = {
    "trunk": "国道相当(幹線)", "primary": "国道/主要地方道",
    "secondary": "県道相当", "tertiary": "主要な市町村道",
    "unclassified": "一般道", "residential": "生活道路",
    "service": "私道/構内道", "track": "農道/林道",
}

def _overpass(query):
    r = requests.post(OVERPASS, data={"data": query}, headers=UA, timeout=60)
    r.raise_for_status()
    return r.json()

def nearest_road(lat, lon, radius=250):
    q = f"""[out:json][timeout:40];
way(around:{radius},{lat},{lon})[highway];
out tags geom;"""
    js = _overpass(q)
    best = None
    for el in js.get("elements", []):
        hw = el.get("tags", {}).get("highway")
        name = el.get("tags", {}).get("name") or el.get("tags", {}).get("ref") or ""
        width = el.get("tags", {}).get("width")
        dmin = min((haversine_m(lat, lon, p["lat"], p["lon"]) for p in el.get("geometry", [])), default=1e9)
        cand = {"距離m": round(dmin, 1), "highway": hw,
                "種別概略": ROAD_CLASS.get(hw, hw), "名称": name, "幅員tag": width}
        if best is None or cand["距離m"] < best["距離m"]:
            best = cand
    return best

def nearest_building(lat, lon, radius=400):
    """最寄り建物（＝近隣住宅の目安）までの距離。防音壁/騒音の一次判断に。"""
    q = f"""[out:json][timeout:40];
way(around:{radius},{lat},{lon})[building];
out geom;"""
    js = _overpass(q)
    dmin = 1e9
    for el in js.get("elements", []):
        for p in el.get("geometry", []):
            dmin = min(dmin, haversine_m(lat, lon, p["lat"], p["lon"]))
    return round(dmin, 1) if dmin < 1e9 else None

def coast_distance(lat, lon, radius=8000):
    """海岸線(natural=coastline)までの最短距離。重塩害(海岸500m)判定用。"""
    q = f"""[out:json][timeout:50];
way(around:{radius},{lat},{lon})[natural=coastline];
out geom;"""
    js = _overpass(q)
    dmin = 1e9
    for el in js.get("elements", []):
        for p in el.get("geometry", []):
            dmin = min(dmin, haversine_m(lat, lon, p["lat"], p["lon"]))
    return round(dmin, 1) if dmin < 1e9 else None  # None=半径内に海岸なし(=内陸)

def reverse_geocode(lat, lon):
    r = requests.get("https://nominatim.openstreetmap.org/reverse",
                     params={"lat": lat, "lon": lon, "format": "json", "accept-language": "ja"},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("display_name")


# ---------------------------------------------------------------------------
# 4. 区域判定（国土数値情報 SHP を点内包判定）
#    使い方: 大分県(44)の各SHPを nlftp.mlit.go.jp/ksj/ からDLし ksj/<key>/ に置く。
#    フォルダ内の *.shp を全部読み、点を含むポリゴンの属性を返す。
# ---------------------------------------------------------------------------
KSJ_LAYERS = {
    # key(フォルダ名)          : 意味 / 主なKSJデータ名（DL時に選ぶ）
    "shigaika":   "区域区分(市街化区域/調整区域) — 都市地域/用途地域データ(A29)",
    "youto":      "用途地域(A29)",
    "shizen_kouen":"自然公園地域(A10)",
    "sabo":       "砂防指定地",
    "jisuberi":   "地すべり防止区域",
    "kyukeisha":  "急傾斜地崩壊危険区域",
    "nochi":      "農業地域(農振農用地区分)(A12)",
}

def point_in_layer(folder, lat, lon):
    """ksj/<folder>/ 内の全SHPに対し点内包判定し、ヒット属性のリストを返す。"""
    import geopandas as gpd
    from shapely.geometry import Point
    shps = glob.glob(os.path.join(folder, "**", "*.shp"), recursive=True)
    if not shps:
        return {"状態": "SHP未配置", "hits": []}
    hits = []
    for shp in shps:
        try:
            gdf = gpd.read_file(shp)
            if gdf.crs is None:
                gdf = gdf.set_crs("EPSG:4326")
            pt = gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(gdf.crs).iloc[0]
            sub = gdf[gdf.geometry.contains(pt)]
            for _, row in sub.iterrows():
                attrs = {k: v for k, v in row.items() if k != "geometry"}
                hits.append({"shp": os.path.basename(shp), "attrs": attrs})
        except Exception as e:
            hits.append({"shp": os.path.basename(shp), "error": str(e)})
    return {"状態": "判定済", "該当": len(hits) > 0, "hits": hits}

def zone_checks(lat, lon, ksj_root="ksj"):
    out = {}
    for key, desc in KSJ_LAYERS.items():
        folder = os.path.join(ksj_root, key)
        out[key] = {"説明": desc, **point_in_layer(folder, lat, lon)}
    return out


# ---------------------------------------------------------------------------
# 4b. 農地：地目・青地/白地・第何種農地（推定）
#    ★第何種農地は農地台帳/農地ナビの公表事項ではなく、農地転用許可(農地法5条)の
#      立地基準として農業委員会が個別判定するもの。ここでは公開データから「生ファクト」を
#      集め、簡易な立地基準で「推定」を出すだけ。確定は必ず農業委員会に確認すること。
# ---------------------------------------------------------------------------
def _polygon_area_ha(gdf_row_geom):
    """ポリゴン面積をha換算（等積図法 EPSG:6933）。集団農地規模の目安。"""
    import geopandas as gpd
    gs = gpd.GeoSeries([gdf_row_geom], crs="EPSG:4326").to_crs("EPSG:6933")
    return round(gs.area.iloc[0] / 10000.0, 2)

def nearest_station_m(lat, lon, radius=2000):
    """最寄り鉄道駅までの距離（第2種/第3種の立地基準の一要素）。"""
    q = f"""[out:json][timeout:40];
(node(around:{radius},{lat},{lon})[railway=station];
 node(around:{radius},{lat},{lon})[railway=halt];
 way(around:{radius},{lat},{lon})[railway=station];);
out center;"""
    try:
        js = _overpass(q)
    except Exception as e:
        return {"距離m": None, "備考": f"取得失敗: {e}"}
    dmin = 1e9; name = None
    for el in js.get("elements", []):
        c = el if "lat" in el else el.get("center", {})
        if "lat" not in c:
            continue
        d = haversine_m(lat, lon, c["lat"], c["lon"])
        if d < dmin:
            dmin = d; name = el.get("tags", {}).get("name")
    if dmin >= 1e9:
        return {"距離m": None, "備考": f"半径{radius}m内に駅なし"}
    return {"距離m": round(dmin, 1), "駅名": name}

def _collect_farmland_facts(lat, lon, chiban_chimoku=None, ksj_root="ksj"):
    """第何種の推定に使う『生ファクト』を公開データから収集して返す。"""
    facts = {}
    # 地目（一次は登記簿。引数で受け取る。未指定なら要登記簿）
    facts["地目_登記"] = chiban_chimoku or "（登記簿から取得。未指定）"

    # 農振農用地区分（青地/白地）: 国土数値情報 農業地域(A12) を点内包判定
    nochi = point_in_layer(os.path.join(ksj_root, "nochi"), lat, lon)
    facts["農業地域A12_状態"] = nochi["状態"]
    aochi = None; area_ha = None; raw = []
    if nochi.get("状態") == "判定済":
        if nochi.get("該当"):
            aochi = "農振地域内（青地=農用地区域の可能性。属性で要確認）"
            for h in nochi["hits"]:
                raw.append(h.get("attrs", {}))
        else:
            aochi = "農振農用地区域外（白地）の可能性"
    facts["農振区分_推定"] = aochi
    facts["農業地域A12_属性"] = raw

    # 市街化区域/調整区域: 国土数値情報 都市地域/用途地域(A29) を点内包判定
    shi = point_in_layer(os.path.join(ksj_root, "shigaika"), lat, lon)
    facts["区域区分A29_状態"] = shi["状態"]
    facts["市街化区域内"] = bool(shi.get("該当")) if shi.get("状態") == "判定済" else None
    facts["区域区分A29_属性"] = [h.get("attrs", {}) for h in shi.get("hits", [])]

    # 集団農地規模の目安: 点を含む農用地区域ポリゴンの面積(ha)
    try:
        import geopandas as gpd
        from shapely.geometry import Point
        shps = glob.glob(os.path.join(ksj_root, "nochi", "**", "*.shp"), recursive=True)
        for shp in shps:
            gdf = gpd.read_file(shp)
            if gdf.crs is None:
                gdf = gdf.set_crs("EPSG:4326")
            pt = gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(gdf.crs).iloc[0]
            sub = gdf[gdf.geometry.contains(pt)]
            if len(sub):
                area_ha = _polygon_area_ha(sub.to_crs("EPSG:4326").geometry.iloc[0])
                break
    except Exception:
        area_ha = None
    facts["含有農用地区域_面積ha"] = area_ha

    # 最寄り駅距離
    facts["最寄り駅"] = nearest_station_m(lat, lon)

    # 土地改良事業の有無：公開点データからは確実に取れない → 要確認
    facts["土地改良事業"] = "自動取得不可（要 土地改良区/自治体 確認）"
    return facts

def _estimate_farmland_class(facts):
    """簡易な立地基準による第何種農地の推定。※簡略版。確定は農業委員会。"""
    chimoku = str(facts.get("地目_登記", ""))
    # 非農地（雑種地・宅地等）なら第何種の判定対象外
    if any(k in chimoku for k in ["雑種地", "宅地", "山林", "原野", "公衆用道路"]):
        return {"第何種_推定": "農地非該当（第何種の判定対象外）",
                "推定根拠": [f"登記地目={chimoku} は農地(田/畑)でない"],
                "確度": "—",
                "注意": "現況主義のため、現況が耕作地でないかは農業委員会で要確認"}

    aochi = facts.get("農振区分_推定") or ""
    ekid = facts.get("最寄り駅", {}).get("距離m")
    area = facts.get("含有農用地区域_面積ha")
    shigaika = facts.get("市街化区域内")

    basis = []; est = None; conf = "低"

    if "青地" in aochi:
        basis.append("農振農用地区域内(青地)の可能性 → 転用は原則不可。まず農振除外(農振法13条2)が先行")
    if shigaika:
        est = "第3種農地 相当"; conf = "中"
        basis.append("市街化区域内 → 転用は届出制で最も容易(第3種相当)")
    elif ekid is not None and ekid <= 300:
        est = "第3種農地"; conf = "中"
        basis.append(f"鉄道駅 {ekid:.0f}m(≈300m以内) → 市街地化が著しい区域の傾向")
    elif ekid is not None and ekid <= 500:
        est = "第2種農地"; conf = "低"
        basis.append(f"鉄道駅 {ekid:.0f}m(≈500m圏) → 市街地化が見込まれる区域の傾向")
    elif area is not None and area >= 10:
        est = "第1種農地 寄り"; conf = "低"
        basis.append(f"含有農用地区域 約{area}ha(≧10ha集団の可能性) → 良好な営農条件で原則不許可寄り")
        if shigaika is False:
            basis.append("市街化調整/区域外かつ良好条件なら『甲種農地』の可能性も(要確認)")
    else:
        est = "第2種農地 寄り（その他）"; conf = "低"
        basis.append("駅至近でも大規模集団でもない → その他(第2種)の傾向")

    if area is not None:
        basis.append(f"周辺農用地区域の面積目安: 約{area}ha")
    if facts.get("土地改良事業"):
        basis.append("土地改良事業の有無は未取得（第1種/甲種の判定に影響）→ 要確認")

    return {"第何種_推定": est, "推定根拠": basis, "確度": conf}

def farmland_status(lat, lon, chiban_chimoku=None, ksj_root="ksj"):
    """農地の地目・青地白地の生ファクト＋第何種の推定＋農業委員会確認の但し書きを返す。"""
    facts = _collect_farmland_facts(lat, lon, chiban_chimoku, ksj_root)
    est = _estimate_farmland_class(facts)
    return {
        "生ファクト": facts,
        "推定結果": est,
        "確認事項": ("第何種農地(甲種/第1〜3種)の区分は農地台帳・農地ナビの公表事項ではなく、"
                    "農地転用許可(農地法5条)の立地基準として農業委員会が申請ごとに個別判定します。"
                    "本結果は公開データからの推定であり、確定には必ず農業委員会への確認が必要です。"),
    }


# ---------------------------------------------------------------------------
# 5. まとめ実行
# ---------------------------------------------------------------------------
def run_all(lat, lon, ksj_root="ksj"):
    res = {"入力": {"lat": lat, "lon": lon}}
    def safe(label, fn):
        try:
            res[label] = fn()
        except Exception as e:
            res[label] = {"error": str(e)}
        time.sleep(0.5)
    safe("逆ジオコード", lambda: reverse_geocode(lat, lon))
    safe("標高傾斜",     lambda: slope_deg(lat, lon))
    safe("ハザード",     lambda: hazards(lat, lon))
    safe("最寄り道路",   lambda: nearest_road(lat, lon))
    safe("最寄り建物m",  lambda: nearest_building(lat, lon))
    safe("海岸距離m",    lambda: coast_distance(lat, lon))
    safe("区域判定",     lambda: zone_checks(lat, lon, ksj_root))
    safe("農地ステータス", lambda: farmland_status(lat, lon, ksj_root=ksj_root))
    return res


if __name__ == "__main__":
    import argparse, yaml
    ap = argparse.ArgumentParser()
    ap.add_argument("--yaml", help="案件情報.yaml のパス")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--ksj", default="ksj", help="国土数値情報SHP の親フォルダ")
    ap.add_argument("--out", default="results.json")
    a = ap.parse_args()
    if a.yaml:
        with open(a.yaml, encoding="utf-8") as f:
            meta = yaml.safe_load(f)
        latlon = str(meta["緯度経度"]).replace("　", " ").replace(",", " ").split()
        lat, lon = float(latlon[0]), float(latlon[1])
    else:
        lat, lon = a.lat, a.lon
    result = run_all(lat, lon, ksj_root=a.ksj)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"wrote {a.out}")
