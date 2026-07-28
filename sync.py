# -*- coding: utf-8 -*-
"""
KONSALT Staj Programi 2026 — Challenge 7: CSV -> CMDB Sync Motoru

envanter.csv dosyasindaki kayitlari dogrular, kirli veriyi ayirir ve
hedef CMDB API'sine guvenilir sekilde aktarir (token yenileme, retry, idempotency).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# --- Sabitler ----------------------------------------------------------------

DEFAULT_BASE_URL = "http://127.0.0.1:5050"
CLIENT_ID = "staj"
CLIENT_SECRET = "konsalt2026"
VALID_CI_TYPES = {"server", "application", "network_device"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RETRY_BACKOFF_SECONDS = (1, 2, 4)
MAX_ATTEMPTS = 4
TOKEN_REFRESH_BUFFER_SECONDS = 5

# --- Veri yapilari -----------------------------------------------------------


@dataclass
class SyncReport:
    baslangic: str = field(default_factory=lambda: _utc_now())
    bitis: str | None = None
    okunan: int = 0
    elenen: int = 0
    eklenen: int = 0
    guncellenen: int = 0
    atlanan_delta: int = 0
    planlanan: int = 0
    kalici_hata: int = 0
    retry_sayisi: int = 0
    token_alma_sayisi: int = 0
    api_istek_sayisi: int = 0
    toplam_sure_saniye: float = 0.0
    ortalama_api_suresi_ms: float | None = None
    en_hizli_api_suresi_ms: float | None = None
    en_yavas_api_suresi_ms: float | None = None
    dry_run: bool = False
    hatali_kayitlar_dosyasi: str = "hatali_kayitlar.csv"
    detaylar: list[dict[str, Any]] = field(default_factory=list)

    def kapat(self) -> None:
        self.bitis = _utc_now()

    def denge_kontrolu(self) -> bool:
        """okunan = elenen + islenen (+ planlanan dry-run)"""
        islenen = (
            self.eklenen
            + self.guncellenen
            + self.kalici_hata
            + self.atlanan_delta
            + self.planlanan
        )
        return self.okunan == self.elenen + islenen

    def ozet(self) -> dict[str, Any]:
        return {
            "baslangic": self.baslangic,
            "bitis": self.bitis,
            "dry_run": self.dry_run,
            "okunan": self.okunan,
            "elenen": self.elenen,
            "eklenen": self.eklenen,
            "guncellenen": self.guncellenen,
            "atlanan_delta": self.atlanan_delta,
            "planlanan": self.planlanan,
            "kalici_hata": self.kalici_hata,
            "retry_sayisi": self.retry_sayisi,
            "token_alma_sayisi": self.token_alma_sayisi,
            "api_istek_sayisi": self.api_istek_sayisi,
            "toplam_sure_saniye": round(self.toplam_sure_saniye, 3),
            "ortalama_api_suresi_ms": self.ortalama_api_suresi_ms,
            "en_hizli_api_suresi_ms": self.en_hizli_api_suresi_ms,
            "en_yavas_api_suresi_ms": self.en_yavas_api_suresi_ms,
            "denge_kontrolu": self.denge_kontrolu(),
            "hatali_kayitlar_dosyasi": self.hatali_kayitlar_dosyasi,
            "detaylar": self.detaylar,
        }


@dataclass
class ValidatedRecord:
    satir_no: int
    ham: dict[str, str]
    payload: dict[str, Any]
    payload_hash: str


@dataclass
class InvalidRecord:
    satir_no: int
    ham: dict[str, str]
    sebepler: list[str]


# --- Yardimcilar -------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _configure_logging(verbose: bool, log_file: Path) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(console)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root.addHandler(file_handler)


def _normalize_row(row: dict[str, str]) -> dict[str, str]:
    return {key: (value or "").strip() for key, value in row.items()}


def _row_signature(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(row.get(col, "") for col in CSV_COLUMNS)


def _payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_optional_int(value: str) -> int | None:
    if not value:
        return None
    return int(value)


def _validate_ip(value: str) -> bool:
    if not value:
        return True
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _validate_email(value: str) -> bool:
    if not value:
        return True
    return bool(EMAIL_RE.match(value))


CSV_COLUMNS = [
    "ci_name",
    "ci_type",
    "ip_address",
    "os",
    "owner_email",
    "ram_gb",
    "location",
]


# --- Dogrulama katmani (Gorev 2) ---------------------------------------------


def validate_inventory(
    rows: list[tuple[int, dict[str, str]]],
) -> tuple[list[ValidatedRecord], list[InvalidRecord]]:
    valid: list[ValidatedRecord] = []
    invalid: list[InvalidRecord] = []

    seen_exact: set[tuple[str, ...]] = set()
    seen_ci_names: dict[str, int] = {}

    for satir_no, raw in rows:
        row = _normalize_row(raw)
        sebepler: list[str] = []

        ci_name = row.get("ci_name", "")
        ci_type_raw = row.get("ci_type", "")
        ip_address = row.get("ip_address", "")
        os_name = row.get("os", "")
        owner_email = row.get("owner_email", "")
        ram_raw = row.get("ram_gb", "")
        location = row.get("location", "")

        if not ci_name:
            sebepler.append("bos_zorunlu_alan:ci_name")

        signature = _row_signature(row)
        exact_duplicate = signature in seen_exact
        if exact_duplicate:
            sebepler.append("birebir_tekrar_eden_satir")
        else:
            seen_exact.add(signature)

        if ci_name:
            norm_name = ci_name.strip().upper()
            if norm_name in seen_ci_names:
                if not exact_duplicate:
                    ilk = seen_ci_names[norm_name]
                    sebepler.append(
                        f"buyuk_kucuk_harf_farkiyla_duplicate_ci_name (ilk_satir:{ilk})"
                    )
            else:
                seen_ci_names[norm_name] = satir_no

        ci_type = ci_type_raw.lower() if ci_type_raw else ""
        if not ci_type:
            sebepler.append("bos_zorunlu_alan:ci_type")
        elif ci_type not in VALID_CI_TYPES:
            sebepler.append(
                f"gecersiz_ci_type:'{ci_type_raw}' (kabul_edilen:{sorted(VALID_CI_TYPES)})"
            )

        if not location:
            sebepler.append("bos_zorunlu_alan:location")

        if ip_address and not _validate_ip(ip_address):
            sebepler.append(f"gecersiz_ip_adresi:'{ip_address}'")

        if owner_email and not _validate_email(owner_email):
            sebepler.append(f"bozuk_e_posta:'{owner_email}'")

        ram_gb: int | None
        if not ram_raw:
            ram_gb = None
        else:
            try:
                ram_gb = _parse_optional_int(ram_raw)
                if ram_gb is None:
                    sebepler.append("ram_gb_sayisal_olmalı")
            except ValueError:
                sebepler.append(f"ram_gb_sayisal_olmalı:'{ram_raw}'")
                ram_gb = None

        if sebepler:
            invalid.append(InvalidRecord(satir_no=satir_no, ham=row, sebepler=sebepler))
            continue

        payload = {
            "ci_name": ci_name,
            "ci_type": ci_type,
            "ip_address": ip_address or None,
            "os": os_name or None,
            "owner_email": owner_email or None,
            "ram_gb": ram_gb,
            "location": location,
        }
        valid.append(
            ValidatedRecord(
                satir_no=satir_no,
                ham=row,
                payload=payload,
                payload_hash=_payload_hash(payload),
            )
        )

    return valid, invalid


def write_invalid_records(path: Path, records: list[InvalidRecord]) -> None:
    fieldnames = CSV_COLUMNS + ["satir_no", "sebep"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            row = {col: rec.ham.get(col, "") for col in CSV_COLUMNS}
            row["satir_no"] = rec.satir_no
            row["sebep"] = "; ".join(rec.sebepler)
            writer.writerow(row)


def read_csv(path: Path) -> list[tuple[int, dict[str, str]]]:
    rows: list[tuple[int, dict[str, str]]] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for idx, row in enumerate(reader, start=2):
            if not any((v or "").strip() for v in row.values()):
                continue
            rows.append((idx, row))
    return rows


# --- API istemcisi (Gorev 3 + 4) ---------------------------------------------


class CmdbClient:
    """Token yenileme, retry ve idempotency tek noktada."""

    def __init__(self, base_url: str, dry_run: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self.retry_count = 0
        self.token_fetch_count = 0
        self.request_durations_ms: list[float] = []
        self._session = requests.Session()

    def _fetch_token(self) -> None:
        logging.debug("Yeni token aliniyor...")
        started = time.perf_counter()
        response = self._session.post(
            f"{self.base_url}/api/token",
            json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
            timeout=30,
        )
        self.request_durations_ms.append((time.perf_counter() - started) * 1000)
        self.token_fetch_count += 1
        response.raise_for_status()
        data = response.json()
        self._token = data["token"]
        expires_in = int(data.get("expires_in", 60))
        self._token_expires_at = time.time() + expires_in
        logging.info("Token alindi (gecerlilik: %s sn)", expires_in)

    def _ensure_token(self) -> str:
        if (
            self._token is None
            or time.time() >= self._token_expires_at - TOKEN_REFRESH_BUFFER_SECONDS
        ):
            self._fetch_token()
        assert self._token is not None
        return self._token

    def api_istek(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        *,
        auth_retry: bool = True,
    ) -> requests.Response:
        """Token yenileme + 503 exponential backoff ile tek giris noktasi."""
        if self.dry_run:
            logging.info("[DRY-RUN] %s %s body=%s", method, path, json_body)
            fake = requests.Response()
            fake.status_code = 201 if method.upper() == "POST" else 200
            fake._content = b'{"dry_run": true}'
            return fake

        url = f"{self.base_url}{path}"
        last_response: requests.Response | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {"Authorization": f"Bearer {self._ensure_token()}"}
            started = time.perf_counter()
            response = self._session.request(
                method,
                url,
                json=json_body,
                headers=headers,
                timeout=30,
            )
            self.request_durations_ms.append((time.perf_counter() - started) * 1000)
            last_response = response

            if response.status_code == 401 and auth_retry:
                logging.warning("401 alindi — token yenileniyor ve istek tekrarlaniyor")
                self._token = None
                self._fetch_token()
                return self.api_istek(
                    method, path, json_body, auth_retry=False
                )

            if response.status_code == 503 and attempt < MAX_ATTEMPTS:
                wait = RETRY_BACKOFF_SECONDS[attempt - 1]
                self.retry_count += 1
                logging.warning(
                    "503 (deneme %s/%s) — %s sn bekleniyor",
                    attempt,
                    MAX_ATTEMPTS,
                    wait,
                )
                time.sleep(wait)
                continue

            return response

        assert last_response is not None
        return last_response

    def upsert_ci(self, payload: dict[str, Any]) -> str:
        """POST dener; 409 ise PUT ile gunceller (idempotent)."""
        name = payload["ci_name"]
        response = self.api_istek("POST", "/api/ci", payload)

        if response.status_code == 201:
            return "created"
        if response.status_code == 409:
            logging.info("409 — %s zaten var, PUT ile guncelleniyor", name)
            put_response = self.api_istek("PUT", f"/api/ci/{name}", payload)
            if put_response.status_code == 200:
                return "updated"
            put_response.raise_for_status()

        if response.status_code >= 400:
            logging.error(
                "Kalici API hatasi %s %s: %s",
                response.status_code,
                name,
                response.text,
            )
            response.raise_for_status()

        return "updated"


# --- Delta sync (Bonus) ------------------------------------------------------


def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_state(path: Path, state: dict[str, str]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)


# --- Ana akis ----------------------------------------------------------------


def run_sync(
    csv_path: Path,
    base_url: str,
    *,
    dry_run: bool = False,
    delta_sync: bool = True,
    invalid_out: Path = Path("hatali_kayitlar.csv"),
    report_out: Path = Path("sync_raporu.json"),
    state_path: Path = Path("sync_state.json"),
) -> SyncReport:
    started = time.perf_counter()
    report = SyncReport(dry_run=dry_run)
    client = CmdbClient(base_url, dry_run=dry_run)
    state = load_state(state_path) if delta_sync and not dry_run else {}

    rows = read_csv(csv_path)
    report.okunan = len(rows)
    logging.info("CSV okundu: %s satir", report.okunan)

    valid, invalid = validate_inventory(rows)
    report.elenen = len(invalid)
    write_invalid_records(invalid_out, invalid)
    logging.info(
        "Dogrulama tamam: %s gecerli, %s elendi -> %s",
        len(valid),
        len(invalid),
        invalid_out,
    )

    for record in valid:
        name = record.payload["ci_name"]
        try:
            if delta_sync and state.get(name) == record.payload_hash:
                report.atlanan_delta += 1
                logging.info("Delta: degismedi, atlandi — %s", name)
                report.detaylar.append(
                    {"ci_name": name, "durum": "atlandi_delta", "satir_no": record.satir_no}
                )
                continue

            if dry_run:
                action = "dry_run_create"
                report.planlanan += 1
                logging.info("[DRY-RUN] Gonderilecek: %s", name)
            else:
                action = client.upsert_ci(record.payload)
                if action == "created":
                    report.eklenen += 1
                else:
                    report.guncellenen += 1
                state[name] = record.payload_hash

            report.detaylar.append(
                {
                    "ci_name": name,
                    "durum": action,
                    "satir_no": record.satir_no,
                }
            )
        except requests.RequestException as exc:
            report.kalici_hata += 1
            logging.error("Kalici hata — %s: %s", name, exc)
            report.detaylar.append(
                {
                    "ci_name": name,
                    "durum": "kalici_hata",
                    "satir_no": record.satir_no,
                    "hata": str(exc),
                }
            )

    report.retry_sayisi = client.retry_count
    report.token_alma_sayisi = client.token_fetch_count
    report.api_istek_sayisi = len(client.request_durations_ms)
    report.toplam_sure_saniye = time.perf_counter() - started
    if client.request_durations_ms:
        report.ortalama_api_suresi_ms = round(
            sum(client.request_durations_ms) / len(client.request_durations_ms), 2
        )
        report.en_hizli_api_suresi_ms = round(min(client.request_durations_ms), 2)
        report.en_yavas_api_suresi_ms = round(max(client.request_durations_ms), 2)
    if delta_sync and not dry_run:
        save_state(state_path, state)

    report.kapat()
    with report_out.open("w", encoding="utf-8") as fh:
        json.dump(report.ozet(), fh, indent=2, ensure_ascii=False)

    logging.info("Rapor yazildi: %s", report_out)
    _print_summary(report)
    return report


def _print_summary(report: SyncReport) -> None:
    print()
    print("=" * 56)
    print("  SYNC OZETI")
    print("=" * 56)
    print(f"  Okunan satir        : {report.okunan}")
    print(f"  Elenen (dogrulama)  : {report.elenen}")
    print(f"  Yeni eklenen        : {report.eklenen}")
    print(f"  Guncellenen         : {report.guncellenen}")
    print(f"  Delta atlanan       : {report.atlanan_delta}")
    if report.planlanan:
        print(f"  Planlanan (dry-run) : {report.planlanan}")
    print(f"  Kalici hata         : {report.kalici_hata}")
    print(f"  Retry sayisi        : {report.retry_sayisi}")
    print(f"  Token alma sayisi   : {report.token_alma_sayisi}")
    print(f"  API istek sayisi    : {report.api_istek_sayisi}")
    print(f"  Toplam sure (sn)    : {report.toplam_sure_saniye:.3f}")
    if report.ortalama_api_suresi_ms is not None:
        print(f"  Ortalama API (ms)   : {report.ortalama_api_suresi_ms:.2f}")
        print(f"  En hizli API (ms)   : {report.en_hizli_api_suresi_ms:.2f}")
        print(f"  En yavas API (ms)   : {report.en_yavas_api_suresi_ms:.2f}")
    print(f"  Denge kontrolu      : {'OK' if report.denge_kontrolu() else 'HATA'}")
    if report.dry_run:
        print("  Mod                 : DRY-RUN (API'ye istek gonderilmedi)")
    print("=" * 56)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CSV envanterini hedef CMDB API'sine guvenilir sekilde aktarir.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("envanter.csv"),
        help="Kaynak CSV dosyasi (varsayilan: envanter.csv)",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Hedef API adresi (varsayilan: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="API'ye istek gondermeden ne yapilacagini raporla",
    )
    parser.add_argument(
        "--no-delta",
        action="store_true",
        help="Delta sync'i kapat (her calismada tum kayitlari gonder)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Konsola DEBUG seviyesinde log yaz",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _configure_logging(args.verbose, Path("sync.log"))

    if not args.csv.exists():
        logging.error("CSV bulunamadi: %s", args.csv)
        return 1

    try:
        report = run_sync(
            args.csv,
            args.base_url,
            dry_run=args.dry_run,
            delta_sync=not args.no_delta,
        )
    except requests.RequestException as exc:
        logging.exception("Sync basarisiz: %s", exc)
        return 1

    if not report.denge_kontrolu():
        logging.error("Denge kontrolu basarisiz — kayit sayimini inceleyin")
        return 1

    if report.kalici_hata > 0:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
