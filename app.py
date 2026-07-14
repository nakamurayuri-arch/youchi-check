# -*- coding: utf-8 -*-
"""
用地チェック（緯度経度だけ・全項目）— 無料ホスト設置用
起動: streamlit run app.py
入力: 緯度経度のみ（会社情報・PDFは使わない → 公開情報のみ）
"""
import os, sys, re, tempfile
import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tools"))
import site_checks, build_report

st.set_page_config(page_title="用地チェック（緯度経度）", layout="wide")
st.title("用地チェック（緯度経度だけ）")
st.caption("緯度経度を入れるだけで、ハザード・傾斜・市街化調整・自然公園・砂防・農地・道路・海岸距離などを自動判定します。会社情報は使いません。")

KSJ_ROOT = os.environ.get("KSJ_ROOT", "ksj")

def parse_coord(s):
    s = (s or "").strip()
    m = re.search(r"(-?\d+\.\d+)\s*[, ]\s*(-?\d+\.\d+)", s)
    if m: return float(m.group(1)), float(m.group(2))
    m = re.search(r"(\d+)[°](\d+)['′](\d+\.?\d*)\"?\s*N[, ]*\s*(\d+)[°](\d+)['′](\d+\.?\d*)\"?\s*E", s, re.I)
    if m:
        lat = int(m.group(1)) + int(m.group(2))/60 + float(m.group(3))/3600
        lon = int(m.group(4)) + int(m.group(5))/60 + float(m.group(6))/3600
        return round(lat, 7), round(lon, 7)
    return None

coord = st.text_input("緯度, 経度", placeholder="例: 33.0598671, 131.9332333")
go = st.button("▶ チェックする", type="primary")

def hz(v):
    if isinstance(v, dict):
        if v.get("該当") is True:  return "⚠ 該当", "red"
        if v.get("該当") is False: return "○ 非該当", "green"
    return "要確認", "gray"

def zone_row(z, name, lat, lon, link):
    """区域判定の1行を (ラベル, 値, 色, リンク) で返す"""
    if not isinstance(z, dict):
        return name, "要確認", "gray", link
    if z.get("状態") == "SHP未配置":
        return name, "データ未設置 → リンクで確認", "gray", link
    if z.get("該当") is True:
        return name, "⚠ 該当", "red", link
    return name, "○ 非該当", "green", link

def show_rows(rows):
    for r in rows:
        name, val = r[0], r[1]
        color = r[2] if len(r) > 2 else "black"
        link = r[3] if len(r) > 3 else None
        cols = st.columns([2, 3, 2])
        cols[0].markdown(f"**{name}**")
        cmap = {"red": ":red", "green": ":green", "gray": ":gray"}
        cols[1].markdown(f"{cmap.get(color,'')}[{val}]" if color in cmap else val)
        if link: cols[2].markdown(f"[公式マップで確認]({link})")

if go:
    c = parse_coord(coord)
    if not c:
        st.error("緯度経度を『33.0598671, 131.9332333』の形で入力してください。"); st.stop()
    lat, lon = c
    with st.spinner("判定中…（10〜40秒）"):
        res = site_checks.run_all(lat, lon, ksj_root=KSJ_ROOT)

    # リンク（その地点に合わせて中心化）
    L_haz  = f"https://disaportal.gsi.go.jp/maps/?ll={lat},{lon}&z=17&base=pale"
    L_noch = "https://map.maff.go.jp/"
    L_eadas= "https://eadas.env.go.jp/eiadb/ebidbs/"
    L_gmap = f"https://www.google.com/maps?q={lat},{lon}"

    st.subheader("基本")
    sl = res.get("標高傾斜") or {}
    show_rows([
        ["緯度経度", f"{lat:.6f}, {lon:.6f}"],
        ["住所（推定）", res.get("逆ジオコード") or "取得できず"],
        ["標高", f"{sl.get('elevation_m','—')} m" if isinstance(sl, dict) else "—"],
        ["傾斜（推定）", f"{sl.get('max_slope_deg','—')}°" if isinstance(sl, dict) else "—"],
    ])

    st.subheader("ハザード")
    haz = res.get("ハザード") or {}
    show_rows([[k, *hz(v), L_haz] for k, v in haz.items()])

    st.subheader("許認可・区域")
    z = res.get("区域判定") or {}
    show_rows([
        zone_row(z.get("shigaika"),   "市街化調整区域（区域区分）", lat, lon, L_eadas),
        zone_row(z.get("youto"),      "用途地域", lat, lon, L_eadas),
        zone_row(z.get("shizen_kouen"),"自然公園（国立/国定/県立）", lat, lon, L_eadas),
        zone_row(z.get("sabo"),       "砂防指定地", lat, lon, L_eadas),
        zone_row(z.get("jisuberi"),   "地すべり防止区域", lat, lon, L_eadas),
        zone_row(z.get("kyukeisha"),  "急傾斜地崩壊危険区域", lat, lon, L_eadas),
        ["埋蔵文化財包蔵地", "要確認（全国統一データなし）→ 自治体教委", "gray", L_eadas],
    ])

    st.subheader("農地")
    farm = res.get("農地ステータス") or {}
    est = (farm.get("推定結果") or {}) if isinstance(farm, dict) else {}
    facts = (farm.get("生ファクト") or {}) if isinstance(farm, dict) else {}
    show_rows([
        ["青地/白地（農振区分）", facts.get("農振区分_推定") or "要確認", "gray", L_noch],
        ["第何種農地（推定）", est.get("第何種_推定") or "要確認", "gray", L_noch],
    ])
    if est.get("推定根拠"):
        st.caption("推定根拠: " + " / ".join(est["推定根拠"]))
    st.caption("※第何種農地の確定は農業委員会への確認が必要です（本結果は公開データからの推定）。")

    st.subheader("周辺")
    road = res.get("最寄り道路") or {}
    bldg = res.get("最寄り建物m"); coast = res.get("海岸距離m")
    bt = "周辺に建物なし" if bldg in (None, "要確認") else (f"{bldg} m（100m未満・要注意）" if isinstance(bldg,(int,float)) and bldg<100 else f"{bldg} m")
    ct = "8km内に海岸なし（内陸）" if coast in (None, "要確認") else (f"{coast} m（500m未満・重塩害要注意）" if isinstance(coast,(int,float)) and coast<500 else f"{coast} m")
    show_rows([
        ["最寄り道路", f"{road.get('種別概略','—')}（{road.get('名称','')}） {road.get('距離m','')}m" if isinstance(road, dict) else "—"],
        ["最寄り建物（住宅目安）", bt, "red" if isinstance(bldg,(int,float)) and bldg<100 else "black"],
        ["海岸まで（重塩害）", ct, "red" if isinstance(coast,(int,float)) and coast<500 else "black"],
    ])

    st.markdown(f"##### 公式マップ： [ハザード]({L_haz})　[農地ナビ]({L_noch})　[EADAS環境]({L_eadas})　[Googleマップ]({L_gmap})")

    # xlsx ダウンロード
    try:
        outdir = tempfile.mkdtemp()
        meta = {"案件名": "用地チェック", "所在": res.get("逆ジオコード") or "", "緯度経度": f"{lat}, {lon}"}
        files = build_report.build({"立地": res, "入力": {"lat": lat, "lon": lon}}, meta, outdir)
        st.download_button("📊 結果をxlsxでダウンロード", open(files["xlsx"], "rb").read(),
                           file_name="用地チェック.xlsx")
    except Exception as e:
        st.caption(f"(xlsx生成はスキップ: {e})")

    st.info("これは公開データからの一次確認です。市街化調整・農地・埋蔵文化財などの確定は、上のリンク先や各自治体・現地でご確認ください。")
