# Ekstra Çalışmalar Raporu — Challenge 7 Sync Motoru

**Hazırlayan:** Beyza Yılmaz  
**Proje:** CSV → CMDB Sync Motoru  
**Repo:** https://github.com/beyzayilmaz1/konsalt-challenge-07-sync-motoru  
**Tarih:** 28 Temmuz 2026

---

## Amaç

Bu belge, challenge'ın zorunlu görevleri (Görev 1–5) ve resmi bonus maddelerinin (`--dry-run`, delta sync, logging) dışında projeye eklenen çalışmaları özetler. Ana kullanım kılavuzu `README.md` dosyasındadır; bu rapor yalnızca **ek katkıları** belgelemek içindir.

---

## Zorunlu ve Resmi Bonus Özeti (Kısa)

| Kapsam | Durum |
|--------|-------|
| Görev 1–5 (doğrulama, token, retry, idempotency, rapor) | Tamamlandı |
| Resmi bonus: dry-run | Tamamlandı |
| Resmi bonus: delta sync | Tamamlandı |
| Resmi bonus: logging (INFO / DEBUG) | Tamamlandı |

---

## Ekstra Çalışmalar

### 1. GitHub Actions CI

**Dosya:** `.github/workflows/ci.yml`

Her `push` ve `pull_request` olayında otomatik test pipeline'ı çalışır:

- Ubuntu üzerinde Python 3.11 kurulumu
- `requirements.txt` bağımlılıklarının yüklenmesi
- `python -m unittest discover -s tests -v` ile birim testlerinin koşturulması

**Neden eklendi?** Gerçek entegrasyon projelerinde kod değişikliği sonrası regresyon kontrolü standarttır. Manuel teste ek olarak, repoya her commit atıldığında temel senaryoların otomatik doğrulanması hedeflendi.

---

### 2. Birim Testleri

**Dosya:** `tests/test_sync.py`

Mock tabanlı 5 test senaryosu yazıldı:

| Test | Ne doğrulanıyor |
|------|-----------------|
| `test_validate_inventory_filters_dirty_row` | Kirli satırdaki tüm doğrulama hataları |
| `test_api_istek_refreshes_token_after_401` | 401 sonrası token yenileme ve istek tekrarı |
| `test_api_istek_retries_on_503_with_backoff` | 503'te 1s → 2s backoff ile yeniden deneme |
| `test_upsert_ci_falls_back_to_put_on_conflict` | 409 → PUT idempotent akışı |
| `test_run_sync_skips_unchanged_records_on_second_delta_run` | Delta sync ile ikinci çalışmada atlama |

**Neden eklendi?** API simülasyonu rastgele 503 ürettiği için bazı davranışlar her çalışmada farklı görünebilir. Testler, kritik mantığın deterministik ve tekrarlanabilir şekilde doğrulanmasını sağlar.

---

### 3. Performans ve Operasyon Metrikleri

**Dosya:** `sync.py` → `SyncReport`, `sync_raporu.json`, konsol özeti

Standart sayaçlara ek olarak şu metrikler raporlanır:

- `token_alma_sayisi`
- `api_istek_sayisi`
- `toplam_sure_saniye`
- `ortalama_api_suresi_ms`
- `en_hizli_api_suresi_ms`
- `en_yavas_api_suresi_ms`

**Neden eklendi?** Üretim ortamındaki entegrasyon araçlarında "kaç API çağrısı yapıldı?", "ne kadar sürdü?", "yavaş istek var mı?" sorularına cevap vermek operasyonel izleme için gereklidir.

---

### 4. Genişletilmiş Komut Satırı Arayüzü

**Dosya:** `sync.py` → `parse_args()`

Resmi `--dry-run` dışında ek CLI seçenekleri:

| Parametre | İşlev |
|-----------|-------|
| `--csv yol.csv` | Farklı kaynak CSV ile çalışma |
| `--base-url http://host:5050` | Farklı hedef API adresi |
| `--no-delta` | Delta sync'i kapatma (tüm kayıtları yeniden gönderme) |
| `-v` / `--verbose` | Konsolda DEBUG seviyesi log |

**Neden eklendi?** Aynı aracın geliştirme, test ve farklı ortamlarda (local/staging) tekrar kullanılabilmesi için esneklik sağlar.

---

### 5. Proaktif Token Yenileme

**Dosya:** `sync.py` → `CmdbClient._ensure_token()`

Challenge yalnızca 401 alındığında token yenilemeyi gerektirir. Ek olarak token süresi dolmadan **5 saniye önce** proaktif yenileme uygulanır (`TOKEN_REFRESH_BUFFER_SECONDS`).

**Neden eklendi?** 32+ kayıt ve retry gecikmeleriyle toplam süre 60 saniyeyi aşabilir. Proaktif yenileme, gereksiz 401 hatalarını ve ekstra istek maliyetini azaltır.

---

### 6. Satır Bazlı Detay Raporu

**Dosya:** `sync_raporu.json` → `detaylar` alanı

Özet sayaçlara ek olarak her geçerli kayıt için:

- `ci_name`
- `satir_no`
- `durum` (`created`, `updated`, `atlandi_delta`, `kalici_hata`, `dry_run_create`)

**Neden eklendi?** Müşteriye yalnızca "32 kayıt işlendi" demek yerine, hangi satırın ne olduğunu satır numarasıyla göstermek denetlenebilirlik sağlar.

---

### 7. Repo Hijyeni

**Dosya:** `.gitignore`

Çalışma anında oluşan geçici dosyalar repoya dahil edilmez:

- `sync_state.json` (delta sync state)
- `sync.log` (debug log)
- `__pycache__/`, `.venv/` vb.

**Neden eklendi?** Teslim edilen repoda yalnızca kaynak kod ve anlamlı çıktı dosyaları kalmalı; ortam-spesifik artefaktlar commit edilmemelidir.

---

## Teslim Dosyaları ile İlişki

 Paylaşılacak ana teslim seti:

| Dosya | Rol |
|-------|-----|
| `sync.py` | Ana sync aracı |
| `hatali_kayitlar.csv` | Doğrulamada elenen 6 kayıt |
| `sync_raporu.json` | İşlem özeti (ilk temiz çalıştırma) |
| `README.md` | Mimari ve kullanım dokümanı |

Bu rapor (`EKSTRA_CALISMALAR.md`) ise yukarıdaki setin **tamamlayıcısıdır**; zorunlu teslim parçası değildir.

---

## Sonuç

Proje, challenge gereksinimlerinin tamamını karşılamanın ötesinde:

- otomatik CI,
- birim testleri,
- operasyonel metrikler,
- esnek CLI,
- proaktif token yönetimi,
- satır bazlı detay raporu

gibi gerçek dünya entegrasyon pratiklerine yakın ek katmanlar içermektedir. Bu ekstralar kod kalitesini, tekrarlanabilirliği ve operasyonel şeffaflığı artırmak amacıyla bilinçli olarak eklenmiştir.
