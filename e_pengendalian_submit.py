"""e_pengendalian_submit.py — Automasi input realisasi ke e-Pengendalian Ver 2.0
(https://epengendalian.sultraprov.go.id — sistem baru Biro Pembangunan, Juli 2026)

Workflow:
  1. python3 pdo_update.py "Fungsional Per 12_06_2026.pdf" --export-json submit_data.json
  2. python3 e_pengendalian_submit.py submit_data.json --dry-run   # preview dulu
  3. python3 e_pengendalian_submit.py submit_data.json -y          # jalankan

Model sistem baru (beda total dari versi lama):
  - Input realisasi PER PEKAN (vol_w1..w5 + pag_w1..w5), bukan per bulan.
  - Pekan yang sudah lewat DIKUNCI server — tidak bisa backfill.
  - Tiap rekening diakses via anggaran_id (di-scrape dari data_realisasi.php).
  - Volume = rupiah / "Nilai Kegiatan per Satuan Volume" (pagu tahunan / vol tahunan)
    — ekuivalen formula hitung_rf lama, nilai per satuan diambil live dari form.
  - Rekonsiliasi TOTAL-BASED: nilai pekan target = c11n (realisasi bulan ini SPJ)
    dikurangi jumlah pekan-pekan lain yang sudah terisi.

Legacy versi situs lama (Laravel, epengendalian-sultraprov.org) disimpan di
e_pengendalian_submit_v1_legacy.py sebagai referensi.
"""
from __future__ import annotations

import argparse, csv, json, math, os, re, sys, time, urllib.parse, urllib.request, http.cookiejar
from datetime import datetime
from pathlib import Path

BASE_URL  = "https://epengendalian.sultraprov.go.id"
LOGIN_URL = f"{BASE_URL}/modules/auth/login.php"
DATA_URL  = f"{BASE_URL}/modules/admin-opd/data_realisasi.php"
INPUT_URL = f"{BASE_URL}/modules/admin-opd/input_realisasi.php"

# Kredensial WAJIB dari environment variable — tidak ada fallback tersimpan di source.
# Sistem baru login pakai USERNAME (bukan email). EPENGENDALIAN_EMAIL tetap dibaca
# untuk kompatibilitas dengan setup shell lama.
DEFAULT_USER = os.environ.get("EPENGENDALIAN_USER") or os.environ.get("EPENGENDALIAN_EMAIL")
DEFAULT_PASS = os.environ.get("EPENGENDALIAN_PASS")

DELAY_SEC = 1.5    # jeda antar POST (sopan ke server Pemprov) — sengaja tidak diubah
TIMEOUT   = 180    # DB terpusat Diskominfo lambat (halaman berat 6-18s, kadang lebih)
MAP_CACHE_FILE = Path(__file__).parent / "_anggaran_map_cache.json"


def normalize_code(kode: str) -> str:
    """Normalisasi kode SIPD/rekening: strip leading zeros tiap segmen.

    Contoh: '5.1.02.02.001.00061' → '5.1.2.2.1.61' (padding beda tetap match).
    """
    return ".".join(str(int(s)) if s.isdigit() else s for s in kode.split("."))


def parse_num(val) -> float:
    """Parse angka dari hidden input / JS var ('0.82', '0,82', '21500')."""
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s or s in ("-", "None"):
        return 0.0
    if "," in s and "." in s:                 # format id-ID: 1.234,56
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


# ─── HTTP ────────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Jangan ikuti redirect — 302 setelah login mengarah ke dashboard yang
    query DB-nya bisa 18+ detik. Kita cukup baca header Location."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_opener():
    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), _NoRedirect())
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"),
        ("Accept", "text/html,application/xhtml+xml,*/*;q=0.8"),
    ]
    return opener


def http_request(opener, url: str, data: dict | None = None,
                 referer: str | None = None, retries: int = 1):
    """Return (status_code, body, location). Retry sekali kalau timeout."""
    headers = {}
    if referer:
        headers["Referer"] = referer
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Origin"] = BASE_URL
    req = urllib.request.Request(url, data=body, headers=headers)
    for attempt in range(retries + 1):
        try:
            r = opener.open(req, timeout=TIMEOUT)
            return r.getcode(), r.read().decode("utf-8", errors="replace"), ""
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303):
                return e.code, "", e.headers.get("Location", "")
            raise
        except (TimeoutError, urllib.error.URLError) as e:
            if attempt < retries:
                print(f"    ⏳ timeout/koneksi ({e}) — coba ulang...")
                time.sleep(3)
                continue
            raise
    raise RuntimeError("unreachable")


def assert_logged_in(code: int, location: str, konteks: str):
    if code in (301, 302, 303) and "login" in location:
        raise RuntimeError(f"Sesi berakhir / belum login saat {konteks} (redirect ke {location})")


# ─── Auth ────────────────────────────────────────────────────────────────────

def login(username: str, password: str):
    """Login ke e-Pengendalian Ver 2.0. Return (opener, csrf_token).

    csrf_token bersifat session-wide — satu token dipakai semua form.
    """
    opener = build_opener()

    code, html, _ = http_request(opener, LOGIN_URL)
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    if not m:
        raise RuntimeError("csrf_token tidak ditemukan di halaman login — struktur situs berubah lagi?")
    csrf = m.group(1)

    code, html, loc = http_request(
        opener, LOGIN_URL,
        data={"csrf_token": csrf, "username": username, "password": password},
        referer=LOGIN_URL)

    if code in (301, 302, 303) and "dashboard" in loc:
        return opener, csrf
    err = re.search(r'alert[^>]*>\s*([^<]{5,120})', html or "")
    raise RuntimeError(f"Login gagal (HTTP {code}): {err.group(1).strip() if err else 'periksa username/password'}")


# ─── Discovery: (item, rekening) → anggaran_id ───────────────────────────────

def discover_map(opener, tahun: int, bulan: int, use_cache: bool = True) -> dict[str, int]:
    """Return {'<norm_item>|<norm_rek>': anggaran_id}.

    Kode rekening yang sama bisa muncul di banyak item (Perjadin Biasa ada di 17
    item per Jul 2026), jadi kunci mapping WAJIB pasangan item+rekening.
    anggaran_id berskala tahun — cache di-invalidasi kalau tahun berbeda.
    """
    if use_cache and MAP_CACHE_FILE.exists():
        cached = json.loads(MAP_CACHE_FILE.read_text(encoding="utf-8"))
        if cached.get("tahun") == tahun:
            data = cached["map"]
            print(f"  ✅ Mapping anggaran_id dari cache ({len(data)} entry) — hapus {MAP_CACHE_FILE.name} untuk refresh")
            return {k: int(v) for k, v in data.items()}

    print(f"  Scanning data_realisasi.php tahun={tahun} bulan={bulan} (halaman ini lambat, sabar)...")
    code, html, loc = http_request(opener, f"{DATA_URL}?tahun={tahun}&bulan={bulan}",
                                   referer=DATA_URL)
    assert_logged_in(code, loc, "scan mapping")

    mapping: dict[str, int] = {}
    cur_item = None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        m = re.search(r"<td[^>]*>\s*([56](?:\.\d+){2,5})\s*</td>", row)
        if not m:
            continue
        kode = m.group(1)
        nseg = kode.count(".") + 1
        if kode.startswith("6") and nseg == 6:
            cur_item = normalize_code(kode)
        elif kode.startswith("5") and nseg == 6 and cur_item:
            aid = re.search(r"anggaran_id=(\d+)", row)
            if aid:
                mapping[f"{cur_item}|{normalize_code(kode)}"] = int(aid.group(1))

    if not mapping:
        raise RuntimeError("Tidak ada rekening ter-mapping — cek apakah struktur halaman berubah")

    MAP_CACHE_FILE.write_text(json.dumps({"tahun": tahun, "map": mapping}, indent=2),
                              encoding="utf-8")
    print(f"  ✅ {len(mapping)} rekening ter-mapping, disimpan ke cache")
    return mapping


# ─── Form per rekening ───────────────────────────────────────────────────────

def form_url(aid: int, tahun: int, bulan: int) -> str:
    return f"{INPUT_URL}?anggaran_id={aid}&tahun={tahun}&bulan={bulan}"


def fetch_form(opener, aid: int, tahun: int, bulan: int) -> dict:
    """GET form input realisasi satu rekening. Return state lengkap:

    vols/pags : nilai hidden per pekan 1..5 (string mentah utk pekan non-target)
    locked    : set pekan yang dikunci server (readonly)
    vars      : nilaiPerSatuan, targetVolKum, targetPagKum, realVolKum,
                realPagKum, totalVolTahun, totalPagTahun (dari JS — sumber
                kebenaran validasi server)
    """
    url = form_url(aid, tahun, bulan)
    code, html, loc = http_request(opener, url, referer=f"{DATA_URL}?tahun={tahun}&bulan={bulan}")
    assert_logged_in(code, loc, f"fetch form anggaran_id={aid}")

    jsvars = {}
    for name in ("targetVolKum", "targetPagKum", "realVolKum", "realPagKum",
                 "totalVolTahun", "totalPagTahun", "nilaiPerSatuan"):
        m = re.search(rf"var {name} = ([0-9]+(?:\.[0-9]+)?);", html)
        jsvars[name] = float(m.group(1)) if m else 0.0

    vols_raw, pags_raw, locked = {}, {}, set()
    for w in range(1, 6):
        mv = re.search(rf'name="vol_w{w}_hidden"[^>]*value="([^"]*)"', html)
        mp = re.search(rf'name="pag_w{w}_hidden"[^>]*value="([^"]*)"', html)
        if not mv or not mp:
            raise RuntimeError(f"Field pekan {w} tidak ditemukan di form anggaran_id={aid}")
        vols_raw[w] = mv.group(1)
        pags_raw[w] = mp.group(1)
        tag = re.search(rf'<input[^>]*data-target="vol_w{w}_hidden"[^>]*>', html)
        if tag and "readonly" in tag.group(0):
            locked.add(w)

    return {"url": url, "vols_raw": vols_raw, "pags_raw": pags_raw,
            "vols": {w: parse_num(v) for w, v in vols_raw.items()},
            "pags": {w: parse_num(v) for w, v in pags_raw.items()},
            "locked": locked, "vars": jsvars}


def plan_week_value(form: dict, c11n: int, minggu: int) -> dict:
    """Hitung nilai pekan target dgn rekonsiliasi total-based + mirror validasi server.

    Return {'status': 'submit'|'sync'|'blocked', 'pag': int, 'vol': float, 'note': str}
    """
    v = form["vars"]
    pag_other = sum(p for w, p in form["pags"].items() if w != minggu)
    vol_other = sum(x for w, x in form["vols"].items() if w != minggu)

    pag_new = c11n - pag_other
    if pag_new < 0:
        return {"status": "blocked", "pag": 0, "vol": 0.0,
                "note": f"total pekan lain (Rp {pag_other:,.0f}) > c11n SPJ (Rp {c11n:,}) — cek manual"}
    if abs(pag_new - form["pags"][minggu]) < 0.5:
        return {"status": "sync", "pag": int(round(pag_new)), "vol": form["vols"][minggu],
                "note": "sudah sinkron dengan SPJ"}

    nps = v["nilaiPerSatuan"]
    vol_new = round(pag_new / nps, 2) if nps > 0 else 0.0
    note = ""

    # Mirror 4 validasi updateTotals() sisi situs (server dianggap sama):
    # volume boleh di-clamp (angka turunan, efek pembulatan); rupiah TIDAK —
    # kalau rupiah melanggar, jangan kirim, lapor.
    vol_cap = min(v["targetVolKum"], v["totalVolTahun"]) - v["realVolKum"] - vol_other
    if vol_new > vol_cap + 0.001:
        clamped = max(0.0, math.floor(vol_cap * 100) / 100)
        note = f"vol {vol_new} > cap {vol_cap:.2f} → clamp ke {clamped}"
        vol_new = clamped

    tp = pag_other + pag_new
    if v["realPagKum"] + tp > v["totalPagTahun"] + 0.001:
        return {"status": "blocked", "pag": int(round(pag_new)), "vol": vol_new,
                "note": f"kumulatif Rp {v['realPagKum'] + tp:,.0f} > pagu tahunan Rp {v['totalPagTahun']:,.0f}"}
    if tp > (v["targetPagKum"] - v["realPagKum"]) + 0.001:
        return {"status": "blocked", "pag": int(round(pag_new)), "vol": vol_new,
                "note": (f"realisasi bulan ini Rp {tp:,.0f} > sisa target kumulatif "
                         f"Rp {v['targetPagKum'] - v['realPagKum']:,.0f} — target bulanan di "
                         f"e-Pengendalian terlalu kecil, minta Biro sesuaikan")}

    return {"status": "submit", "pag": int(round(pag_new)), "vol": vol_new, "note": note}


def build_payload(form: dict, csrf: str, minggu: int, pag: int, vol: float) -> dict:
    """Payload POST — pekan non-target dikirim persis nilai mentah dari form
    (perilaku identik browser), hanya pekan target yang diganti."""
    payload = {"csrf_token": csrf}
    for w in range(1, 6):
        if w == minggu:
            payload[f"vol_w{w}_hidden"] = f"{vol:.2f}"
            payload[f"pag_w{w}_hidden"] = str(pag)
        else:
            payload[f"vol_w{w}_hidden"] = form["vols_raw"][w]
            payload[f"pag_w{w}_hidden"] = form["pags_raw"][w]
    payload["simpan"] = ""
    return payload


def submit_and_verify(opener, csrf: str, form: dict, minggu: int,
                      pag: int, vol: float, aid: int, tahun: int, bulan: int) -> tuple[bool, str]:
    """POST lalu verifikasi dengan re-GET form dan bandingkan nilai tersimpan.

    Catatan: JANGAN cek substring "alert-danger" di response POST sebagai tanda
    gagal — string itu selalu ada di HTML (template JS client-side validation
    di dalam <script>, dirender statis di server pada tiap load, bukan alert
    yang benar-benar muncul). Satu-satunya sumber kebenaran adalah re-GET form
    setelah POST dan bandingkan nilai pag/vol tersimpan.
    """
    time.sleep(DELAY_SEC)
    payload = build_payload(form, csrf, minggu, pag, vol)
    code, html, loc = http_request(opener, form["url"], data=payload, referer=form["url"])

    after = fetch_form(opener, aid, tahun, bulan)
    if abs(after["pags"][minggu] - pag) < 0.5 and abs(after["vols"][minggu] - vol) < 0.011:
        return True, f"terverifikasi (HTTP {code})"
    return False, (f"POST terkirim (HTTP {code}) tapi verifikasi gagal: server menyimpan "
                   f"pag={after['pags'][minggu]:,.0f} vol={after['vols'][minggu]} "
                   f"(harusnya pag={pag:,} vol={vol})")


# ─── Batch Runner ─────────────────────────────────────────────────────────────

def run_batch(export_json_path: str, dry_run: bool, auto_confirm: bool,
              username: str, password: str, no_cache: bool, submit_gaji: bool,
              minggu_override: int | None, tahun_override: int | None) -> int:

    t_start = time.monotonic()

    with open(export_json_path, encoding="utf-8") as f:
        export = json.load(f)

    bulan_num  = export["bulan_num"]
    bulan_nama = export["bulan_nama"]
    new_date   = export["new_date"]
    items_data = export["items"]

    m_tahun = re.search(r"(20\d{2})", str(new_date))
    tahun   = tahun_override or (int(m_tahun.group(1)) if m_tahun else datetime.now().year)

    rek_dengan_realisasi = sum(
        1 for it in items_data.values()
        for d in it["details"] if d["c11n"] > 0
    )
    total_rek = sum(len(it["details"]) for it in items_data.values())

    gaji_rek = [
        d for it in items_data.values() for d in it["details"]
        if normalize_code(d["kode"]).startswith("5.1.1.") and d["c11n"] > 0
    ]
    gaji_total = sum(d["c11n"] for d in gaji_rek)

    print(f"\n  Tanggal    : {new_date}")
    print(f"  Bulan      : {bulan_nama} (bulan ke-{bulan_num}, tahun {tahun})")
    print(f"  Pekan      : {'ke-' + str(minggu_override) + ' (manual)' if minggu_override else 'auto (pekan terbuka pertama)'}")
    print(f"  Total rek  : {total_rek} | Dengan realisasi: {rek_dengan_realisasi}")
    print(f"  Mode       : {'DRY-RUN (tidak ada yang dikirim)' if dry_run else 'LIVE'}")
    print(f"  LS Gaji    : {'AKTIF (--submit-gaji)' if submit_gaji else 'DILEWATI (default)'}"
          + (f" — {len(gaji_rek)} rek, Rp {gaji_total:,}" if submit_gaji and gaji_rek else ""))

    if submit_gaji and gaji_rek and not dry_run:
        print("\n  ⚠️  PERHATIAN: Anda akan MENGINPUT realisasi GAJI (5.1.01.*).")
        print("     Pastikan tidak double-input dengan penginputan manual/pihak lain.")
        if not auto_confirm:
            g = input(f"     Lanjut kirim Rp {gaji_total:,}? [y/N]: ").strip().lower()
            if g != "y":
                print("     Gaji dibatalkan — jalankan lagi tanpa --submit-gaji untuk skip.")
                return 0

    if not auto_confirm:
        ans = input("\n  Lanjut? [y/N]: ").strip().lower()
        if ans != "y":
            print("  Dibatalkan.")
            return 0

    print("\n  Login...")
    t_login = time.monotonic()
    opener, csrf = login(username, password)
    print(f"  ✅ Login berhasil ({time.monotonic() - t_login:.1f}s)")

    t_map = time.monotonic()
    mapping = discover_map(opener, tahun, bulan_num, use_cache=not no_cache)
    print(f"  ⏱ Mapping anggaran_id: {time.monotonic() - t_map:.1f}s")

    results = []
    ok = err = skip = blocked = 0
    t_batch = time.monotonic()

    for item_kode, item_data in items_data.items():
        norm_item = normalize_code(item_kode)

        for det in item_data["details"]:
            rek_kode = det["kode"]
            c11n     = det["c11n"]

            if c11n <= 0:
                results.append(_log_row(item_kode, rek_kode, c11n, 0, 0.0, "skip", "c11n=0"))
                skip += 1
                continue

            # Belanja Pegawai / LS Gaji (5.1.01.*) — OPT-IN via --submit-gaji.
            if normalize_code(rek_kode).startswith("5.1.1.") and not submit_gaji:
                results.append(_log_row(item_kode, rek_kode, c11n, 0, 0.0, "skip",
                                        "Belanja Pegawai (5.1.01) — perlu flag --submit-gaji"))
                skip += 1
                continue

            aid = mapping.get(f"{norm_item}|{normalize_code(rek_kode)}")
            if not aid:
                print(f"  ❌ {item_kode} / {rek_kode} — tidak ada di mapping e-Pengendalian")
                results.append(_log_row(item_kode, rek_kode, c11n, 0, 0.0, "error",
                                        "pasangan item|rekening tidak ditemukan — cek data_realisasi.php"))
                err += 1
                continue

            form = fetch_form(opener, aid, tahun, bulan_num)

            open_weeks = [w for w in range(1, 6) if w not in form["locked"]]
            if not open_weeks:
                print(f"  🔒 {rek_kode} — semua pekan bulan {bulan_nama} terkunci")
                results.append(_log_row(item_kode, rek_kode, c11n, 0, 0.0, "blocked",
                                        "semua pekan terkunci — minta admin Biro buka lock"))
                blocked += 1
                continue
            minggu = minggu_override or open_weeks[0]
            if minggu in form["locked"]:
                results.append(_log_row(item_kode, rek_kode, c11n, 0, 0.0, "blocked",
                                        f"pekan ke-{minggu} terkunci (terbuka: {open_weeks})"))
                blocked += 1
                continue

            plan = plan_week_value(form, c11n, minggu)

            if plan["status"] == "sync":
                results.append(_log_row(item_kode, rek_kode, c11n, plan["pag"], plan["vol"],
                                        "skip", plan["note"]))
                skip += 1
                continue
            if plan["status"] == "blocked":
                print(f"  🚫 {rek_kode} — {plan['note']}")
                results.append(_log_row(item_kode, rek_kode, c11n, plan["pag"], plan["vol"],
                                        "blocked", plan["note"]))
                blocked += 1
                continue

            tag = "[DRY] " if dry_run else ""
            print(f"  {tag}→ {rek_kode} | pekan {minggu} | pag=Rp {plan['pag']:,} | vol={plan['vol']}"
                  + (f" | {plan['note']}" if plan["note"] else ""))

            if dry_run:
                results.append(_log_row(item_kode, rek_kode, c11n, plan["pag"], plan["vol"],
                                        "dry-run", f"pekan {minggu}; tidak dikirim"))
                ok += 1
                continue

            sukses, msg = submit_and_verify(opener, csrf, form, minggu,
                                            plan["pag"], plan["vol"], aid, tahun, bulan_num)
            if sukses:
                results.append(_log_row(item_kode, rek_kode, c11n, plan["pag"], plan["vol"], "ok", msg))
                ok += 1
            else:
                print(f"    ❌ Gagal: {msg}")
                results.append(_log_row(item_kode, rek_kode, c11n, plan["pag"], plan["vol"], "error", msg))
                err += 1

    print(f"\n  ✅ {ok} sukses | ❌ {err} gagal | 🚫 {blocked} terblokir | ⏭ {skip} dilewati")
    print(f"  ⏱ Batch submit: {time.monotonic() - t_batch:.1f}s | Total run: {time.monotonic() - t_start:.1f}s")

    log_dir  = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"submit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(log_path, "w", newline="", encoding="utf-8") as f:
        if results:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
    print(f"  📄 Log: {log_path}")

    return 1 if (err > 0 or blocked > 0) else 0


def _log_row(item_kode, rek_kode, c11n, pag, vol, status, msg) -> dict:
    return {"kode_item": item_kode, "kode_rek": rek_kode,
            "c11n": c11n, "pag_pekan": pag, "vol_pekan": vol,
            "status": status, "msg": msg,
            "timestamp": datetime.now().isoformat(timespec="seconds")}


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Automasi input realisasi ke e-Pengendalian Sultra Ver 2.0")
    ap.add_argument("export_json", help="File JSON dari: pdo_update.py --export-json <file>")
    ap.add_argument("--dry-run",   action="store_true", help="Preview saja, tidak mengirim ke server")
    ap.add_argument("--yes", "-y", action="store_true", help="Auto-confirm tanpa prompt")
    ap.add_argument("--no-cache",  action="store_true", help="Paksa rebuild mapping anggaran_id")
    ap.add_argument("--minggu",    type=int, choices=range(1, 6), default=None,
                    help="Paksa pekan target 1-5 (default: pekan terbuka pertama)")
    ap.add_argument("--tahun",     type=int, default=None,
                    help="Override tahun anggaran (default: dari new_date export)")
    ap.add_argument("--submit-gaji", action="store_true",
                    help="Ikutkan rekening Belanja Pegawai/LS Gaji (5.1.01.*). Default: dilewati.")
    ap.add_argument("--user",      default=DEFAULT_USER, help="Username login (default: env EPENGENDALIAN_USER)")
    ap.add_argument("--password",  default=DEFAULT_PASS, help="Password login (default: env EPENGENDALIAN_PASS)")
    args = ap.parse_args()

    if not args.user or not args.password:
        print("❌ Kredensial tidak ditemukan.")
        print("   Set env EPENGENDALIAN_USER & EPENGENDALIAN_PASS, atau pakai --user/--password.")
        return 1

    return run_batch(args.export_json, args.dry_run, args.yes,
                     args.user, args.password, args.no_cache, args.submit_gaji,
                     args.minggu, args.tahun)


if __name__ == "__main__":
    sys.exit(main())
