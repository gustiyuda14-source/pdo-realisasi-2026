"""
pdo_update.py — Pengendali Digital ON, Smart Wizard Update Mingguan

Recall sederhana: drop PDF SPJ Fungsional ke folder, jalankan:
    py pdo_update.py "Fungsional Per <tgl>_<bln>_<thn>.pdf"

Script akan:
  1. Auto-detect baseline HTML terbaru (by CURR_DATE)
  2. Baca PDF + deteksi bulan transition
  3. Susun RAW_DATA pakai logic rolling snapshot
  4. Generate file HTML + diff report markdown
  5. Optional: push ke GitHub Pages (`gh` CLI sudah login)

Aturan utama (lihat CLAUDE.md):
  - JANGAN tebak kode rekening (ambil dari PDF)
  - Format 6-segmen wajib
  - Bulan transition: c10 di-rollover otomatis dari PDF Kol.10

Flags:
  --baseline <path>     Override auto-detect baseline
  --output <path>       Override nama file output
  --no-deploy           Skip push ke GitHub Pages
  --dry-run             Generate ke memory saja, tidak tulis file
  --c11p-rolling        Pakai literal rolling (c11p=c11n_lama) bukan rebase 0
  --repo <name>         Override nama repo GitHub Pages
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import io
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Force UTF-8 stdout (Windows console)
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ─── Constants ──────────────────────────────────────────────
PAGU_TOTAL = 24335335344
PROJ = Path(__file__).resolve().parent
DEFAULT_REPO = "pdo-realisasi-2026"

BULAN_FULL_TO_ABBR = {
    "Januari": "Jan", "Februari": "Feb", "Maret": "Mar", "April": "Apr",
    "Mei": "Mei", "Juni": "Jun", "Juli": "Jul", "Agustus": "Agu",
    "September": "Sep", "Oktober": "Okt", "November": "Nov", "Desember": "Des",
}
BULAN_ABBR_TO_NUM = {abbr: i+1 for i, abbr in enumerate(["Jan","Feb","Mar","Apr","Mei","Jun","Jul","Agu","Sep","Okt","Nov","Des"])}
BULAN_FULL_TO_NUM = {full: i+1 for i, full in enumerate(["Januari","Februari","Maret","April","Mei","Juni","Juli","Agustus","September","Oktober","November","Desember"])}

REK6 = re.compile(r"^\d+\.\d+\.\d+\.\d+\.\d+\.\d+$")
KEG  = re.compile(r"^6\.\d{2}\.\d{2}\.\d\.\d{2}\.\d{4}$")
SUB  = re.compile(r"^6\.\d{2}\.\d{2}\.\d\.\d{2}$")


# ─── Helpers ────────────────────────────────────────────────
def parse_money(s: str) -> int:
    """Parse Rp format Indonesia: 'Rp1.234.567,00' → 1234567."""
    if not s or str(s).strip() in ("-", "", "None", "Rp0,00"):
        return 0
    s = re.sub(r"^Rp\s*", "", str(s).strip())
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    else:
        s = s.replace('.', '')
    try:
        return int(round(float(s)))
    except ValueError:
        return 0


def parse_date(s: str):
    """Parse date '22 Mei 2026' → (day, month_num, year, month_abbr)."""
    m = re.match(r"(\d+)\s+(\w+)\s+(\d{4})", s.strip())
    if not m:
        return None
    day = int(m.group(1))
    bln = m.group(2)
    year = int(m.group(3))
    num = BULAN_ABBR_TO_NUM.get(bln) or BULAN_FULL_TO_NUM.get(bln)
    return day, num, year, bln


def fmt_rp(n: int) -> str:
    """Format integer to Indonesian Rp string (no Rp prefix)."""
    if not n:
        return "0"
    return f"{n:,}".replace(",", ".")


def js_str(s: str) -> str:
    """Format Python string to safe JS single-quoted string body."""
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace("\\", "\\\\").replace("'", "\\'")


def run(cmd: list[str], cwd=None, check=True, capture=True) -> subprocess.CompletedProcess:
    """Run a subprocess, return CompletedProcess. Raises on check=True failure."""
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=capture, text=True, encoding="utf-8")


def ask(prompt: str, default: str = "") -> str:
    """Interactive prompt with optional default."""
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def confirm(prompt: str, default: bool = True) -> bool:
    """Yes/no confirmation."""
    hint = "Y/n" if default else "y/N"
    val = input(f"{prompt} [{hint}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "ya")


def banner(title: str):
    print(f"\n{'─' * 64}")
    print(f"  {title}")
    print(f"{'─' * 64}")


# ─── PDF Extraction ─────────────────────────────────────────
def extract_pdf(pdf_path: Path) -> dict:
    """Extract sub/item/rek from PDF SPJ Fungsional. Returns dict + meta (bulan)."""
    import pdfplumber

    sub_data: dict = {}
    item_data: dict = {}
    rek_data: list = []
    current_item: str | None = None
    bulan_pdf: str | None = None

    with pdfplumber.open(pdf_path) as pdf:
        # Halaman 1: deteksi "Bulan : <X>"
        first_text = pdf.pages[0].extract_text() or ""
        m = re.search(r"Bulan\s*:\s*(\w+)", first_text)
        if m:
            bulan_pdf = m.group(1)

        for pi, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables() or []:
                for r in table:
                    if not r or not r[0]:
                        continue
                    kode = (r[0] or "").strip()
                    row = [(c or "").strip() for c in r]
                    if len(row) != 14:
                        continue
                    rec = dict(
                        kode=kode, nama=row[1], page=pi,
                        pagu=parse_money(row[2]),
                        ls_gaji_lalu=parse_money(row[3]),
                        ls_gaji_kini=parse_money(row[4]),
                        ls_bj_lalu=parse_money(row[6]),
                        ls_bj_kini=parse_money(row[7]),
                        c10=parse_money(row[9]),
                        c11=parse_money(row[10]),
                        total=parse_money(row[12]),
                        sisa=parse_money(row[13]),
                    )
                    if SUB.match(kode):
                        sub_data[kode] = rec
                    elif KEG.match(kode):
                        item_data[kode] = rec
                        current_item = kode
                    elif REK6.match(kode):
                        rec["parent_item"] = current_item
                        rek_data.append(rec)

    return dict(sub=sub_data, item=item_data, rek=rek_data, bulan_pdf=bulan_pdf)


# ─── Baseline Parsing ───────────────────────────────────────
def split_balanced(text: str) -> list[str]:
    """Walk top-level brace-balanced objects in `text`."""
    objs, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i] in " \n\t,":
            i += 1
        if i >= n:
            break
        if text[i] != "{":
            i += 1
            continue
        depth, start = 0, i
        in_str, str_ch = False, ""
        while i < n:
            ch = text[i]
            if in_str:
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if ch == str_ch:
                    in_str = False
            else:
                if ch in ("'", '"'):
                    in_str = True
                    str_ch = ch
                elif ch in ("{", "["):
                    depth += 1
                elif ch in ("}", "]"):
                    depth -= 1
                    if depth == 0 and ch == "}":
                        i += 1
                        break
            i += 1
        objs.append(text[start:i])
    return objs


def parse_baseline(html_path: Path) -> dict:
    """Parse baseline HTML, return constants + ordered RAW_DATA + dicts."""
    html = html_path.read_text(encoding="utf-8")
    prev = re.search(r"const PREV_DATE\s*=\s*'([^']+)'", html).group(1)
    curr = re.search(r"const CURR_DATE\s*=\s*'([^']+)'", html).group(1)

    mblock = re.search(r"const RAW_DATA = \[(.*?)\];", html, re.DOTALL)
    if not mblock:
        raise ValueError(f"RAW_DATA tidak ditemukan di {html_path}")
    raw_objs = split_balanced(mblock.group(1))

    nodes = []
    det_re = re.compile(
        r"\{k:'([\d\.]+)',n:'((?:[^'\\]|\\.)*)',p:(\d+),c10:(\d+),c11p:(\d+),"
        r"c11n:(\d+),total:(\d+),sisa:(\d+),delta:(\d+)\}"
    )

    for obj in raw_objs:
        tm = re.match(r"\{t:'(prog|subkeg|item)'", obj)
        if not tm:
            continue
        typ = tm.group(1)
        kode = re.search(r"k:'([\d\.]+)'", obj).group(1)
        nm = re.search(r"n:'((?:[^'\\]|\\.)*)'", obj)
        nama = nm.group(1) if nm else ""
        pagu = int(re.search(r",p:(\d+)", obj).group(1))
        m_old = int(re.search(r",m:(\d+)", obj).group(1))
        f_old = int(re.search(r",f:(\d+)", obj).group(1))
        pgm = re.search(r",pg:'([\d\.]+)'", obj)
        skm = re.search(r",sk:'([\d\.]+)'", obj)
        pg = pgm.group(1) if pgm else None
        sk = skm.group(1) if skm else None
        dets = []
        dm = re.search(r"details:\s*\[(.*)\]\s*\}", obj, re.DOTALL)
        if dm:
            for d in det_re.finditer(dm.group(1)):
                dets.append(dict(
                    k=d.group(1), n=d.group(2), p=int(d.group(3)),
                    c10=int(d.group(4)), c11p=int(d.group(5)), c11n=int(d.group(6)),
                    total=int(d.group(7)), sisa=int(d.group(8)), delta=int(d.group(9)),
                ))
        nodes.append(dict(type=typ, kode=kode, nama=nama, p=pagu,
                          m_old=m_old, f_old=f_old, pg=pg, sk=sk, details_old=dets))

    return dict(html=html, prev_date=prev, curr_date=curr, nodes=nodes)


def auto_detect_baseline(folder: Path) -> Path | None:
    """Pilih file *.html terbaru by CURR_DATE."""
    candidates = []
    for f in folder.glob("pengendali_digital_on_*.html"):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"const CURR_DATE\s*=\s*'([^']+)'", text)
            if not m:
                continue
            d = parse_date(m.group(1))
            if not d:
                continue
            day, mon, year, _ = d
            candidates.append((datetime(year, mon, day), f))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


# ─── Plan Builder ───────────────────────────────────────────
def build_plan(baseline: dict, pdf_data: dict, c11p_mode: str = "rebase") -> dict:
    """Build new RAW_DATA plan.

    c11p_mode:
      'rebase'  → c11p = 0 saat bulan transition (DEFAULT, semantik bulan baru)
      'rolling' → c11p = c11n_lama selalu (rolling literal)
    """
    sub_pdf, item_pdf, rek_pdf = pdf_data["sub"], pdf_data["item"], pdf_data["rek"]
    rek_by_parent = defaultdict(list)
    for r in rek_pdf:
        rek_by_parent[r["parent_item"]].append(r)

    # Detect bulan transition
    prev_d = parse_date(baseline["curr_date"])
    pdf_bulan_full = pdf_data.get("bulan_pdf")
    pdf_bulan_num = BULAN_FULL_TO_NUM.get(pdf_bulan_full, prev_d[1] if prev_d else None)
    bulan_transition = bool(prev_d and pdf_bulan_num and pdf_bulan_num != prev_d[1])

    item_new = {}
    for nd in baseline["nodes"]:
        if nd["type"] != "item":
            continue
        k = nd["kode"]
        p_pdf = item_pdf.get(k)
        m_new = p_pdf["total"] if p_pdf else nd["m_old"]

        pdf_rek = rek_by_parent.get(k, [])
        old_dets = nd["details_old"]
        pdf_by_k: dict[str, list] = {}
        for r in pdf_rek:
            pdf_by_k.setdefault(r["kode"], []).append(r)

        new_dets = []
        used = set()

        def compute_det(rk: str, rows: list, fallback_pagu: int, fallback_old: dict | None):
            """Compute c10/c11p/c11n/total/delta untuk satu rekening detail.

            Prinsip TOTAL-BASED (berlaku universal ke SEMUA section: LS-Gaji /
            LS-Barang&Jasa / UP-GU-TU):

              `total` (kumulatif semua jalur realisasi = Kol.13 PDF) adalah
              ground-truth dan SELALU benar. Delta mingguan diturunkan dari selisih
              total antar-snapshot (total_baru − total_lama), BUKAN dari kolom
              'bulan ini' PDF. Alasannya: SIPD me-reset kolom 'bulan ini' ke 0 saat
              transisi bulan (terbukti pada LS Gaji: Kol.5 → 0, seluruh realisasi
              didorong ke Kol.4) sehingga realisasi riil "hilang" bila kita baca
              kolom mentah. Pendekatan total-based ini IDENTIK hasilnya dengan
              kolom-based di minggu normal, tapi kebal terhadap anomali reset kolom
              di section manapun. Selisih yang "hilang" itu justru direkonstruksi
              sebagai realisasi bulan berjalan — nilai yang seharusnya SIPD sediakan.
            """
            c10_sum = sum(r["c10"] + r["ls_gaji_lalu"] + r["ls_bj_lalu"] for r in rows)
            c11_sum = sum(r["c11"] + r["ls_gaji_kini"] + r["ls_bj_kini"] for r in rows)
            p_sum = sum(r["pagu"] for r in rows)
            p_use = p_sum if p_sum > 0 else fallback_pagu
            total_d = c10_sum + c11_sum        # kumulatif = Kol.13, selalu benar
            sisa_d = p_use - total_d
            nama = rows[0]["nama"] if rows else (fallback_old["n"] if fallback_old else rk)

            if bulan_transition and fallback_old is not None:
                # Bulan baru: anchor c10 = total snapshot lalu (BUKAN Kol.10 PDF mentah,
                # yang saat transisi bisa tercampur realisasi telat bulan lalu). Selisih
                # total masuk sebagai realisasi bulan baru (c11n); c11p mulai dari 0.
                c10_val  = fallback_old["total"]
                c11p_val = 0
                c11n_val = total_d - c10_val
            elif fallback_old is not None:
                # Bulan sama: bawa anchor c10 dari baseline (tetap sepanjang bulan),
                # c11n = akumulasi bulan ini s.d. sekarang = total − anchor,
                # c11p = c11n minggu lalu. (rebase mode: c11p paksa 0 → delta = c11n penuh)
                c10_val  = fallback_old["c10"]
                c11n_val = total_d - c10_val
                c11p_val = 0 if c11p_mode == "rebase" else fallback_old["c11n"]
            else:
                # Rekening baru tanpa histori: percaya kolom PDF apa adanya.
                c10_val  = c10_sum
                c11p_val = 0
                c11n_val = c11_sum

            delta = c11n_val - c11p_val
            return dict(k=rk, n=nama, p=p_use, c10=c10_val, c11p=c11p_val,
                        c11n=c11n_val, total=total_d, sisa=sisa_d, delta=delta)

        # 1. Iterate gu5 details first (preserve order)
        for od in old_dets:
            rk = od["k"]
            used.add(rk)
            rows = pdf_by_k.get(rk, [])
            if not rows:
                # rekening missing dari PDF — rollover c10
                c10_roll = od["c10"] + od["c11n"]
                new_dets.append(dict(
                    k=rk, n=od["n"], p=od["p"],
                    c10=c10_roll, c11p=0, c11n=0,
                    total=c10_roll, sisa=od["p"] - c10_roll, delta=0,
                ))
            else:
                new_dets.append(compute_det(rk, rows, od["p"], od))

        # 2. Append rekening baru di PDF
        for rk, rows in pdf_by_k.items():
            if rk in used:
                continue
            new_dets.append(compute_det(rk, rows, 0, None))

        item_new[k] = dict(
            kode=k, nama=nd["nama"], p=nd["p"], sk=nd["sk"],
            m_new=m_new, f_new=nd["m_old"], details=new_dets,
        )

    # Roll up sub-keg
    subkeg_new = {}
    for nd in baseline["nodes"]:
        if nd["type"] != "subkeg":
            continue
        k = nd["kode"]
        items_under = [it for it in item_new.values() if it["sk"] == k]
        m_sum = sum(it["m_new"] for it in items_under)
        subkeg_new[k] = dict(kode=k, nama=nd["nama"], p=nd["p"], pg=nd["pg"],
                             m_new=m_sum, f_new=nd["m_old"])

    # Roll up prog
    prog_new = {}
    for nd in baseline["nodes"]:
        if nd["type"] != "prog":
            continue
        k = nd["kode"]
        subs_under = [s for s in subkeg_new.values() if s["pg"] == k]
        m_sum = sum(s["m_new"] for s in subs_under)
        prog_new[k] = dict(kode=k, nama=nd["nama"], p=nd["p"], m_new=m_sum, f_new=nd["m_old"])

    return dict(prog=prog_new, subkeg=subkeg_new, item=item_new,
                bulan_transition=bulan_transition,
                pdf_bulan=pdf_bulan_full)


# ─── HTML Formatter & Template ──────────────────────────────
INDENT = {"prog": "  ", "subkeg": "    ", "item": "      "}


def format_raw_data(baseline: dict, plan: dict) -> str:
    """Format RAW_DATA JS literal preserving baseline order."""
    lines = []
    prev_type = None
    for nd in baseline["nodes"]:
        typ, kode = nd["type"], nd["kode"]
        if typ == "prog":
            if prev_type is not None:
                lines.append("")
            p = plan["prog"][kode]
            lines.append(
                f"{INDENT['prog']}{{t:'prog',k:'{kode}',n:'{js_str(p['nama'])}',"
                f"p:{p['p']},m:{p['m_new']},f:{p['f_new']}}},"
            )
        elif typ == "subkeg":
            s = plan["subkeg"][kode]
            lines.append(
                f"{INDENT['subkeg']}{{t:'subkeg',k:'{kode}',n:'{js_str(s['nama'])}',"
                f"p:{s['p']},m:{s['m_new']},f:{s['f_new']},pg:'{s['pg']}'}},"
            )
        elif typ == "item":
            it = plan["item"][kode]
            base = (
                f"{INDENT['item']}{{t:'item',k:'{kode}',n:'{js_str(it['nama'])}',"
                f"p:{it['p']},m:{it['m_new']},f:{it['f_new']},sk:'{it['sk']}'"
            )
            if not it["details"]:
                lines.append(base + "},")
            else:
                lines.append(base + ",")
                lines.append(f"{INDENT['item']}  details:[")
                for j, d in enumerate(it["details"]):
                    sep = "," if j < len(it["details"]) - 1 else ""
                    lines.append(
                        f"{INDENT['item']}    {{k:'{d['k']}',n:'{js_str(d['n'])}',p:{d['p']},"
                        f"c10:{d['c10']},c11p:{d['c11p']},c11n:{d['c11n']},"
                        f"total:{d['total']},sisa:{d['sisa']},delta:{d['delta']}}}{sep}"
                    )
                lines.append(f"{INDENT['item']}  ]}},")
        prev_type = typ
    return "\n" + "\n".join(lines) + "\n"


def apply_template(baseline: dict, plan: dict, new_curr: str) -> str:
    """Apply RAW_DATA + label tanggal updates ke baseline HTML.

    Konvensi label:
      - Konstanta PREV_DATE → baseline.curr_date (snapshot lama jadi 'lalu')
      - Konstanta CURR_DATE → new_curr ('22 Mei 2026')
      - Semua label hardcoded tanggal di-update via regex generic
    """
    html = baseline["html"]

    # Replace RAW_DATA block
    new_block = format_raw_data(baseline, plan)
    html = re.sub(
        r"const RAW_DATA = \[(.*?)\];",
        lambda m: "const RAW_DATA = [" + new_block + "];",
        html, count=1, flags=re.DOTALL,
    )

    # Replace constants
    html = re.sub(r"const PREV_DATE\s*=\s*'[^']*'",
                  f"const PREV_DATE  = '{baseline['curr_date']}'", html, count=1)
    html = re.sub(r"const CURR_DATE\s*=\s*'[^']*'",
                  f"const CURR_DATE  = '{new_curr}'", html, count=1)

    # Parse dates
    prev_d = parse_date(baseline["curr_date"])     # snapshot lama
    new_d  = parse_date(new_curr)                  # snapshot baru
    if not prev_d or not new_d:
        return html

    prev_full_bulan = next(k for k,v in BULAN_FULL_TO_NUM.items() if v == prev_d[1])
    new_full_bulan  = next(k for k,v in BULAN_FULL_TO_NUM.items() if v == new_d[1])
    prev_abbr = prev_d[3]
    new_abbr = new_d[3]

    # Generic label replacements — common patterns from gu5/sebelumnya
    # Format: literal string → replacement
    label_subs = [
        # Header H2
        (re.compile(r"Perbandingan Mingguan \w+ \d{4}"),
         f"Perbandingan Mingguan {new_full_bulan} {new_d[2]}"),
        # Update <bulan tgl>
        (re.compile(r"Update \d+ \w+ \d{4}"),
         f"Update {new_d[0]} {new_full_bulan} {new_d[2]}"),
        # "Minggu Lalu → <baseline_date> (Final)?"
        (re.compile(r"Minggu Lalu(?:\s*\([^)]*\))?\s*→\s*\d+ \w+ \d{4}(?:\s*\(Final\))?"),
         f"Minggu Lalu ({prev_d[0]} {prev_abbr}) → {new_d[0]} {new_full_bulan} {new_d[2]}"),
        # card-title "vs <date>"
        (re.compile(r"Minggu Lalu vs \d+ \w+ \d{4}"),
         f"Minggu Lalu vs {new_d[0]} {new_full_bulan} {new_d[2]}"),
        # tbl h3
        (re.compile(r"Rekapitulasi \d+ Items — Minggu Lalu vs \d+ \w+ \d{4}"),
         lambda m: re.sub(r"Minggu Lalu vs \d+ \w+ \d{4}",
                         f"Minggu Lalu vs {new_d[0]} {new_full_bulan} {new_d[2]}", m.group(0))),
        # th tabel "<date> (Rp)"
        (re.compile(r">(\d+)\s+(?:Apr|Mei|Jun|Jul|Agu|Sep|Okt|Nov|Des|Jan|Feb|Mar)\s+(\d{4})\s+\(Rp\)<"),
         f">{new_d[0]} {new_abbr} {new_d[2]} (Rp)<"),
        # cmp-lbl "<date>"
        (re.compile(r'<div class="cmp-lbl">\d+\s+\w+\s+\d{4}</div>'),
         f'<div class="cmp-lbl">{new_d[0]} {new_abbr} {new_d[2]}</div>'),
        # KPI hardcoded date (kpi c-green / kpi-lbl)
        (re.compile(r'<div class="kpi c-green"><div class="kpi-lbl">\d+\s+\w+\s+\d{4}</div>'),
         f'<div class="kpi c-green"><div class="kpi-lbl">{new_d[0]} {new_full_bulan} {new_d[2]}</div>'),
        # selisihBanner sel-lbl
        (re.compile(r'<div class="sel-item"><div class="sel-lbl">\d+\s+\w+\s+\d{4}</div>'),
         f'<div class="sel-item"><div class="sel-lbl">{new_d[0]} {new_full_bulan} {new_d[2]}</div>'),
        # buildProgRealisasi labels — "<num? abbr|full>:" + ${pM
        (re.compile(r'<span style="color:\$\{bc\}">(?:\d+\s+)?[A-Z][a-z]+:?\s*\$\{pM\.toFixed\(1\)\}%</span>'),
         f'<span style="color:${{bc}}">{new_d[0]} {new_abbr}: ${{pM.toFixed(1)}}%</span>'),
        # buildProgRealisasi small label "<bulan|abbr>" before bar
        (re.compile(r'width:44px">(?:\d+\s+)?[A-Z][a-z]+</div>'),
         f'width:44px">{new_d[0]} {new_abbr}</div>'),
        # Modal compact box "<date>" inside KPI grid card
        (re.compile(r'>(\d+\s+(?:Apr|Mei|Jun|Jul|Agu|Sep|Okt|Nov|Des|Jan|Feb|Mar))\s+(\d{4})</div>'),
         lambda m: f'>{new_d[0]} {new_abbr} {new_d[2]}</div>' if int(m.group(2)) == new_d[2] else m.group(0)),
        # <title>
        (re.compile(r"<title>Pengendali Digital ON[^<]*</title>"),
         f"<title>Pengendali Digital ON — {new_full_bulan} {new_d[2]} · Perbandingan Mingguan per {new_d[0]} {new_abbr}</title>"),
    ]

    for pat, repl in label_subs:
        if callable(repl):
            html = pat.sub(repl, html)
        else:
            html = pat.sub(repl, html)

    # Modal label: "Realisasi Bulan Lalu (s.d. <bulan> · Kol.10 SPJ)" — adjust
    # Hitung bulan sebelumnya (Maret untuk April-snapshot baseline, April untuk Mei-snapshot baseline, dst)
    prev_prev_num = prev_d[1] - 1 if prev_d[1] > 1 else 12
    prev_prev_full = next(k for k,v in BULAN_FULL_TO_NUM.items() if v == prev_prev_num)
    # Bulan c10 reference (s.d. bulan sebelum CURR_DATE baru)
    c10_bulan_num = new_d[1] - 1 if new_d[1] > 1 else 12
    c10_bulan_full = next(k for k,v in BULAN_FULL_TO_NUM.items() if v == c10_bulan_num)

    # Update modal label lama → label akurat untuk bulan baru
    html = re.sub(
        r"Realisasi Bulan Lalu <small style=\"color:#64748B\">\(s\.d\. \w+ · Kol\.10 SPJ\)</small>",
        f"Realisasi s.d. {c10_bulan_full} <small style=\"color:#64748B\">(sebelum {new_full_bulan} · Kol.10 SPJ)</small>",
        html,
    )
    html = re.sub(
        r"Realisasi (?:Bulan Lalu|s\.d\. \w+) <small style=\"color:#64748B\">\(s\.d\. \w+[^)]*\)</small>",
        f"Realisasi s.d. {c10_bulan_full} <small style=\"color:#64748B\">(sebelum {new_full_bulan} · Kol.10 SPJ)</small>",
        html,
    )
    html = re.sub(
        r"Realisasi Minggu Lalu <small style=\"color:#64748B\">\(\$\{PREV_DATE\} · Kol\.11 SPJ lalu\)</small>",
        f"Realisasi Awal {new_full_bulan} <small style=\"color:#64748B\">(per ${{PREV_DATE}} · belum ada SPJ {new_full_bulan})</small>",
        html,
    )
    html = re.sub(
        r"Realisasi per \$\{CURR_DATE\} <small style=\"color:#64748B\">\(Kol\.11 SPJ ini\)</small>",
        f"Realisasi per ${{CURR_DATE}} <small style=\"color:#64748B\">(realisasi {new_full_bulan} · Kol.11 SPJ ini)</small>",
        html,
    )
    html = re.sub(
        r"Total Realisasi Saat Ini <small style=\"color:#64748B\">\(Kol\.10 \+ Kol\.11 baru\)</small>",
        f"Total Realisasi Saat Ini <small style=\"color:#64748B\">(s.d. {c10_bulan_full} + {new_full_bulan} · Kol.13 SPJ)</small>",
        html,
    )

    # Inject conditional note (if not already there)
    note_marker = "ℹ️ Catatan"
    if note_marker not in html:
        note_block = """    <div style=\"font-size:10px;font-weight:700;color:#64748B;text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px\">
      📋 Detail Rincian Rekening SPJ (${(it.details||[]).length} rekening)
    </div>`;

  const itemNaik = (it.m - it.f) > 0;
  const anyDetNaik = (it.details||[]).some(x=>x.delta>0);
  if(itemNaik && !anyDetNaik){
    h+=`<div style=\"background:rgba(251,191,36,0.10);border:1px solid rgba(251,191,36,0.30);border-left:3px solid #FBBF24;border-radius:8px;padding:9px 12px;margin-bottom:10px;font-size:10.5px;color:#FBBF24;line-height:1.45\">
      <strong style=\"display:block;margin-bottom:2px;font-weight:600\">ℹ️ Catatan</strong>
      <span style=\"color:#E2E8F0\">Kenaikan <strong style=\"color:#A78BFA\">+Rp ${rp(it.m-it.f)}</strong> bersifat <em>penyesuaian pencatatan periode s.d. PREVMONTH</em> — belum ada realisasi tercatat di NEWMONTH untuk rekening item ini.</span>
    </div>`;
  }

  (it.details||[]).forEach(det=>{"""
        note_block = note_block.replace("PREVMONTH", c10_bulan_full).replace("NEWMONTH", new_full_bulan)
        html = html.replace(
            """    <div style=\"font-size:10px;font-weight:700;color:#64748B;text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px\">
      📋 Detail Rincian Rekening SPJ (${(it.details||[]).length} rekening)
    </div>`;\n\n  (it.details||[]).forEach(det=>{""",
            note_block,
        )
    else:
        # Update PREVMONTH/NEWMONTH inside existing note
        html = re.sub(
            r"penyesuaian pencatatan periode s\.d\. \w+",
            f"penyesuaian pencatatan periode s.d. {c10_bulan_full}",
            html,
        )
        html = re.sub(
            r"belum ada realisasi tercatat di \w+",
            f"belum ada realisasi tercatat di {new_full_bulan}",
            html,
        )

    return html


# ─── Verify ─────────────────────────────────────────────────
def verify(html: str, expected_total: int | None = None) -> dict:
    """Cross-check konsistensi output."""
    prog_re = re.compile(r"\{t:'prog',k:'([\d\.]+)',n:'((?:[^'\\]|\\.)*)',p:(\d+),m:(\d+),f:(\d+)")
    sub_re  = re.compile(r"\{t:'subkeg',k:'([\d\.]+)',n:'((?:[^'\\]|\\.)*)',p:(\d+),m:(\d+),f:(\d+),pg:'([\d\.]+)'")
    item_re = re.compile(r"\{t:'item',k:'([\d\.]+)',n:'((?:[^'\\]|\\.)*)',p:(\d+),m:(\d+),f:(\d+),sk:'([\d\.]+)'")
    det_re  = re.compile(r"\{k:'([\d\.]+)',n:'((?:[^'\\]|\\.)*)',p:(\d+),c10:(\d+),c11p:(\d+),c11n:(\d+),total:(\d+),sisa:(\d+),delta:(\d+)\}")

    progs = {m.group(1): dict(p=int(m.group(3)), m=int(m.group(4)), f=int(m.group(5))) for m in prog_re.finditer(html)}
    subs  = {m.group(1): dict(p=int(m.group(3)), m=int(m.group(4)), f=int(m.group(5)), pg=m.group(6)) for m in sub_re.finditer(html)}
    items = {m.group(1): dict(p=int(m.group(3)), m=int(m.group(4)), f=int(m.group(5)), sk=m.group(6), n=m.group(2)) for m in item_re.finditer(html)}

    e1 = sum(1 for pk, p in progs.items() if sum(s["m"] for s in subs.values() if s["pg"] == pk) != p["m"])
    e2 = sum(1 for sk, s in subs.items() if sum(it["m"] for it in items.values() if it["sk"] == sk) != s["m"])

    # CEK 3: delta details consistency
    mblock = re.search(r"const RAW_DATA = \[(.*?)\];", html, re.DOTALL)
    raw_objs = split_balanced(mblock.group(1))
    e3 = 0
    items_naik_det = 0
    # Tidak ada lagi bypass GAJI: sejak delta total-based, item.δ = Σdet.δ juga
    # konsisten untuk rekening gaji (5.1.01.*). Cek e3 kini berlaku ke semua item.
    for obj in raw_objs:
        tm = re.match(r"\{t:'item',k:'([\d\.]+)'", obj)
        if not tm:
            continue
        k = tm.group(1)
        sum_delta = sum(int(d.group(9)) for d in det_re.finditer(obj))
        if sum_delta > 0:
            items_naik_det += 1
        item_delta = items[k]["m"] - items[k]["f"]
        if abs(item_delta - sum_delta) > 1:
            e3 += 1

    tm_total = sum(p["m"] for p in progs.values())
    tf_total = sum(p["f"] for p in progs.values())
    e4_match = (expected_total is None) or (tm_total == expected_total)

    return dict(
        progs=progs, subs=subs, items=items,
        e1=e1, e2=e2, e3=e3, e4_match=e4_match,
        total_m=tm_total, total_f=tf_total,
        items_naik_det=items_naik_det,
    )


# ─── Diff Report ────────────────────────────────────────────
def build_diff_report(baseline: dict, plan: dict, ver: dict, new_curr: str, pdf_total: int) -> str:
    """Generate markdown diff report."""
    out = []
    out.append(f"# Update Mingguan PDO — {new_curr}\n")
    out.append(f"**Snapshot sebelumnya:** {baseline['curr_date']}  ")
    out.append(f"**Total Pagu:** Rp {fmt_rp(PAGU_TOTAL)}  ")
    out.append(f"**Realisasi {baseline['curr_date']}:** Rp {fmt_rp(ver['total_f'])} ({ver['total_f']/PAGU_TOTAL*100:.2f}% pagu)  ")
    out.append(f"**Realisasi {new_curr}:** Rp {fmt_rp(ver['total_m'])} ({ver['total_m']/PAGU_TOTAL*100:.2f}% pagu)  ")
    out.append(f"**Kenaikan 7 hari:** +Rp {fmt_rp(ver['total_m'] - ver['total_f'])}\n")

    if plan["bulan_transition"]:
        out.append(f"> 🔄 **Bulan transition terdeteksi** (PDF Bulan = {plan['pdf_bulan']}). c10 di-rollover otomatis.\n")

    # Cross-check
    cek_status = "✅ MATCH" if ver["e4_match"] else f"❌ MISMATCH (selisih {ver['total_m']-pdf_total:+,})"
    e1, e2, e3 = ver["e1"], ver["e2"], ver["e3"]
    s1 = "✅ OK" if e1 == 0 else f"❌ {e1} mismatch"
    s2 = "✅ OK" if e2 == 0 else f"❌ {e2} mismatch"
    s3 = "✅ OK" if e3 == 0 else f"❌ {e3} mismatch"
    out.append("\n## Cross-check\n\n| Cek | Hasil |\n|-----|-------|\n")
    out.append(f"| 1. prog.m = Σsub.m | {s1} |\n")
    out.append(f"| 2. sub.m = Σitem.m | {s2} |\n")
    out.append(f"| 3. item.δ = Σdet.δ (non-gaji) | {s3} |\n")
    out.append(f"| 4. Total m = PDF Kol.13 | {cek_status} |\n")

    # Sub-keg naik
    out.append("\n## Sub-Kegiatan dengan Kenaikan\n\n")
    out.append("| Sub-Keg | Δ (Rp) | % Pagu | Uraian |\n|---|---:|---:|---|\n")
    naik_subs = [(k, s) for k, s in ver["subs"].items() if s["m"] - s["f"] > 0]
    naik_subs.sort(key=lambda x: -(x[1]["m"] - x[1]["f"]))
    for k, s in naik_subs:
        d = s["m"] - s["f"]
        pc = d / s["p"] * 100 if s["p"] else 0
        nm = plan["subkeg"][k]["nama"][:50]
        out.append(f"| `{k}` | +{fmt_rp(d)} | {pc:.2f}% | {nm} |\n")

    # Top 10 item naik
    out.append("\n## Top 10 Item dengan Kenaikan\n\n")
    out.append("| Kode Item | Δ (Rp) | Uraian |\n|---|---:|---|\n")
    naik_items = [(k, it, it["m"] - it["f"]) for k, it in ver["items"].items() if it["m"] - it["f"] > 0]
    naik_items.sort(key=lambda x: -x[2])
    for k, it, d in naik_items[:10]:
        out.append(f"| `{k}` | +{fmt_rp(d)} | {it['n'][:55]} |\n")

    # Rekening baru
    out.append("\n## Rekening Baru (di PDF, belum di baseline)\n\n")
    baseline_rek = set()
    for nd in baseline["nodes"]:
        if nd["type"] == "item":
            for d in nd["details_old"]:
                baseline_rek.add((nd["kode"], d["k"]))
    new_rek = []
    for k, it in plan["item"].items():
        for d in it["details"]:
            if (k, d["k"]) not in baseline_rek:
                new_rek.append((k, d, it["nama"]))
    if new_rek:
        out.append("| Parent Item | Kode Rekening | c11n (Rp) | Uraian |\n|---|---|---:|---|\n")
        for parent_k, d, parent_nama in new_rek:
            out.append(f"| `{parent_k}` | `{d['k']}` | {fmt_rp(d['c11n'])} | {d['n'][:50]} |\n")
    else:
        out.append("_Tidak ada rekening baru._\n")

    # Anomali
    out.append("\n## Anomali\n\n")
    anomali = []
    for k, it in plan["item"].items():
        item_delta = it["m_new"] - it["f_new"]
        sum_d = sum(d["delta"] for d in it["details"])
        if item_delta > 0 and sum_d == 0:
            anomali.append((k, it["nama"], item_delta))
    if anomali:
        out.append("Item naik tapi semua detail delta = 0 (penyesuaian pencatatan periode lalu):\n\n")
        for k, nama, d in anomali:
            out.append(f"- `{k}` — +Rp {fmt_rp(d)} — {nama[:60]}\n")
    else:
        out.append("_Tidak ada anomali._\n")

    out.append(f"\n---\n*Generated by `pdo_update.py` · {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")
    return "".join(out)


# ─── GitHub Pages Deploy ────────────────────────────────────
def render_archive_index(snapshots: list[dict], repo_name: str) -> str:
    """Render archive/index.html — daftar history snapshot."""
    rows = []
    for s in snapshots:
        rows.append(
            f'<a class="card" href="{s["filename"]}"><div class="dt">{s["date_label"]}</div>'
            f'<div class="meta">Realisasi: Rp {s["total_m"]} · {s["pct"]}% pagu</div></a>'
        )
    return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PDO Arsip — Mingguan</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',system-ui,sans-serif;background:#0F172A;color:#E2E8F0;padding:24px;min-height:100vh}}
.wrap{{max-width:760px;margin:0 auto}}
h1{{font-size:20px;font-weight:600;margin-bottom:6px;color:#38BDF8}}
p.sub{{font-size:12px;color:#94A3B8;margin-bottom:24px}}
.card{{display:block;background:#1A2540;border:1px solid rgba(148,163,184,0.12);border-radius:12px;padding:14px 18px;margin-bottom:10px;text-decoration:none;color:inherit;transition:background .15s,transform .15s}}
.card:hover{{background:#202D47;transform:translateY(-1px)}}
.dt{{font-size:14px;font-weight:600;color:#34D399}}
.meta{{font-size:11px;color:#94A3B8;margin-top:3px;font-family:'JetBrains Mono',monospace}}
.back{{display:inline-block;margin-top:18px;font-size:11px;color:#38BDF8;text-decoration:none}}
.back:hover{{text-decoration:underline}}
</style></head><body><div class="wrap">
<h1>📊 PDO — Arsip Mingguan</h1>
<p class="sub">Inspektorat Pemprov Sulawesi Tenggara · TA 2026 · {len(snapshots)} snapshot</p>
{''.join(rows)}
<a class="back" href="../">← Kembali ke versi terbaru</a>
</div></body></html>"""


def render_root_redirect(latest_filename: str) -> str:
    """Render redirect index.html ke versi terbaru."""
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta http-equiv="refresh" content="0;url=archive/{latest_filename}"><title>Redirecting...</title></head>
<body>Redirecting to <a href="archive/{latest_filename}">latest snapshot</a>...</body></html>"""


def render_readme(repo_name: str, latest_date: str) -> str:
    """README untuk repo."""
    return f"""# {repo_name}

Dashboard realisasi anggaran **Inspektorat Pemprov Sulawesi Tenggara TA 2026** — update mingguan dari SPJ Fungsional SIPD.

🌐 **Lihat dashboard:** [https://gustiyuda14-source.github.io/{repo_name}/](https://gustiyuda14-source.github.io/{repo_name}/)
📅 **Snapshot terkini:** {latest_date}
📂 **Arsip mingguan:** [archive/](archive/)

## Update Workflow

Tiap GU baru:
```bash
py pdo_update.py "Fungsional Per <tgl>_<bln>_<thn>.pdf"
```

Script otomatis: extract PDF → hitung rolling snapshot → generate HTML + report → push ke Pages.

Lihat `pdo_update.py --help` untuk flag tambahan.
"""


def deploy_pages(out_html_path: Path, report_md_path: Path, plan: dict, ver: dict,
                 new_curr: str, repo_name: str, auto_confirm: bool = False) -> str | None:
    """Setup repo (kalau belum) + commit + push. Returns Pages URL."""
    git_dir = PROJ / ".git"
    is_first = not git_dir.exists()

    # Generate file naming
    new_d = parse_date(new_curr)
    iso_date = f"{new_d[2]:04d}-{new_d[1]:02d}-{new_d[0]:02d}"

    archive_dir = PROJ / "archive"
    reports_dir = PROJ / "reports"
    archive_dir.mkdir(exist_ok=True)
    reports_dir.mkdir(exist_ok=True)

    # Copy output → archive/<iso_date>.html
    archive_html = archive_dir / f"{iso_date}.html"
    archive_html.write_text(out_html_path.read_text(encoding="utf-8"), encoding="utf-8")
    # Copy report → reports/<iso_date>.md
    archive_report = reports_dir / f"{iso_date}.md"
    archive_report.write_text(report_md_path.read_text(encoding="utf-8"), encoding="utf-8")

    # Build snapshot list (scan archive/)
    snapshots = []
    for f in sorted(archive_dir.glob("????-??-??.html"), reverse=True):
        try:
            html = f.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"const CURR_DATE\s*=\s*'([^']+)'", html)
            curr = m.group(1) if m else f.stem
            # Parse total m from prog rows
            prog_re = re.compile(r"\{t:'prog'[^}]*m:(\d+)")
            total = sum(int(x.group(1)) for x in prog_re.finditer(html))
            snapshots.append(dict(
                filename=f.name, date_label=curr,
                total_m=fmt_rp(total),
                pct=f"{total/PAGU_TOTAL*100:.2f}",
            ))
        except Exception:
            continue

    if not snapshots:
        return None

    latest = snapshots[0]
    # Write root index.html (redirect)
    (PROJ / "index.html").write_text(render_root_redirect(latest["filename"]), encoding="utf-8")
    # Write archive index
    (archive_dir / "index.html").write_text(render_archive_index(snapshots, repo_name), encoding="utf-8")
    # Write README
    (PROJ / "README.md").write_text(render_readme(repo_name, latest["date_label"]), encoding="utf-8")
    # .nojekyll + .gitignore (first run)
    (PROJ / ".nojekyll").write_text("", encoding="utf-8")
    gi = PROJ / ".gitignore"
    if not gi.exists():
        gi.write_text("_*.json\n_*.py\nFungsional*.pdf\n*.pptx\n*~$*\n__pycache__/\n",
                      encoding="utf-8")

    # Git init + first push
    if is_first:
        print(f"\n  [first deploy] Setup git repo + GitHub Pages...")
        if not auto_confirm:
            if not confirm(f"  Buat repo PUBLIC '{repo_name}' di github.com/gustiyuda14-source dan push?", default=True):
                print("  Dibatalkan.")
                return None
        run(["git", "init"], cwd=PROJ)
        run(["git", "branch", "-M", "main"], cwd=PROJ)
        # Ensure local git identity exists (avoid commit failure)
        try:
            name = run(["git", "config", "--get", "user.name"], cwd=PROJ, check=False).stdout.strip()
            email = run(["git", "config", "--get", "user.email"], cwd=PROJ, check=False).stdout.strip()
        except Exception:
            name, email = "", ""
        if not name:
            run(["git", "config", "--local", "user.name", "gustiyuda14-source"], cwd=PROJ)
        if not email:
            # Use GitHub no-reply (privacy-friendly)
            run(["git", "config", "--local", "user.email",
                 "273990713+gustiyuda14-source@users.noreply.github.com"], cwd=PROJ)
        run(["git", "add",
             "index.html", ".nojekyll", ".gitignore", "README.md",
             "archive", "reports", "pdo_update.py", "CLAUDE.md"], cwd=PROJ)
        run(["git", "commit", "-m", f"Initial PDO dashboard — {new_curr}"], cwd=PROJ)
        # Create repo via gh
        try:
            result = run(["gh", "repo", "create", repo_name, "--public", "--source=.", "--remote=origin", "--push"],
                         cwd=PROJ, check=False)
            if result.returncode != 0:
                print(f"  gh create error: {result.stderr.strip()}")
                # Maybe repo exists, try adding remote + push
                run(["git", "remote", "add", "origin",
                     f"https://github.com/gustiyuda14-source/{repo_name}.git"], cwd=PROJ, check=False)
                run(["git", "push", "-u", "origin", "main"], cwd=PROJ)
        except Exception as e:
            print(f"  ⚠️ gh repo create failed: {e}")
            return None
        # Enable Pages
        try:
            run(["gh", "api", f"repos/gustiyuda14-source/{repo_name}/pages",
                 "-X", "POST", "-f", "source[branch]=main", "-f", "source[path]=/"],
                cwd=PROJ, check=False)
        except Exception:
            pass
    else:
        # Subsequent run — commit + push
        run(["git", "add",
             "index.html", "README.md", f"archive/{iso_date}.html",
             f"reports/{iso_date}.md", "archive/index.html", "pdo_update.py", "CLAUDE.md"],
            cwd=PROJ, check=False)
        # Check only STAGED changes (not untracked / unstaged)
        diff_result = run(["git", "diff", "--cached", "--quiet"], cwd=PROJ, check=False)
        if diff_result.returncode == 0:
            print("  ⚠️ Tidak ada perubahan untuk di-commit (idempotent — sudah pernah deploy).")
            return f"https://gustiyuda14-source.github.io/{repo_name}/"
        if not auto_confirm:
            if not confirm(f"  Commit & push update untuk {new_curr}?", default=True):
                print("  Dibatalkan.")
                return None
        run(["git", "commit", "-m", f"Update SPJ {new_curr}"], cwd=PROJ)
        run(["git", "push"], cwd=PROJ)

    pages_url = f"https://gustiyuda14-source.github.io/{repo_name}/"
    return pages_url


# ─── Main Wizard ────────────────────────────────────────────
def suggest_output_name(new_curr: str) -> str:
    d = parse_date(new_curr)
    if not d:
        return "pengendali_digital_on_mingguan_output.html"
    return f"pengendali_digital_on_mingguan_{d[3].lower()}_{d[0]}_{d[2]}.html"


def find_latest_output_html(folder: Path) -> Path | None:
    """Cari file output terbaru `pengendali_digital_on_mingguan_*.html` by mtime."""
    cands = list(folder.glob("pengendali_digital_on_mingguan_*.html"))
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def find_matching_report(folder: Path, new_curr: str) -> Path | None:
    """Cari report markdown matching new_curr date."""
    d = parse_date(new_curr)
    if not d:
        return None
    name = f"_REPORT_{d[3].lower()}_{d[0]}_{d[2]}.md"
    p = folder / name
    return p if p.exists() else None


def build_validation_dict(baseline, plan, ver, new_curr, pdf_path, out_path, report_path):
    """Build structured validation summary for JSON output."""
    items_naik = [(k, it, it["m"] - it["f"]) for k, it in ver["items"].items() if it["m"] - it["f"] > 0]
    items_naik.sort(key=lambda x: -x[2])
    items_real = [(k, it) for k, it in ver["items"].items() if it["m"] > 0]
    items_real.sort(key=lambda x: -x[1]["m"])

    sisa_per_program = []
    for pk, p in ver["progs"].items():
        sisa = p["p"] - p["m"]
        sisa_per_program.append(dict(
            kode=pk, nama=plan["prog"][pk]["nama"],
            pagu=p["p"], realisasi=p["m"], sisa=sisa,
            pct_realisasi=round(p["m"] / p["p"] * 100, 2) if p["p"] else 0.0,
            pct_sisa=round(sisa / p["p"] * 100, 2) if p["p"] else 0.0,
        ))

    return dict(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        pdf_file=pdf_path.name,
        baseline_date=baseline["curr_date"],
        new_date=new_curr,
        bulan_transition=plan["bulan_transition"],
        pdf_bulan=plan["pdf_bulan"],
        total_pagu=PAGU_TOTAL,
        total_f=ver["total_f"],
        total_m=ver["total_m"],
        delta_total=ver["total_m"] - ver["total_f"],
        pct_pagu=round(ver["total_m"] / PAGU_TOTAL * 100, 2),
        pct_pagu_prev=round(ver["total_f"] / PAGU_TOTAL * 100, 2),
        cross_check=dict(e1=ver["e1"], e2=ver["e2"], e3=ver["e3"], e4_match=ver["e4_match"]),
        all_checks_pass=(ver["e1"] == 0 and ver["e2"] == 0 and ver["e3"] == 0 and ver["e4_match"]),
        sisa_per_program=sisa_per_program,
        top_items_realisasi=[
            dict(kode=k, nama=it["n"], pagu=it["p"], m=it["m"],
                 pct_pagu=round(it["m"] / it["p"] * 100, 2) if it["p"] else 0.0)
            for k, it in items_real[:3]
        ],
        top_items_delta=[
            dict(kode=k, nama=it["n"], f=it["f"], m=it["m"], delta=d,
                 pct_pagu=round(it["m"] / it["p"] * 100, 2) if it["p"] else 0.0)
            for k, it, d in items_naik[:3]
        ],
        output_html=out_path.name if out_path else None,
        output_report=report_path.name if report_path else None,
    )


def build_export_dict(plan: dict, new_curr: str) -> dict:
    """Export data c11n per rekening untuk e_pengendalian_submit.py."""
    bulan_nama = plan["pdf_bulan"] or ""
    bulan_num  = BULAN_FULL_TO_NUM.get(bulan_nama, 0)
    items_out  = {}
    for kode, item in plan["item"].items():
        dets = [
            dict(kode=d["k"], nama=d["n"], pagu=d["p"], c11n=d["c11n"])
            for d in item["details"]
        ]
        if dets:
            items_out[kode] = dict(nama=item["nama"], details=dets)
    return dict(bulan_num=bulan_num, bulan_nama=bulan_nama, new_date=new_curr, items=items_out)


def main():
    ap = argparse.ArgumentParser(description="PDO Update — Smart Wizard")
    ap.add_argument("pdf", nargs="?", help="Path PDF SPJ Fungsional baru (opsional kalau --deploy-only)")
    ap.add_argument("--baseline", help="Override path baseline HTML (auto-detect by default)")
    ap.add_argument("--output", help="Override path output HTML")
    ap.add_argument("--no-deploy", action="store_true", help="Skip push ke GitHub Pages")
    ap.add_argument("--dry-run", action="store_true", help="Generate ke memory saja, tidak tulis file")
    ap.add_argument("--c11p-rolling", action="store_true", help="[DEPRECATED — kini default] Pakai c11p=c11n_lama (literal rolling)")
    ap.add_argument("--c11p-rebase", action="store_true", help="Pakai c11p=0 (mode lama; delta detail = c11n penuh). Default sekarang rolling.")
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"Nama repo GH Pages (default: {DEFAULT_REPO})")
    ap.add_argument("--yes", "-y", action="store_true", help="Auto-confirm semua prompt")
    ap.add_argument("--validation-json", help="Emit struct validation summary ke file JSON (untuk dipakai skill Claude Code)")
    ap.add_argument("--export-json", help="Export data c11n per rekening untuk e_pengendalian_submit.py")
    ap.add_argument("--deploy-only", action="store_true",
                    help="Skip generate, langsung deploy file output terbaru ke GitHub Pages")
    args = ap.parse_args()

    # ─── DEPLOY-ONLY MODE ──────────────────────────────────
    if args.deploy_only:
        banner("[deploy-only] Push file output terbaru ke GitHub Pages")
        out_path = find_latest_output_html(PROJ)
        if not out_path:
            print(f"❌ Tidak ada file 'pengendali_digital_on_mingguan_*.html' di {PROJ}")
            return 1
        html = out_path.read_text(encoding="utf-8")
        m = re.search(r"const CURR_DATE\s*=\s*'([^']+)'", html)
        if not m:
            print(f"❌ CURR_DATE tidak ditemukan di {out_path.name}")
            return 1
        new_curr = m.group(1)
        report_path = find_matching_report(PROJ, new_curr)
        if not report_path:
            print(f"⚠️ Report tidak ditemukan untuk {new_curr}, lanjut tanpa report (placeholder kosong).")
            # Buat report placeholder agar deploy_pages tidak crash
            tmp_report = PROJ / f"_REPORT_placeholder.md"
            tmp_report.write_text(f"# Update {new_curr}\n\n_Report otomatis tidak tersedia._\n", encoding="utf-8")
            report_path = tmp_report
        print(f"  HTML: {out_path.name}")
        print(f"  Report: {report_path.name}")
        print(f"  Tanggal: {new_curr}")
        pages_url = deploy_pages(out_path, report_path, {}, {}, new_curr, args.repo, auto_confirm=args.yes)
        if pages_url:
            print(f"\n  ✅ URL Pages: {pages_url}")
        else:
            print(f"  ⚠️ Deploy tidak selesai.")
        return 0

    # PDF wajib untuk mode normal
    if not args.pdf:
        print("❌ Argumen PDF wajib (kecuali pakai --deploy-only).")
        return 1
    pdf_path = Path(args.pdf)
    if not pdf_path.is_absolute():
        pdf_path = PROJ / pdf_path
    if not pdf_path.exists():
        print(f"❌ PDF tidak ditemukan: {pdf_path}")
        return 1

    # ─── Step 1: baseline ─────────────────────────────
    banner("[1/5] Auto-detect baseline")
    if args.baseline:
        baseline_path = Path(args.baseline)
        if not baseline_path.is_absolute():
            baseline_path = PROJ / baseline_path
    else:
        baseline_path = auto_detect_baseline(PROJ)
        if not baseline_path:
            print(f"❌ Tidak ada baseline HTML di {PROJ}")
            return 1
        print(f"  Terbaru: {baseline_path.name}")
    if not baseline_path.exists():
        print(f"❌ Baseline tidak ditemukan: {baseline_path}")
        return 1
    baseline = parse_baseline(baseline_path)
    print(f"  Snapshot lama: {baseline['curr_date']} (prev: {baseline['prev_date']})")

    # ─── Step 2: PDF + bulan transition ───────────────
    banner("[2/5] Baca PDF + deteksi bulan")
    print(f"  PDF: {pdf_path.name}")
    pdf_data = extract_pdf(pdf_path)
    prev_d = parse_date(baseline["curr_date"])
    prev_bulan_num = prev_d[1] if prev_d else None
    pdf_bulan_full = pdf_data.get("bulan_pdf")
    pdf_bulan_num = BULAN_FULL_TO_NUM.get(pdf_bulan_full) if pdf_bulan_full else None
    print(f"  Bulan PDF: {pdf_bulan_full or '(tidak terdeteksi)'}")
    print(f"  Sub-keg={len(pdf_data['sub'])}, item={len(pdf_data['item'])}, rek={len(pdf_data['rek'])}")
    if pdf_bulan_num and prev_bulan_num and pdf_bulan_num != prev_bulan_num:
        print(f"  🔄 Bulan transition: {prev_d[3]} → {pdf_bulan_full} (c10 di-rollover)")

    # ─── Step 3: tanggal CURR_DATE baru ───────────────
    banner("[3/5] Tanggal snapshot baru")
    # Default: ambil tgl dari nama PDF (mis. "Per 22_5_2026.pdf" → 22 Mei 2026)
    nm = re.search(r"(\d+)_(\d+)_(\d{4})", pdf_path.name)
    default_curr = ""
    if nm:
        day, mon, year = int(nm.group(1)), int(nm.group(2)), int(nm.group(3))
        abbr = next((k for k,v in BULAN_ABBR_TO_NUM.items() if v == mon), None)
        if abbr:
            default_curr = f"{day} {abbr} {year}"
    if args.yes and default_curr:
        new_curr = default_curr
        print(f"  Tanggal: {new_curr} (auto)")
    else:
        new_curr = ask(f"  Tanggal snapshot baru", default_curr) if default_curr else ask("  Tanggal snapshot baru (format: D Mmm YYYY)")
    if not parse_date(new_curr):
        print(f"❌ Tanggal tidak valid: {new_curr}")
        return 1

    # Output filename
    if args.output:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = PROJ / out_path
    else:
        default_out = suggest_output_name(new_curr)
        if args.yes:
            out_path = PROJ / default_out
            print(f"  Output: {default_out} (auto)")
        else:
            chosen = ask("  Nama file output", default_out)
            out_path = PROJ / chosen

    # ─── Step 4: build + generate + verify ────────────
    banner("[4/5] Generate + verify")
    plan = build_plan(baseline, pdf_data, c11p_mode="rebase" if args.c11p_rebase else "rolling")
    new_html = apply_template(baseline, plan, new_curr)

    # PDF total (Kol.13 BELANJA DAERAH from page 1 — sum from all prog items)
    pdf_total = sum(p["m_new"] for p in plan["prog"].values())

    ver = verify(new_html, expected_total=pdf_total)
    print(f"  Total Pagu:      Rp {fmt_rp(PAGU_TOTAL)}")
    print(f"  Realisasi lalu:  Rp {fmt_rp(ver['total_f'])}")
    print(f"  Realisasi baru:  Rp {fmt_rp(ver['total_m'])}")
    print(f"  Kenaikan:        +Rp {fmt_rp(ver['total_m'] - ver['total_f'])}")
    print()
    print(f"  Cross-check:")
    e1, e2, e3 = ver["e1"], ver["e2"], ver["e3"]
    s1 = "✅" if e1 == 0 else f"❌ {e1} mismatch"
    s2 = "✅" if e2 == 0 else f"❌ {e2} mismatch"
    s3 = "✅" if e3 == 0 else f"❌ {e3} mismatch"
    s4 = "✅ MATCH" if ver["e4_match"] else "❌ MISMATCH"
    print(f"    [1] prog.m = Σsub.m:        {s1}")
    print(f"    [2] sub.m  = Σitem.m:       {s2}")
    print(f"    [3] item.δ = Σdet.δ (n-gj): {s3}")
    print(f"    [4] total  = PDF Kol.13:    {s4}")

    if args.dry_run:
        print(f"\n  [DRY-RUN] File TIDAK ditulis. Output size: {len(new_html):,} chars".replace(",", "."))
        # Tetap emit JSON kalau diminta (validation only mode)
        if args.validation_json:
            vdict = build_validation_dict(baseline, plan, ver, new_curr, pdf_path, None, None)
            vpath = Path(args.validation_json)
            if not vpath.is_absolute():
                vpath = PROJ / vpath
            vpath.write_text(json.dumps(vdict, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  ✅ Validation JSON: {vpath.name}")
        return 0

    # Tulis file output
    out_path.write_text(new_html, encoding="utf-8")
    print(f"\n  ✅ HTML: {out_path.name}")

    # Generate diff report
    report_md = build_diff_report(baseline, plan, ver, new_curr, pdf_total)
    new_d = parse_date(new_curr)
    report_name = f"_REPORT_{new_d[3].lower()}_{new_d[0]}_{new_d[2]}.md"
    report_path = PROJ / report_name
    report_path.write_text(report_md, encoding="utf-8")
    print(f"  ✅ Report: {report_path.name}")

    # Emit validation JSON (untuk skill Claude Code)
    if args.validation_json:
        vdict = build_validation_dict(baseline, plan, ver, new_curr, pdf_path, out_path, report_path)
        vpath = Path(args.validation_json)
        if not vpath.is_absolute():
            vpath = PROJ / vpath
        vpath.write_text(json.dumps(vdict, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  ✅ Validation JSON: {vpath.name}")

    # Emit export JSON (untuk e_pengendalian_submit.py)
    if args.export_json:
        edict = build_export_dict(plan, new_curr)
        epath = Path(args.export_json)
        if not epath.is_absolute():
            epath = PROJ / epath
        epath.write_text(json.dumps(edict, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  ✅ Export JSON: {epath.name}")

    # ─── Step 5: deploy ───────────────────────────────
    if args.no_deploy:
        print(f"\n  [skip deploy] flag --no-deploy aktif.")
        return 0

    banner("[5/5] Deploy GitHub Pages")
    pages_url = deploy_pages(out_path, report_path, plan, ver, new_curr,
                              args.repo, auto_confirm=args.yes)
    if pages_url:
        print(f"\n  ✅ URL Pages: {pages_url}")
        print(f"     (perlu ~1 menit untuk build pertama kali)")
    else:
        print(f"  ⚠️ Deploy tidak selesai.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
