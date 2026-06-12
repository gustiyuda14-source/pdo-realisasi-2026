# pdo-realisasi-2026

Dashboard realisasi anggaran **Inspektorat Pemprov Sulawesi Tenggara TA 2026** — update mingguan dari SPJ Fungsional SIPD.

🌐 **Lihat dashboard:** [https://gustiyuda14-source.github.io/pdo-realisasi-2026/](https://gustiyuda14-source.github.io/pdo-realisasi-2026/)
📅 **Snapshot terkini:** 12 Jun 2026
📂 **Arsip mingguan:** [archive/](archive/)

## Update Workflow

Tiap GU baru:
```bash
py pdo_update.py "Fungsional Per <tgl>_<bln>_<thn>.pdf"
```

Script otomatis: extract PDF → hitung rolling snapshot → generate HTML + report → push ke Pages.

Lihat `pdo_update.py --help` untuk flag tambahan.
