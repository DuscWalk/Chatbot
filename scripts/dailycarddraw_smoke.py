"""Check the card backend with synthetic users; clean up every inserted test row."""

import argparse
import json
import struct
import subprocess
import uuid
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from manage_dailycarddraw import ROOT, compose, import_catalog
from manage_plugins import read, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        action="store_true",
        help="Import the full catalog (disposable CI database only)",
    )
    args = parser.parse_args()
    credentials = read(ROOT / "runtime/dailycarddraw/credentials.json")
    opener = build_opener(ProxyHandler({}))
    base = "http://127.0.0.1:3100"
    test_id = "smoke-" + uuid.uuid4().hex[:20]
    card_key = test_id

    def request(path, body=None, token=None, method=None, raw=False):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with opener.open(req, timeout=20) as response:
                data = response.read()
                return response.status, data if raw else json.loads(data)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    checks = {}
    token = ""
    try:
        status, result = request("/manage/api/overview")
        assert status == 401
        status, result = request("/api/daily-carddraw/admin/pools")
        assert status == 401
        checks["admin_authentication"] = True
        status, result = request(
            "/manage/api/login",
            {"username": credentials["panel_username"], "password": credentials["panel_password"]},
        )
        assert status == 200 and result["success"]
        token = result["data"]["token"]
        status, result = request("/manage/api/cards", token=token)
        assert status == 200 and result["success"]
        checks["panel_login_and_cards"] = True
        status, result = request(
            "/manage/api/cards/import",
            {"cards": [{"id": card_key, "name": "合成测试卡", "rarity": 3, "profession": "术师"}]},
            token=token,
        )
        assert status == 200 and result["data"]["created"] == 1
        checks["card_import"] = True
        if args.catalog:
            import_catalog()
        for mode, width in (("single", 320), ("ten", 3200)):
            payload = {
                "qq_id": test_id,
                "nickname": "Synthetic integration check",
                "group_id": "synthetic-" + mode,
                "pool_key": "normal_pool",
                "draw_mode": mode,
            }
            status, result = request("/api/daily-carddraw/draw", payload, credentials["api_token"])
            assert status == 200 and result["success"], ("draw", status)
            data = result["data"]
            assert len(data["cards"]) == (1 if mode == "single" else 10)
            status, image = request(data["image_url"], raw=True)
            assert status == 200 and image[:8] == b"\x89PNG\r\n\x1a\n"
            assert struct.unpack(">II", image[16:24]) == (width, 480)
            payload["group_id"] = "synthetic-other-group"
            status, result = request("/api/daily-carddraw/draw", payload, credentials["api_token"])
            assert 400 <= status < 500 and not result["success"], "daily quota was not enforced"
        checks["single_ten_images_and_shared_daily_quota"] = True
        if args.catalog:
            import_catalog()
            imported = read(ROOT / "runtime/dailycarddraw/latest-import.json")
            assert imported["created"] == 0 and imported["enabled_cards"] == imported["cards"]
            for mode in ("single", "ten"):
                payload["draw_mode"] = mode
                status, result = request(
                    "/api/daily-carddraw/draw", payload, credentials["api_token"]
                )
                assert 400 <= status < 500 and not result["success"], "import reset used quota"
            checks["full_catalog_reimport_preserves_ids_and_used_quotas"] = True
        status, result = request("/api/daily-carddraw/history?qq_id=" + test_id)
        assert status == 200 and result["success"] and result["data"]["total"] == 2
        status, result = request("/api/daily-carddraw/stats?qq_id=" + test_id)
        assert status == 200 and result["success"]
        assert result["data"]["total_single_draw_count"] == 1
        assert result["data"]["total_ten_draw_count"] == 1
        checks["history_and_statistics"] = True
    finally:
        # No real QQ identifiers are accepted here; only this run's freshly generated ID.
        cleanup = r"""
const mysql = require('mysql2/promise');
const {config} = require('./src/config');
(async () => {
  const id = process.argv[1];
  if (!/^smoke-[a-f0-9]{20}$/.test(id)) throw new Error('Unsafe test identifier');
  const conn = await mysql.createConnection(config.db);
  try {
    await conn.beginTransaction();
    await conn.execute('DELETE FROM draw_record_item WHERE record_id IN (SELECT id FROM draw_record WHERE qq_id=?)',[id]);
    for (const table of ['draw_record','daily_quota','user_profile']) {
      await conn.execute('DELETE FROM ' + table + ' WHERE qq_id=?',[id]);
    }
    await conn.execute('DELETE FROM card_item WHERE card_key=?',[id]);
    await conn.commit();
    const [rows] = await conn.execute('SELECT COUNT(*) n FROM user_profile WHERE qq_id=?',[id]);
    if (rows[0].n !== 0) throw new Error('Test cleanup incomplete');
  } finally { await conn.end(); }
})().catch(() => process.exit(1));
"""
        subprocess.run(
            [*compose(), "exec", "-T", "backend", "node", "-e", cleanup, test_id],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    checks["synthetic_rows_removed"] = True
    checks["qq_sends"] = 0
    write(ROOT / "runtime/dailycarddraw/latest-check.json", checks)
    print(json.dumps(checks, ensure_ascii=False))


if __name__ == "__main__":
    main()
