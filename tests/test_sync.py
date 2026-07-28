import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import sync


def make_response(status_code, payload=None, text=""):
    response = requests.Response()
    response.status_code = status_code
    response._content = (
        b"" if payload is None else sync.json.dumps(payload).encode("utf-8")
    )
    response.encoding = "utf-8"
    response.url = "http://test.local/api"
    if text:
        response._content = text.encode("utf-8")
    return response


class SyncTests(unittest.TestCase):
    def test_validate_inventory_filters_dirty_row(self):
        rows = [
            (
                2,
                {
                    "ci_name": "",
                    "ci_type": "storage",
                    "ip_address": "10.20.1.999",
                    "os": "Linux",
                    "owner_email": "not-an-email",
                    "ram_gb": "abc",
                    "location": "",
                },
            )
        ]

        valid, invalid = sync.validate_inventory(rows)

        self.assertEqual(valid, [])
        self.assertEqual(len(invalid), 1)
        self.assertIn("bos_zorunlu_alan:ci_name", invalid[0].sebepler)
        self.assertIn("bos_zorunlu_alan:location", invalid[0].sebepler)
        self.assertTrue(
            any("gecersiz_ci_type" in sebep for sebep in invalid[0].sebepler)
        )
        self.assertIn("gecersiz_ip_adresi:'10.20.1.999'", invalid[0].sebepler)
        self.assertIn("bozuk_e_posta:'not-an-email'", invalid[0].sebepler)
        self.assertIn("ram_gb_sayisal_olmalı:'abc'", invalid[0].sebepler)

    def test_upsert_ci_falls_back_to_put_on_conflict(self):
        client = sync.CmdbClient("http://test.local")
        payload = {"ci_name": "IST-WEB01"}

        with mock.patch.object(
            client,
            "api_istek",
            side_effect=[make_response(409), make_response(200)],
        ) as api_istek:
            result = client.upsert_ci(payload)

        self.assertEqual(result, "updated")
        self.assertEqual(api_istek.call_count, 2)
        self.assertEqual(api_istek.call_args_list[0].args, ("POST", "/api/ci", payload))
        self.assertEqual(
            api_istek.call_args_list[1].args,
            ("PUT", "/api/ci/IST-WEB01", payload),
        )

    def test_api_istek_refreshes_token_after_401(self):
        client = sync.CmdbClient("http://test.local")
        request_responses = [make_response(401), make_response(201)]
        tokens = iter(["token-1", "token-2"])

        def fake_fetch_token():
            client._token = next(tokens)
            client._token_expires_at = 9999999999

        with (
            mock.patch.object(client, "_fetch_token", side_effect=fake_fetch_token) as fetch,
            mock.patch.object(
                client._session, "request", side_effect=request_responses
            ) as request_mock,
        ):
            response = client.api_istek("POST", "/api/ci", {"ci_name": "IST-WEB01"})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(request_mock.call_count, 2)
        first_headers = request_mock.call_args_list[0].kwargs["headers"]
        second_headers = request_mock.call_args_list[1].kwargs["headers"]
        self.assertEqual(first_headers["Authorization"], "Bearer token-1")
        self.assertEqual(second_headers["Authorization"], "Bearer token-2")
        self.assertEqual(len(client.request_durations_ms), 2)

    def test_api_istek_retries_on_503_with_backoff(self):
        client = sync.CmdbClient("http://test.local")
        client._token = "stable-token"
        client._token_expires_at = 9999999999

        with (
            mock.patch.object(
                client._session,
                "request",
                side_effect=[make_response(503), make_response(503), make_response(201)],
            ) as request_mock,
            mock.patch("sync.time.sleep") as sleep_mock,
        ):
            response = client.api_istek("POST", "/api/ci", {"ci_name": "IST-WEB01"})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(request_mock.call_count, 3)
        self.assertEqual(client.retry_count, 2)
        self.assertEqual(len(client.request_durations_ms), 3)
        sleep_mock.assert_has_calls([mock.call(1), mock.call(2)])

    def test_run_sync_skips_unchanged_records_on_second_delta_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            csv_path = temp_path / "envanter.csv"
            invalid_out = temp_path / "hatali_kayitlar.csv"
            report_out = temp_path / "sync_raporu.json"
            state_path = temp_path / "sync_state.json"

            with csv_path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=sync.CSV_COLUMNS)
                writer.writeheader()
                writer.writerow(
                    {
                        "ci_name": "IST-WEB01",
                        "ci_type": "server",
                        "ip_address": "10.0.0.1",
                        "os": "Ubuntu",
                        "owner_email": "ops@example.com",
                        "ram_gb": "8",
                        "location": "Istanbul-DC",
                    }
                )

            with (
                mock.patch.object(sync.CmdbClient, "upsert_ci", return_value="created") as upsert,
                mock.patch("sync._print_summary"),
            ):
                first_report = sync.run_sync(
                    csv_path,
                    "http://test.local",
                    invalid_out=invalid_out,
                    report_out=report_out,
                    state_path=state_path,
                )

            self.assertEqual(first_report.eklenen, 1)
            self.assertEqual(first_report.atlanan_delta, 0)
            self.assertEqual(upsert.call_count, 1)
            self.assertGreaterEqual(first_report.toplam_sure_saniye, 0)
            self.assertEqual(first_report.api_istek_sayisi, 0)
            self.assertEqual(first_report.token_alma_sayisi, 0)

            with (
                mock.patch.object(sync.CmdbClient, "upsert_ci") as second_upsert,
                mock.patch("sync._print_summary"),
            ):
                second_report = sync.run_sync(
                    csv_path,
                    "http://test.local",
                    invalid_out=invalid_out,
                    report_out=report_out,
                    state_path=state_path,
                )

            self.assertEqual(second_report.eklenen, 0)
            self.assertEqual(second_report.atlanan_delta, 1)
            self.assertGreaterEqual(second_report.toplam_sure_saniye, 0)
            self.assertEqual(second_report.api_istek_sayisi, 0)
            second_upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
