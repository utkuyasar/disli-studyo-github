#!/usr/bin/env python3
"""
Düz Dişli Üreteci — yerel web uygulaması
Çalıştır:  python app.py   →  tarayıcıda http://127.0.0.1:8000
"""
import base64
import math
import tempfile
import threading
import uuid
import webbrowser
from pathlib import Path

from typing import Literal

import cadquery as cq
import uvicorn
from cq_gears import RingGear, SpurGear
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
STEP_DIR = Path(tempfile.gettempdir()) / "disli_studyo"
STEP_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Dişli Stüdyosu")

# ----------------------------- modeller -----------------------------

class GearParams(BaseModel):
    gear_type: Literal["spur", "ring", "pair", "ipair"] = Field(
        default="spur",
        description="spur = düz/helis dişli, ring = halka (iç) dişli, "
                    "pair = dış dişli çifti, ipair = pinyon + halka çifti")
    mate_teeth: int = Field(default=40, ge=6, le=300,
                            description="Karşı dişli z2 (sadece çift)")
    shift2: float = Field(default=0.0, ge=-0.9, le=1.0,
                          description="Karşı dişli profil kaydırma x2 (sadece çift)")
    module: float = Field(gt=0.05, le=50, description="Modül (mm)")
    teeth: int = Field(ge=6, le=300, description="Diş sayısı")
    width: float = Field(gt=0.1, le=500, description="Diş genişliği (mm)")
    bore: float = Field(default=0.0, ge=0.0, le=1000,
                        description="Delik çapı (mm), 0 = deliksiz (sadece düz dişli)")
    rim_width: float = Field(default=0.0, ge=0.0, le=500,
                             description="Halka et kalınlığı (mm), 0 = otomatik 3·m")
    pressure_angle: float = Field(default=20.0, ge=14.0, le=25.0,
                                  description="Normal basınç açısı αn (°)")
    helix_angle: float = Field(default=0.0, ge=-45.0, le=45.0,
                               description="Helis açısı β (°), 0 = düz, + sağ helis")
    profile_shift: float = Field(default=0.0, ge=-0.9, le=1.0,
                                 description="Profil kaydırma katsayısı x "
                                             "(sadece dış dişli)")


# STEP dosyalarını id ile eşle (indirme için)
_files: dict[str, dict] = {}


def transverse_params(p: GearParams):
    """Normal düzlem → alın düzlemi dönüşümleri (Jelaska §2.11.2).

    Kullanıcı normal modül mn ve normal basınç açısı αn girer; cq_gears
    profili alın düzleminde çizdiği için mt = mn/cosβ, tan αt = tan αn/cosβ.
    """
    beta = math.radians(p.helix_angle)
    cosb = math.cos(beta)
    m_t = p.module / cosb
    alpha_t = math.degrees(math.atan(math.tan(math.radians(p.pressure_angle)) / cosb))
    return m_t, alpha_t, cosb


def build_gear(p: GearParams):
    m_t, alpha_t, cosb = transverse_params(p)
    if p.gear_type == "ring":
        rim = p.rim_width if p.rim_width > 0 else 3.0 * p.module
        # RingGear addendum/dedendum katsayısı almıyor; alın düzleminde
        # diş yüksekliğini mn'e sabitlemek için (ht* = h*·cosβ, §2.11 denk. 2.209)
        # sınıf katsayılarını türetilmiş sınıfla eziyoruz.
        cls = type("RingGearT", (RingGear,), {"ka": 1.0 * cosb, "kd": 1.25 * cosb})
        gear = cls(
            module=m_t,
            teeth_number=p.teeth,
            width=p.width,
            rim_width=rim,
            pressure_angle=alpha_t,
            helix_angle=p.helix_angle,
        )
    else:
        x = p.profile_shift
        # Profil kaydırma (Jelaska §2.6):
        #   da = d + 2·mn·(1+x)  →  addendum katsayısı cosβ·(1+x)
        #   df = d − 2·mn·(1.25−x)  →  dedendum katsayısı cosβ·(1.25−x)
        #   s  = mt·(π/2 + 2·x·tan αn)  →  cq_gears'ta backlash = −2·x·cosβ
        #        (s0 = m·(π/2 − backlash·tan αt), tan αn = cosβ·tan αt)
        gear = SpurGear(
            module=m_t,
            teeth_number=p.teeth,
            width=p.width,
            pressure_angle=alpha_t,
            helix_angle=p.helix_angle,
            addendum_coeff=cosb * (1.0 + x),
            dedendum_coeff=cosb * (1.25 - x),
            backlash=-2.0 * x * cosb,
            bore_d=p.bore if p.bore > 0 else None,
        )
    return cq.Workplane("XY").gear(gear)


def pair_params(p: GearParams):
    """Dişli çifti parametreleri (Jelaska §2.7.1–2.7.3).

    inv αwt = inv αt + 2(x1+x2)·tan αn/(z1+z2); aw = a0·cos αt/cos αwt.
    Diş üstü kısaltma k = (aw − a0)/mn − (x1+x2) → tepe boşluğu 0.25·mn korunur.
    """
    m_t, alpha_t, cosb = transverse_params(p)
    at = math.radians(alpha_t)
    z1, z2 = p.teeth, p.mate_teeth
    x1, x2 = p.profile_shift, p.shift2
    a0 = 0.5 * m_t * (z1 + z2)
    q = _inv(at) + 2 * (x1 + x2) * math.tan(math.radians(p.pressure_angle)) / (z1 + z2)
    awt = _inv_solve(q)
    aw = a0 * math.cos(at) / math.cos(awt)
    k = (aw - a0) / p.module - (x1 + x2)
    return m_t, alpha_t, cosb, at, awt, aw, a0, k


def build_pair(p: GearParams):
    m_t, alpha_t, cosb, at, awt, aw, a0, k = pair_params(p)
    x1, x2 = p.profile_shift, p.shift2
    gears = []
    for z, x, hand in ((p.teeth, x1, 1.0), (p.mate_teeth, x2, -1.0)):
        g = SpurGear(
            module=m_t,
            teeth_number=z,
            width=p.width,
            pressure_angle=alpha_t,
            helix_angle=p.helix_angle * hand,
            addendum_coeff=max(cosb * (1.0 + x + k), 0.05),
            dedendum_coeff=cosb * (1.25 - x),
            backlash=-2.0 * x * cosb,
            bore_d=p.bore if p.bore > 0 else None,
        )
        gears.append(cq.Workplane("XY").gear(g).val())
    s1, s2 = gears
    rot2 = math.degrees(math.pi / p.mate_teeth) if p.mate_teeth % 2 == 0 else 0.0
    s2 = s2.rotate((0, 0, 0), (0, 0, 1), rot2).translate((aw, 0, 0))
    return cq.Compound.makeCompound([s1, s2])


def _inv(a: float) -> float:
    """Evolvent fonksiyonu inv(α) = tan α − α (radyan)."""
    return math.tan(a) - a


def _inv_solve(q: float) -> float:
    """inv(α) = q denklemini Newton yöntemiyle çöz (Jelaska, denk. 2.113 notu)."""
    a = 0.35  # ~20° başlangıç
    for _ in range(50):
        f = math.tan(a) - a - q
        df = math.tan(a) ** 2
        if df == 0:
            break
        step = f / df
        a -= step
        if abs(step) < 1e-12:
            break
    return a


def control_measures(p: GearParams, m_t, alpha_t, cosb, d_pitch, d_base,
                     d_tip, d_root):
    """Kontrol ölçüleri (Jelaska §2.6.7): span Wk, bilye ölçüsü M, kiriş kalınlığı.

    Dış (düz/helis) dişliler için. Helis dişlide z·inv αt kullanılır,
    ölçüler normal düzlemdedir.
    """
    an = math.radians(p.pressure_angle)
    at = math.radians(alpha_t)
    z, mn, x = p.teeth, p.module, p.profile_shift
    out = {}

    # --- Span ölçüsü W (denk. 2.103), ölçülen diş sayısı zw ≈ z/9 + 1 (2.104)
    zw = max(2, round(z / 9 + 1))
    W = mn * math.cos(an) * (math.pi * (zw - 0.5) + z * _inv(at)) \
        + 2 * x * mn * math.sin(an)
    out["zw"] = zw
    out["W"] = round(W, 3)
    if p.helix_angle != 0:
        # ölçüm temel silindir helis açısı yönünde: genişlik yeterli mi?
        sin_bb = math.sin(math.radians(abs(p.helix_angle))) * math.cos(an)
        if p.width < W * sin_bb + 1.0:
            out["W_note"] = "Diş genişliği span ölçümü için yetersiz."

    # --- Bilye üzerinden ölçü M (denk. 2.110–2.114), D ≈ 1.7·mn
    D = round(1.7 * mn, 2)
    q = D / (mn * z * math.cos(an)) + _inv(at) \
        - math.pi / (2 * z) + 2 * x * math.tan(an) / z
    a_D = _inv_solve(q)
    d_D = m_t * z * math.cos(at) / math.cos(a_D)
    M = None
    if z % 2 == 0:
        M = d_D + D
    elif p.helix_angle == 0:
        M = d_D * math.cos(math.pi / (2 * z)) + D  # tek z, düz (2.114)
    # tek z + helis: özel hesap gerekir (kitap §2.6.7.4) → gösterme
    out["D"] = D
    if M is not None:
        if d_D - D <= d_root:
            out["M_note"] = "Bilye diş dibine değiyor (2.117) — D'yi büyüt."
        elif M <= d_tip:
            out["M_note"] = "M < da: ölçüm yapılamaz (2.118)."
        out["M"] = round(M, 3)

    # --- Kiriş diş kalınlığı (§2.6.7.1), heliste sanal diş sayısı zv = z/cos³β
    zv = z / cosb**3
    psi = (math.pi / 2 + 2 * x * math.tan(an)) / zv
    out["s_chord"] = round(mn * zv * math.sin(psi), 3)
    out["h_chord"] = round(
        0.5 * mn * zv * (1 - math.cos(psi)) + 0.5 * (d_tip - d_pitch), 3)
    return out


# ------------------- ISO 6336 / DIN 3990 mukavemet -------------------

# Malzeme tablosu: (σHlim, σFlim) N/mm² — ISO 6336-5 MQ kalitesi orta değerler
MATERIALS = {
    "C45":        {"name": "C45 ıslah",              "sH": 590.0,  "sF": 200.0, "ZE": 189.8},
    "42CrMo4":    {"name": "42CrMo4 ıslah",          "sH": 740.0,  "sF": 250.0, "ZE": 189.8},
    "16MnCr5":    {"name": "16MnCr5 sementasyon",    "sH": 1470.0, "sF": 460.0, "ZE": 189.8},
    "18CrNiMo76": {"name": "18CrNiMo7-6 sementasyon","sH": 1500.0, "sF": 500.0, "ZE": 189.8},
    "GG25":       {"name": "GG25 dökme demir",       "sH": 360.0,  "sF": 85.0,  "ZE": 165.4},
}


def tooth_root_geometry(z, x, beta_deg, an_deg, mn):
    """ISO 6336-3 Method C: 30° teğet kesiti (sFn, ρF) ve uçtan yükleme (hFa).

    ISO 53 temel kremayer: haP=1·mn, hfP=1.25·mn, ρfP=0.38·mn.
    Doğrulama: z=20,x=0 → YFa=2.800, YSa=1.553 (DIN 3990 grafikleriyle birebir).
    """
    haP, hfP, rofP = 1.0, 1.25, 0.38
    an = math.radians(an_deg)
    beta = math.radians(beta_deg)
    bb = math.asin(math.sin(beta) * math.cos(an))       # temel helis açısı
    zn = z / (math.cos(bb) ** 2 * math.cos(beta))       # sanal diş sayısı
    E = math.pi / 4 * mn - hfP * mn * math.tan(an) \
        - (1 - math.sin(an)) * rofP * mn / math.cos(an)
    G = rofP - hfP + x
    H = 2 / zn * (math.pi / 2 - E / mn) - math.pi / 3
    th = math.pi / 6
    for _ in range(100):
        th_new = 2 * G / zn * math.tan(th) - H
        if abs(th_new - th) < 1e-12:
            th = th_new
            break
        th = th_new
    sFn = mn * (zn * math.sin(math.pi / 3 - th)
                + math.sqrt(3) * (G / math.cos(th) - rofP))
    roF = mn * (rofP + 2 * G ** 2
                / (math.cos(th) * (zn * math.cos(th) ** 2 - 2 * G)))
    dn = mn * zn
    dbn = dn * math.cos(an)
    dan = dn + 2 * mn * (haP + x)
    aan = math.acos(dbn / dan)
    ga = (0.5 * math.pi + 2 * x * math.tan(an)) / zn + _inv(an) - _inv(aan)
    aFan = aan - ga
    hFa = 0.5 * mn * ((math.cos(ga) - math.sin(ga) * math.tan(aFan)) * dan / mn
                      - zn * math.cos(math.pi / 3 - th)
                      - (G / math.cos(th) - rofP))
    YFa = (6 * hFa / mn * math.cos(aFan)) / ((sFn / mn) ** 2 * math.cos(an))
    L = sFn / hFa
    qs = sFn / (2 * roF)
    YSa = (1.2 + 0.13 * L) * qs ** (1 / (1.21 + 2.3 / L))   # kitap denk. 3.96
    return {"YFa": YFa, "YSa": YSa, "sFn": sFn, "roF": roF,
            "hFa": hFa, "qs": qs, "zn": zn, "bb": bb}


class StrengthParams(BaseModel):
    # dişli geometrisi (pinyon = bizim dişli)
    module: float = Field(gt=0.05, le=50)
    teeth: int = Field(ge=6, le=300)
    width: float = Field(gt=0.1, le=500)
    pressure_angle: float = Field(default=20.0, ge=14.0, le=25.0)
    helix_angle: float = Field(default=0.0, ge=-45.0, le=45.0)
    profile_shift: float = Field(default=0.0, ge=-0.9, le=1.0)
    # çalışma koşulları
    mate_teeth: int = Field(ge=6, le=1000, description="Karşı dişli z2")
    torque: float = Field(gt=0, le=1e6, description="Pinyon momenti T1 (Nm)")
    speed: float = Field(gt=0, le=1e5, description="Pinyon devri n1 (d/dk)")
    ka: float = Field(default=1.25, ge=1.0, le=3.0, description="Uygulama faktörü KA")
    kv: float = Field(default=1.1, ge=1.0, le=2.0, description="Dinamik faktör Kv")
    khb: float = Field(default=1.25, ge=1.0, le=2.5,
                       description="Yayılma faktörü KHβ = KFβ")
    material: str = Field(default="C45")


@app.post("/api/strength")
def strength(p: StrengthParams):
    if p.material not in MATERIALS:
        raise HTTPException(422, "Bilinmeyen malzeme.")
    mat = MATERIALS[p.material]
    mn, z1, z2, x1 = p.module, p.teeth, p.mate_teeth, p.profile_shift
    an = math.radians(p.pressure_angle)
    beta = math.radians(p.helix_angle)
    cosb = math.cos(beta)
    m_t = mn / cosb
    at = math.atan(math.tan(an) / cosb)
    bb = math.asin(math.sin(beta) * math.cos(an))
    u = z2 / z1
    d1 = m_t * z1
    d2 = m_t * z2
    # varsayım: x2 = -x1 → aks mesafesi standart, αwt = αt
    x2 = -x1
    db1, db2 = d1 * math.cos(at), d2 * math.cos(at)
    da1 = d1 + 2 * mn * (1 + x1)
    da2 = d2 + 2 * mn * (1 + x2)
    a_w = 0.5 * m_t * (z1 + z2)

    # kavrama oranları
    e_a = (0.5 * math.sqrt(max(da1**2 - db1**2, 0))
           + 0.5 * math.sqrt(max(da2**2 - db2**2, 0))
           - a_w * math.sin(at)) / (math.pi * m_t * math.cos(at))
    e_b = p.width * abs(math.sin(beta)) / (math.pi * mn)
    e_an = e_a / math.cos(bb) ** 2

    # kuvvet ve hız
    Ft = 2000.0 * p.torque / d1          # N
    v = math.pi * d1 * p.speed / 60000.0  # m/s

    # --- diş dibi (ISO 6336-3 Method C) ---
    g = tooth_root_geometry(z1, x1, p.helix_angle, p.pressure_angle, mn)
    Ye = 0.25 + 0.75 / max(e_an, 1.0)
    Yb = 1 - min(e_b, 1.0) * min(abs(p.helix_angle), 30.0) / 120.0
    Yb = max(Yb, 0.75)
    sF0 = Ft / (p.width * mn) * g["YFa"] * g["YSa"] * Ye * Yb
    sF = sF0 * p.ka * p.kv * p.khb
    sFG = 2.0 * mat["sF"]                # σFE = σFlim·YST (YST = 2), YNT = 1
    SF = sFG / sF if sF > 0 else None

    # --- yüzey basıncı (ISO 6336-2, αwt = αt) ---
    ZH = math.sqrt(2 * math.cos(bb) / (math.cos(at) ** 2 * math.tan(at)))
    if e_b >= 1:
        Zeps = math.sqrt(1 / e_a)
    else:
        Zeps = math.sqrt((4 - e_a) / 3 * (1 - e_b) + e_b / e_a)
    Zbeta = 1 / math.sqrt(cosb)
    sH0 = ZH * mat["ZE"] * Zeps * Zbeta * math.sqrt(
        Ft * (u + 1) / (d1 * p.width * u))
    sH = sH0 * math.sqrt(p.ka * p.kv * p.khb)
    SH = mat["sH"] / sH if sH > 0 else None

    return {
        "material": mat["name"],
        "Ft": round(Ft, 1), "v": round(v, 2), "u": round(u, 3),
        "eps_alpha": round(e_a, 3), "eps_beta": round(e_b, 3),
        "YFa": round(g["YFa"], 3), "YSa": round(g["YSa"], 3),
        "Yeps": round(Ye, 3), "Ybeta": round(Yb, 3),
        "sigma_F": round(sF, 1), "sigma_FG": round(sFG, 1),
        "SF": round(SF, 2),
        "ZH": round(ZH, 3), "Zeps": round(Zeps, 3),
        "sigma_H": round(sH, 1), "sigma_HG": round(mat["sH"], 1),
        "SH": round(SH, 2),
        "note": "Varsayım: x2 = −x1 (standart aks mesafesi). "
                "σFG = 2·σFlim (YST=2, YNT=1); Z/Y ömür-yüzey faktörleri = 1.",
    }


# ----------------------------- API -----------------------------

@app.post("/api/generate")
def generate(p: GearParams):
    if p.gear_type == "pair":
        return generate_pair(p)
    if p.gear_type == "ipair":
        return generate_ipair(p)
    m_t, alpha_t, cosb = transverse_params(p)
    d_pitch = m_t * p.teeth                       # d = z·mn/cosβ
    d_base = d_pitch * math.cos(math.radians(alpha_t))

    if p.gear_type == "ring":
        # İç dişli (Jelaska §2.8.4): diş üstü merkeze doğru, diş dibi dışa doğru
        d_tip = d_pitch - 2 * p.module
        d_root = d_pitch + 2.5 * p.module
        rim = p.rim_width if p.rim_width > 0 else 3.0 * p.module
        d_outer = d_root + 2 * rim
    else:
        x = p.profile_shift
        d_tip = d_pitch + 2 * p.module * (1 + x)
        d_root = d_pitch - 2 * p.module * (1.25 - x)
        d_outer = None
        if p.bore > 0 and p.bore >= d_root - 2 * p.module:
            raise HTTPException(
                422,
                f"Delik çapı çok büyük: diş dibi çapı {d_root:.2f} mm. "
                f"Delik en fazla {d_root - 2*p.module:.1f} mm olabilir.",
            )

    try:
        body = build_gear(p)
    except Exception as e:  # cq_gears geometri hatası
        raise HTTPException(422, f"Geometri üretilemedi: {e}")

    solid = body.val()

    # 3D önizleme için üçgen ağ (mesh)
    verts, tris = solid.tessellate(tolerance=max(0.02, p.module / 60))
    vertices = [c for v in verts for c in (v.x, v.y, v.z)]
    faces = [i for t in tris for i in t]

    # STEP dosyasını üret ve sakla
    gid = uuid.uuid4().hex[:12]
    step_path = STEP_DIR / f"{gid}.stp"
    cq.exporters.export(body, str(step_path), exportType="STEP")
    if p.gear_type == "ring":
        prefix = "halka_disli"
    elif p.helix_angle != 0:
        prefix = "helis_disli"
    else:
        prefix = "duz_disli"
    beta_tag = f"_beta{p.helix_angle:g}" if p.helix_angle != 0 else ""
    x_tag = (f"_x{p.profile_shift:g}"
             if p.profile_shift != 0 and p.gear_type != "ring" else "")
    nice_name = f"{prefix}_m{p.module:g}_z{p.teeth}_b{p.width:g}{beta_tag}{x_tag}.stp"
    _files[gid] = {"path": step_path, "name": nice_name}

    warnings: list[str] = []
    sa_n = None
    if p.gear_type == "ring":
        # İç dişlide alttan kesilme yoktur (Jelaska §2.8.4 / §2.9.1.1).
        # Ama d_tip < d_base ise diş üstü evolvent bölgenin dışında kalır.
        if d_tip < d_base:
            warnings.append(
                "İç dişlinin diş üstü temel dairenin içinde kalıyor — profilin "
                "bir kısmı evolvent olmaz. Daha fazla diş veya daha büyük "
                "basınç açısı öneririm."
            )
    else:
        x = p.profile_shift
        alpha_t_r = math.radians(alpha_t)
        # alttan kesilme (§2.6.5): x_min = ht* − z·sin²αt/(2·cosβ) ölçeğinde,
        # x < 1 − z·sin²(αt)/(2·cosβ) ise undercut (x=0, β=0 → z < 2/sin²α)
        x_min = 1 - p.teeth * math.sin(alpha_t_r) ** 2 / (2 * cosb)
        if x < x_min:
            warnings.append(
                f"Alttan kesilme (undercut): bu diş sayısı için x ≥ "
                f"{x_min:.2f} gerekir. Profil kaydırmayı artır."
            )
        # diş üstü sivrilmesi (§2.6.3, denk. 2.77–2.79): sa ≥ 0.25·mn
        if d_tip > d_base:
            inv = lambda a: math.tan(a) - a
            alpha_a = math.acos(d_base / d_tip)
            sa_t = d_tip * (
                math.pi / (2 * p.teeth)
                + 2 * x * math.tan(math.radians(p.pressure_angle)) / p.teeth
                + inv(alpha_t_r) - inv(alpha_a)
            )
            sa_n = sa_t * cosb
            if sa_n <= 0:
                raise HTTPException(
                    422, "Diş üstü tamamen sivrilip yok oluyor (sa ≤ 0). "
                         "Profil kaydırmayı küçült.")
            if sa_n < 0.25 * p.module:
                warnings.append(
                    f"Diş üstü sivriliyor: sa = {sa_n:.2f} mm < 0.25·mn = "
                    f"{0.25*p.module:.2f} mm. Su verilmiş diş için sınır 0.25·mn, "
                    f"diğerleri için 0.4·mn (denk. 2.79)."
                )
    warning = " • ".join(warnings) if warnings else None

    stats = {
        "d_pitch": round(d_pitch, 3),
        "d_tip": round(d_tip, 3),
        "d_root": round(d_root, 3),
        "d_base": round(d_base, 3),
        "circular_pitch": round(math.pi * p.module, 3),
    }
    if d_outer is not None:
        stats["d_outer"] = round(d_outer, 3)
    if p.helix_angle != 0:
        stats["m_t"] = round(m_t, 4)
        stats["alpha_t"] = round(alpha_t, 3)
    if p.gear_type != "ring":
        stats["x"] = p.profile_shift
        if sa_n is not None:
            stats["s_a"] = round(sa_n, 3)

    control = None
    if p.gear_type != "ring":
        control = control_measures(p, m_t, alpha_t, cosb, d_pitch, d_base,
                                   d_tip, d_root)

    return {
        "id": gid,
        "gear_type": p.gear_type,
        "helix_angle": p.helix_angle,
        "control": control,
        "filename": nice_name,
        "mesh": {"vertices": vertices, "faces": faces},
        "stats": stats,
        "warning": warning,
    }


def generate_pair(p: GearParams):
    m_t, alpha_t, cosb, at, awt, aw, a0, k = pair_params(p)
    mn, z1, z2 = p.module, p.teeth, p.mate_teeth
    x1, x2 = p.profile_shift, p.shift2
    d1, d2 = m_t * z1, m_t * z2
    db1, db2 = d1 * math.cos(at), d2 * math.cos(at)
    da1 = d1 + 2 * mn * (1 + x1 + k)
    da2 = d2 + 2 * mn * (1 + x2 + k)

    try:
        comp = build_pair(p)
    except Exception as e:
        raise HTTPException(422, f"Geometri üretilemedi: {e}")

    verts, tris = comp.tessellate(tolerance=max(0.02, mn / 60))
    vertices = [c for v in verts for c in (v.x, v.y, v.z)]
    faces = [i for t in tris for i in t]

    gid = uuid.uuid4().hex[:12]
    step_path = STEP_DIR / f"{gid}.stp"
    cq.exporters.export(cq.Workplane("XY").newObject([comp]), str(step_path),
                        exportType="STEP")
    nice_name = f"cift_disli_m{mn:g}_z{z1}-z{z2}_b{p.width:g}.stp"
    _files[gid] = {"path": step_path, "name": nice_name}

    # kavrama oranları (§2.7.4)
    e_a = (0.5 * math.sqrt(max(da1**2 - db1**2, 0))
           + 0.5 * math.sqrt(max(da2**2 - db2**2, 0))
           - aw * math.sin(awt)) / (math.pi * m_t * math.cos(at))
    e_b = p.width * abs(math.sin(math.radians(p.helix_angle))) / (math.pi * mn)

    warnings: list[str] = []
    at_deg = math.degrees(at)
    for label, z, x in (("z1", z1, x1), ("z2", z2, x2)):
        x_min = 1 - z * math.sin(at) ** 2 / (2 * cosb)
        if x < x_min:
            warnings.append(f"{label}: alttan kesilme — x ≥ {x_min:.2f} gerekir.")
    if e_a < 1.2:
        warnings.append(
            f"Kavrama oranı düşük (εα = {e_a:.2f} < 1.2) — sürekli iletim "
            f"sınırda. Diş sayısını artır veya kaydırmaları küçült.")
    if e_a < 1.0:
        warnings.append("εα < 1: çift bu haliyle sürekli çalışamaz!")

    stats = {
        "d_pitch": round(d1, 3),
        "d_pitch2": round(d2, 3),
        "d_tip": round(da1, 3),
        "d_root": round(d1 - 2 * mn * (1.25 - x1), 3),
        "d_base": round(db1, 3),
        "circular_pitch": round(math.pi * mn, 3),
        "a_w": round(aw, 3),
        "a_0": round(a0, 3),
        "alpha_wt": round(math.degrees(awt), 3),
        "eps_alpha": round(e_a, 3),
        "eps_beta": round(e_b, 3),
        "eps_gamma": round(e_a + e_b, 3),
        "u": round(z2 / z1, 4),
        "k": round(k, 4),
        "x": x1, "x2": x2,
    }
    if p.helix_angle != 0:
        stats["m_t"] = round(m_t, 4)
        stats["alpha_t"] = round(at_deg, 3)

    return {
        "id": gid,
        "gear_type": "pair",
        "helix_angle": p.helix_angle,
        "control": None,
        "filename": nice_name,
        "mesh": {"vertices": vertices, "faces": faces},
        "stats": stats,
        "warning": " • ".join(warnings) if warnings else None,
    }


def generate_ipair(p: GearParams):
    """Pinyon + halka dişli (iç çift) — Jelaska §2.8.4, DIN işaret kuralı z2<0.

    Halka dişlide profil kaydırma uygulanmaz (x2 = 0); pinyonda x1 serbest.
    """
    mn, z1, z2 = p.module, p.teeth, p.mate_teeth
    x1 = p.profile_shift
    if z2 <= z1:
        raise HTTPException(422, f"İç çiftte halka dişli daha büyük olmalı: z2 > z1 (= {z1}).")

    m_t, alpha_t, cosb = transverse_params(p)
    at = math.radians(alpha_t)
    an = math.radians(p.pressure_angle)
    # işaretli kural: z2s = −z2 → inv αwt = inv αt + 2·x1·tanαn/(z1+z2s)
    q = _inv(at) + 2 * x1 * math.tan(an) / (z1 - z2)
    awt = _inv_solve(q)
    a0 = 0.5 * m_t * (z2 - z1)                 # büyüklük
    aw = a0 * math.cos(at) / math.cos(awt)

    d1, d2 = m_t * z1, m_t * z2
    db1, db2 = d1 * math.cos(at), d2 * math.cos(at)
    da1 = d1 + 2 * mn * (1 + x1)               # pinyon (dış)
    da2 = d2 - 2 * mn                          # halka: diş üstü merkeze doğru
    df2 = d2 + 2.5 * mn
    rim = p.rim_width if p.rim_width > 0 else 3.0 * mn
    d_outer = df2 + 2 * rim

    # ---- katı model ----
    try:
        pin = SpurGear(
            module=m_t, teeth_number=z1, width=p.width,
            pressure_angle=alpha_t, helix_angle=p.helix_angle,
            addendum_coeff=cosb * (1.0 + x1),
            dedendum_coeff=cosb * (1.25 - x1),
            backlash=-2.0 * x1 * cosb,
            bore_d=p.bore if p.bore > 0 else None,
        )
        cls = type("RingGearT", (RingGear,), {"ka": 1.0 * cosb, "kd": 1.25 * cosb})
        ring = cls(
            module=m_t, teeth_number=z2, width=p.width, rim_width=rim,
            pressure_angle=alpha_t, helix_angle=p.helix_angle,
        )
        s_pin = cq.Workplane("XY").gear(pin).val()
        s_ring = cq.Workplane("XY").gear(ring).val()
    except Exception as e:
        raise HTTPException(422, f"Geometri üretilemedi: {e}")

    # halka merkezde, diş boşluğu +x yönüne gelsin; pinyon aw kadar ötelenir
    s_ring = s_ring.rotate((0, 0, 0), (0, 0, 1), math.degrees(math.pi / z2))
    s_pin = s_pin.translate((aw, 0, 0))
    comp = cq.Compound.makeCompound([s_pin, s_ring])

    verts, tris = comp.tessellate(tolerance=max(0.02, mn / 60))
    vertices = [c for v in verts for c in (v.x, v.y, v.z)]
    faces = [i for t in tris for i in t]

    gid = uuid.uuid4().hex[:12]
    step_path = STEP_DIR / f"{gid}.stp"
    cq.exporters.export(cq.Workplane("XY").newObject([comp]), str(step_path),
                        exportType="STEP")
    nice_name = f"ic_cift_m{mn:g}_z{z1}-z{z2}_b{p.width:g}.stp"
    _files[gid] = {"path": step_path, "name": nice_name}

    # ---- kavrama oranı (iç çift): gα = ga1 − ga2 + aw·sin αwt ----
    e_a = (0.5 * math.sqrt(max(da1**2 - db1**2, 0))
           - 0.5 * math.sqrt(max(da2**2 - db2**2, 0))
           + aw * math.sin(awt)) / (math.pi * m_t * math.cos(at))
    e_b = p.width * abs(math.sin(math.radians(p.helix_angle))) / (math.pi * mn)

    warnings: list[str] = []
    x_min = 1 - z1 * math.sin(at) ** 2 / (2 * cosb)
    if x1 < x_min:
        warnings.append(f"Pinyonda alttan kesilme — x1 ≥ {x_min:.2f} gerekir.")
    if z2 - z1 < 10:
        warnings.append(
            f"Diş farkı küçük (z2−z1 = {z2-z1} < 10): diş üstü köşesi girişimi "
            f"riski (§2.9.1.3) — fark artırılmalı.")
    if da2 < db2:
        warnings.append(
            "Halka dişlinin diş üstü temel dairenin içinde — profilin bir kısmı "
            "evolvent olmaz (z2'yi büyüt).")
    if e_a < 1.2:
        warnings.append(f"Kavrama oranı düşük (εα = {e_a:.2f} < 1.2).")

    stats = {
        "d_pitch": round(d1, 3),
        "d_pitch2": round(d2, 3),
        "d_tip": round(da1, 3),
        "d_root": round(d1 - 2 * mn * (1.25 - x1), 3),
        "d_base": round(db1, 3),
        "d_outer": round(d_outer, 3),
        "circular_pitch": round(math.pi * mn, 3),
        "a_w": round(aw, 3),
        "a_0": round(a0, 3),
        "alpha_wt": round(math.degrees(awt), 3),
        "eps_alpha": round(e_a, 3),
        "eps_beta": round(e_b, 3),
        "eps_gamma": round(e_a + e_b, 3),
        "u": round(z2 / z1, 4),
        "k": 0,
        "x": x1, "x2": 0,
    }
    if p.helix_angle != 0:
        stats["m_t"] = round(m_t, 4)
        stats["alpha_t"] = round(alpha_t, 3)

    return {
        "id": gid,
        "gear_type": "ipair",
        "helix_angle": p.helix_angle,
        "control": None,
        "filename": nice_name,
        "mesh": {"vertices": vertices, "faces": faces},
        "stats": stats,
        "warning": " • ".join(warnings) if warnings else None,
    }


@app.get("/api/download/{gid}")
def download(gid: str):
    info = _files.get(gid)
    if not info or not info["path"].exists():
        raise HTTPException(404, "Dosya bulunamadı — dişliyi yeniden üret.")
    return FileResponse(info["path"], filename=info["name"],
                        media_type="application/step")


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

if __name__ == "__main__":
    url = "http://127.0.0.1:8000"
    print(f"Dişli Stüdyosu → {url}")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=8000)
    except OSError:
        # sunucu zaten çalışıyor → sadece tarayıcıyı aç
        webbrowser.open(url)
