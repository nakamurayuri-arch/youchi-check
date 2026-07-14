# -*- coding: utf-8 -*-
"""
資料解析: 登記簿・公図・電力申請PDFから項目を抽出（ベストエフォート＋要確認フラグ）。
テキストはNFKC正規化（全角数字/全角空白→半角）してから正規表現で抽出する。
"""
from __future__ import annotations
import re, unicodedata, os


def norm(t: str) -> str:
    return unicodedata.normalize("NFKC", t or "")

def read_pdf_text(path: str) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(path) as pdf:
        for pg in pdf.pages:
            parts.append(pg.extract_text() or "")
    return norm("\n".join(parts))


# ---------------------------------------------------------------------------
# 資料の分類（ファイル名ベース。フォルダ一括読み込み用）
# ---------------------------------------------------------------------------
LAND_KEYS = ["登記", "謄本", "全部事項", "公図", "字図", "地図", "所有者事項"]
ELEC_KEYS = ["接続検討", "技術検討", "契約申込", "供給", "保証金", "負担金", "振込",
             "電力使用", "発電量調整", "回答書", "受電"]
POS_KEYS  = ["位置情報", "座標", "google", "map"]

def classify(filename: str) -> str:
    n = filename.lower()
    base = os.path.basename(filename)
    if any(k in base for k in POS_KEYS) or "位置情報" in base:
        return "position"
    if any(k in base for k in LAND_KEYS):
        return "land"
    if any(k in base for k in ELEC_KEYS):
        return "electric"
    return "other"


# ---------------------------------------------------------------------------
# 緯度経度の抽出（位置情報PDF等から）
# ---------------------------------------------------------------------------
def extract_latlon(text: str):
    t = norm(text)
    # 10進（URL等）: 33.0598671, 131.9332333
    m = re.search(r"(\d{1,3}\.\d{4,})[,\s]+(\d{1,3}\.\d{4,})", t)
    if m:
        return float(m.group(1)), float(m.group(2))
    # 度分秒: 33°03'35.5"N 131°56'02.0"E
    m = re.search(r"(\d+)[°](\d+)['′](\d+\.?\d*)[\"″]?\s*N\s*(\d+)[°](\d+)['′](\d+\.?\d*)[\"″]?\s*E", t)
    if m:
        lat = int(m.group(1)) + int(m.group(2))/60 + float(m.group(3))/3600
        lon = int(m.group(4)) + int(m.group(5))/60 + float(m.group(6))/3600
        return round(lat, 7), round(lon, 7)
    return None


# ---------------------------------------------------------------------------
# 登記簿・公図
# ---------------------------------------------------------------------------
CHIMOKU = ["宅地", "田", "畑", "山林", "原野", "雑種地", "公衆用道路",
           "保安林", "池沼", "鉱泉地", "牧場", "墓地"]

def parse_land(text: str) -> dict:
    t = norm(text)
    r = {"_warn": []}

    m = re.search(r"(\d+)\s*番(?:の?(\d+))?", t)
    r["地番"] = (m.group(0).replace(" ", "") if m else None)

    # 現況地目 = 表題部で最後に現れた地目
    last, pos = None, -1
    for c in CHIMOKU:
        i = t.rfind(c)
        if i > pos:
            pos, last = i, c
    r["地目_現況推定"] = last
    r["地目_出現"] = [c for c in CHIMOKU if c in t]

    areas = re.findall(r"([\d,]+)\s*(?:㎡|平方メートル|m2|m²)", t)
    r["地積_候補㎡"] = areas

    # 所有者（住所＋氏名が改行で分かれるため次行も拾う）
    owners = re.findall(r"所有者\s*([^\n]{2,40}(?:\n[^\n]{0,25})?)", t)
    owners = [re.sub(r"\s+", " ", o).strip() for o in owners]
    r["所有者_候補"] = owners
    r["現所有者_推定"] = owners[-1] if owners else None  # 末尾＝最新順位

    dates = re.findall(r"(?:平成|令和|昭和)\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日", t)
    r["登記日付_候補"] = dates
    # 甲区の最終更新（相続リスク判定用）: 出現する和暦のうち最新年号
    r["最終更新_推定"] = dates[-1] if dates else None

    r["乙区記載"] = ("あり(抵当権等の有無は抹消線を要目視)"
                    if re.search(r"乙\s*区", t) else "なし/抜粋外(要 原本 全部事項)")

    if "14条" in t or "法第14条" in t:
        r["公図種別"] = "14条地図(信頼度高)"
    elif "準ずる" in t:
        r["公図種別"] = "地図に準ずる図面(要注意)"
    else:
        r["公図種別"] = None

    # 隣接に道・水（法定外公共物）の気配
    r["法定外公共物_気配"] = bool(re.search(r"(公衆用道路|水路|里道|畦畔)", t))

    if not r["現所有者_推定"]:
        r["_warn"].append("所有者を自動抽出できず（画像PDF/レイアウト差の可能性）→要目視")
    return r


# ---------------------------------------------------------------------------
# 電力申請一式
# ---------------------------------------------------------------------------
def parse_electric(text: str) -> dict:
    t = norm(text)
    r = {"_warn": []}

    m = (re.search(r"受付番号[^\d]{0,8}(\d{4,6})", t)
         or re.search(r"管理\s*No\.?\s*(\d{4,6})", t))
    r["受付番号"] = m.group(1) if m else None

    m = re.search(r"回答日\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", t)
    r["接続検討回答日"] = f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}" if m else None

    m = re.search(r"保証金請求額[^\d]{0,10}([\d,]+)", t)
    r["保証金額"] = m.group(1) if m else None

    yen = re.findall(r"([\d,]{6,})\s*円", t)
    r["負担金額"] = max(yen, key=lambda a: int(a.replace(",", ""))) if yen else None

    # 振込（大分銀行ビジネスダイレクト等: 指定日 YYYY MM DD ... 入金金額 N）
    furi = re.findall(r"指定日\s*(\d{4})\s*(\d{1,2})\s*(\d{1,2}).*?入金金額\s*([\d,]+)", t, re.S)
    r["振込_候補"] = [{"日": f"{y}/{int(mo):02d}/{int(da):02d}", "額": amt} for (y, mo, da, amt) in furi]

    m = (re.search(r"最大受電電力[^\d]{0,8}([\d,]+)\s*kW", t)
         or re.search(r"受電電力[^\d]{0,10}([\d,]+)\s*kW", t))
    r["受電電力kW"] = m.group(1) if m else None

    m = re.search(r"入金後\s*(?:約)?\s*(\d+)\s*[ヶか]?月", t)
    r["工期ヶ月"] = int(m.group(1)) if m else None

    r["ノンファーム型接続"] = bool(re.search(r"ノンファーム", t))
    r["連系可否_可"] = bool(re.search(r"連系可否[：:]*\s*可", t)) or ("可" in t and "連系可否" in t)

    return r


# ---------------------------------------------------------------------------
# 受電日・運開・案件化の計算
# ---------------------------------------------------------------------------
def add_months(ym: str, months: int):
    """'YYYY/MM/DD' + months → 'YYYY/MM 頃'"""
    y, m, d = [int(x) for x in ym.split("/")]
    m2 = m + months
    y += (m2 - 1) // 12
    m2 = (m2 - 1) % 12 + 1
    return f"{y}/{m2:02d} 頃"

def calc_timeline(elec: dict) -> dict:
    out = {}
    # 負担金支払日 = 振込のうち最大額（負担金相当）
    furi = elec.get("振込_候補") or []
    def num(a): return int(a["額"].replace(",", ""))
    hosho_pay = min(furi, key=num, default=None)   # 小さい方＝保証金の目安
    futan_pay = max(furi, key=num, default=None)   # 大きい方＝負担金の目安
    out["保証金支払_推定"] = hosho_pay
    out["負担金支払_推定"] = futan_pay
    out["案件化"] = "到達(負担金支払済)" if futan_pay else "未到達/要確認"
    koki = elec.get("工期ヶ月")
    if futan_pay and koki:
        out["受電日_推定"] = add_months(futan_pay["日"], koki)
        out["運開JEPX_推定"] = add_months(futan_pay["日"], koki + 1)
        out["運開EPRX_推定"] = add_months(futan_pay["日"], koki + 4)
    else:
        out["受電日_推定"] = "計算不可（負担金支払日 or 工期 が未抽出）"
    return out
