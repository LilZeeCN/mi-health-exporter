#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mi-Health-Exporter (sync_cloud.py)
零第三方依赖（纯 Python 标准库）的小米运动健康云端增量同步与 CSV 导出工具。
无需 Root、无需开启 USB 调试或手动手机备份，支持一键拉取心率、步数、卡路里、
睡眠、血氧、压力、有效站立、体重、耳机听力及运动记录，并输出 AI-Ready 的标准 CSV。
"""

import argparse
import base64
import csv
import getpass
import hashlib
import json
import os
import sqlite3
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "xiaomi_token.json"
DATA_DIR = BASE_DIR / "data"
ANALYSIS_DIR = BASE_DIR / "analysis"
FITNESS_DB = DATA_DIR / "fitness_data.db"
SUMMARY_DB = DATA_DIR / "fitness_summary.db"

TZ_BEIJING = timezone(timedelta(hours=8))
HLTH_BASE_URL = "https://hlth.io.mi.com"


class RC4Drop1024:
    """标准 RC4-Drop1024 流密码实现（初始化密钥流后丢弃前 1024 字节）"""

    def __init__(self, key_bytes: bytes):
        S = list(range(256))
        j = 0
        klen = len(key_bytes)
        for i in range(256):
            j = (j + S[i] + key_bytes[i % klen]) & 0xFF
            S[i], S[j] = S[j], S[i]
        self.S = S
        self.i = 0
        self.j = 0
        self.crypt(bytes(1024))

    def crypt(self, data: bytes) -> bytes:
        S = self.S
        i, j = self.i, self.j
        out = bytearray(len(data))
        for idx, b in enumerate(data):
            i = (i + 1) & 0xFF
            j = (j + S[i]) & 0xFF
            S[i], S[j] = S[j], S[i]
            out[idx] = b ^ S[(S[i] + S[j]) & 0xFF]
        self.i, self.j = i, j
        return bytes(out)


class XiaomiHealthCloudClient:
    """小米运动健康云端同步客户端"""

    def __init__(self, token_data: dict):
        self.user_id = str(token_data.get("userId", ""))
        self.c_user_id = str(token_data["cUserId"])
        self.service_token = str(token_data["serviceToken"])
        self.ssecurity = str(token_data["ssecurity"])
        self.sid = str(token_data.get("sid", "miothealth"))

    @staticmethod
    def _gen_nonce() -> str:
        rand_bytes = os.urandom(8)
        minutes = int(time.time() // 60)
        return base64.b64encode(rand_bytes + struct.pack(">i", minutes)).decode("ascii")

    @staticmethod
    def _get_signed_nonce(ssecurity: str, nonce: str) -> str:
        raw = base64.b64decode(ssecurity) + base64.b64decode(nonce)
        return base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")

    @staticmethod
    def _gen_sha1_signature(method: str, uri_path: str, params: dict, signed_nonce: str) -> str:
        parts = [method.upper(), uri_path]
        for k in sorted(params.keys()):
            parts.append(f"{k}={params[k]}")
        parts.append(signed_nonce)
        joined = "&".join(parts)
        return base64.b64encode(hashlib.sha1(joined.encode("utf-8")).digest()).decode("ascii")

    def post(self, uri_path: str, data_dict: dict) -> dict:
        nonce = self._gen_nonce()
        snonce = self._get_signed_nonce(self.ssecurity, nonce)
        snonce_bytes = base64.b64decode(snonce)

        params = {"data": json.dumps(data_dict, separators=(",", ":"))}
        params["rc4_hash__"] = self._gen_sha1_signature("POST", uri_path, params, snonce)

        rc4 = RC4Drop1024(snonce_bytes)
        enc_params = {}
        for k in sorted(params.keys()):
            enc_val = base64.b64encode(rc4.crypt(params[k].encode("utf-8"))).decode("ascii")
            enc_params[k] = enc_val

        enc_params["signature"] = self._gen_sha1_signature("POST", uri_path, enc_params, snonce)
        enc_params["_nonce"] = nonce

        body = urllib.parse.urlencode(enc_params).encode("utf-8")
        url = HLTH_BASE_URL + uri_path
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("X-XIAOMI-PROTOCAL-FLAG-CLI", "PROTOCAL-HTTP2")
        req.add_header(
            "Cookie",
            f"cUserId={self.c_user_id}; serviceToken={self.service_token}; locale=zh_CN",
        )
        req.add_header("User-Agent", "Android-3.59.1")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw_resp = resp.read().decode("utf-8")
                dec_bytes = RC4Drop1024(snonce_bytes).crypt(base64.b64decode(raw_resp))
                res = json.loads(dec_bytes.decode("utf-8"))
                if res.get("code") != 0:
                    raise RuntimeError(f"云端接口返回错误: {res}")
                return res.get("result") or {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise PermissionError(
                    "HTTP 401: serviceToken 已过期，请运行 `python3 sync_cloud.py --login` 重新登录刷新凭证。"
                ) from e
            raise RuntimeError(f"HTTP {e.code}: {err_body}") from e

    def fetch_raw_by_time(self, key: str, start_time: int, end_time: int) -> list:
        all_items = []
        next_key = ""
        while True:
            payload = {
                "start_time": int(start_time),
                "end_time": int(end_time),
                "key": key,
            }
            if next_key:
                payload["next_key"] = next_key
            res = self.post("/app/v1/data/get_fitness_data_by_time", payload)
            items = res.get("data_list") or []
            all_items.extend(items)
            if not res.get("has_more") or not res.get("next_key"):
                break
            next_key = res["next_key"]
        return all_items

    def fetch_daily_report(self, key: str, start_time: int, end_time: int, limit: int = 100) -> list:
        payload = {
            "tag": "daily_report",
            "key": key,
            "start_time": int(start_time),
            "end_time": int(end_time),
            "reverse": True,
            "limit": limit,
        }
        res = self.post("/app/v1/data/get_aggregated_fitness_data_by_time", payload)
        items = res.get("data_list") or []
        return [x for x in items if x.get("time", 0) >= start_time]

    def fetch_sport_records(self, watermark: int = 0) -> tuple[list, int]:
        all_records = []
        cur_wm = watermark
        while True:
            payload = {
                "watermark": int(cur_wm),
                "limit": 50,
                "phone_id": "",
                "with_cheat_info": True,
            }
            res = self.post("/app/v1/data/get_sport_records_by_watermark", payload)
            records = res.get("sport_records") or []
            all_records.extend(records)
            new_wm = res.get("watermark", cur_wm)
            if new_wm:
                cur_wm = new_wm
            if not res.get("has_more") or not records:
                break
        return all_records, cur_wm


def init_local_dbs():
    """首次运行时自动初始化本地 SQLite 数据库表结构"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn_fit = sqlite3.connect(FITNESS_DB)
    std_tables = [
        "hr_record",
        "step_record",
        "calorie_record",
        "spo2_record",
        "stress_record",
        "stand_record",
        "hearing_health_record",
    ]
    for t in std_tables:
        conn_fit.execute(
            f"""CREATE TABLE IF NOT EXISTS `{t}` (
                `key` TEXT NOT NULL,
                `sid` TEXT NOT NULL,
                `time` INTEGER NOT NULL,
                `value` TEXT,
                `zoneOffsetInSec` INTEGER NOT NULL,
                `zoneName` TEXT,
                `timeIn0Tz` INTEGER NOT NULL,
                `isUpload` INTEGER NOT NULL,
                `isDeleted` INTEGER NOT NULL,
                `endTime` INTEGER NOT NULL DEFAULT 0,
                `endTimeIn0Tz` INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(`key`, `sid`, `time`)
            )"""
        )
    conn_fit.execute(
        """CREATE TABLE IF NOT EXISTS `sleep_segment` (
            `key` TEXT NOT NULL,
            `sid` TEXT NOT NULL,
            `time` INTEGER NOT NULL,
            `isComplete` INTEGER NOT NULL,
            `value` TEXT,
            `zoneOffsetInSec` INTEGER NOT NULL,
            `zoneName` TEXT,
            `timeIn0Tz` INTEGER NOT NULL,
            `isUpload` INTEGER NOT NULL,
            `isDeleted` INTEGER NOT NULL,
            `endTime` INTEGER NOT NULL DEFAULT 0,
            `endTimeIn0Tz` INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(`key`, `sid`, `time`)
        )"""
    )
    conn_fit.execute(
        """CREATE TABLE IF NOT EXISTS `weight_item_record` (
            `key` TEXT NOT NULL,
            `sid` TEXT NOT NULL,
            `time` INTEGER NOT NULL,
            `zeroTimeIn0Tz` INTEGER NOT NULL,
            `weight` REAL NOT NULL,
            `bmi` REAL,
            `bodyFatRate` REAL,
            `moistureRate` REAL,
            `boneMass` REAL,
            `basalMetabolism` INTEGER,
            `muscleMass` REAL,
            `proteinRate` REAL,
            `visceralFat` REAL,
            `value` TEXT,
            `zoneOffsetInSec` INTEGER NOT NULL,
            `zoneName` TEXT,
            `timeIn0Tz` INTEGER NOT NULL,
            `isUpload` INTEGER NOT NULL,
            `isDeleted` INTEGER NOT NULL,
            `endTime` INTEGER NOT NULL DEFAULT 0,
            `endTimeIn0Tz` INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(`key`, `sid`, `time`)
        )"""
    )
    conn_fit.execute(
        """CREATE TABLE IF NOT EXISTS `sport_report` (
            `sid` TEXT NOT NULL,
            `key` TEXT NOT NULL,
            `time` INTEGER NOT NULL,
            `category` TEXT,
            `zoneOffsetInSec` INTEGER NOT NULL,
            `zoneName` TEXT,
            `value` TEXT,
            `timeIn0Tz` INTEGER NOT NULL,
            `isUpload` INTEGER NOT NULL,
            `isDeleted` INTEGER NOT NULL,
            `offlineCounted` INTEGER NOT NULL,
            `antiCheckResult` TEXT,
            PRIMARY KEY(`sid`, `key`, `time`)
        )"""
    )
    conn_fit.execute(
        """CREATE TABLE IF NOT EXISTS `server_request` (
            `action` TEXT NOT NULL,
            `key` TEXT NOT NULL,
            `watermark` INTEGER NOT NULL,
            `startTime` INTEGER NOT NULL,
            `endTime` INTEGER NOT NULL,
            `nextKey` TEXT,
            `watermarkStr` TEXT,
            `kitWatermark` TEXT,
            PRIMARY KEY(`action`, `key`, `endTime`)
        )"""
    )
    conn_fit.execute(
        """INSERT OR IGNORE INTO `server_request`
           (`action`, `key`, `watermark`, `startTime`, `endTime`)
           VALUES ('incremental_pull', 'sport_report', 0, 0, 0)"""
    )
    conn_fit.commit()
    conn_fit.close()

    conn_sum = sqlite3.connect(SUMMARY_DB)
    conn_sum.execute(
        """CREATE TABLE IF NOT EXISTS `daily_report` (
            `sid` TEXT NOT NULL,
            `dataType` TEXT NOT NULL,
            `updateTime` INTEGER NOT NULL,
            `timeInZero` INTEGER NOT NULL,
            `viewTag` TEXT NOT NULL,
            `value` TEXT NOT NULL,
            `isUpload` INTEGER NOT NULL,
            `isDeleted` INTEGER NOT NULL,
            PRIMARY KEY(`dataType`, `sid`, `timeInZero`, `viewTag`)
        )"""
    )
    conn_sum.commit()
    conn_sum.close()


def interactive_xiaomi_login() -> dict:
    """通过小米账号通行证 (sid=miothealth) 登录获取会话凭证"""
    print("=== 小米运动健康 (miothealth) 云端账号登录 ===")
    user = input("请输入小米账号 (手机号/邮箱/小米ID): ").strip()
    pwd = getpass.getpass("请输入小米账号密码 (本地 MD5 摘要，不落盘): ")

    cj = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(cj)
    headers = {"User-Agent": "Android-3.59.1"}

    url1 = "https://account.xiaomi.com/pass/serviceLogin?sid=miothealth&_json=true"
    req1 = urllib.request.Request(url1, headers=headers)
    raw1 = opener.open(req1, timeout=15).read().decode("utf-8")
    if raw1.startswith("&&&START&&&"):
        raw1 = raw1[11:]
    info1 = json.loads(raw1)

    pwd_hash = hashlib.md5(pwd.encode("utf-8")).hexdigest().upper()
    auth_params = {
        "_json": "true",
        "qs": info1.get("qs", "%3Fsid%3Dmiothealth%26_json%3Dtrue"),
        "sid": "miothealth",
        "_sign": info1.get("_sign", ""),
        "callback": info1.get("callback", "https://hlth.io.mi.com/sts"),
        "user": user,
        "hash": pwd_hash,
    }
    req2 = urllib.request.Request(
        "https://account.xiaomi.com/pass/serviceLoginAuth2",
        data=urllib.parse.urlencode(auth_params).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    raw2 = opener.open(req2, timeout=15).read().decode("utf-8")
    if raw2.startswith("&&&START&&&"):
        raw2 = raw2[11:]
    info2 = json.loads(raw2)

    if info2.get("code") != 0 or not info2.get("location"):
        if info2.get("notificationUrl"):
            raise RuntimeError(
                f"触发二次安全验证，请参考 README 填入凭证或完成验证: {info2.get('notificationUrl')}"
            )
        raise RuntimeError(f"登录失败: {info2.get('desc') or info2}")

    ssecurity = info2["ssecurity"]
    user_id = str(info2["userId"])
    c_user_id = str(info2.get("cUserId", ""))
    nonce = info2["nonce"]
    location = info2["location"]

    sign_str = f"nonce={nonce}&{ssecurity}"
    client_sign = base64.b64encode(hashlib.sha1(sign_str.encode("utf-8")).digest()).decode("ascii")
    sts_url = location + "&clientSign=" + urllib.parse.quote(client_sign)
    opener.open(urllib.request.Request(sts_url, headers=headers), timeout=15)

    service_token = ""
    for cookie in cj.cookiejar:
        if cookie.name == "serviceToken":
            service_token = cookie.value
        elif cookie.name == "cUserId" and not c_user_id:
            c_user_id = cookie.value

    if not service_token:
        raise RuntimeError("未能从 STS 回调中获取到 serviceToken")

    token_data = {
        "userId": user_id,
        "cUserId": c_user_id,
        "serviceToken": service_token,
        "ssecurity": ssecurity,
        "sid": "miothealth",
        "updatedAt": datetime.now(TZ_BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
    }
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as token_file:
        os.chmod(TOKEN_FILE, 0o600)
        token_file.truncate(0)
        json.dump(token_data, token_file, indent=2, ensure_ascii=False)
    print(f"[+] 登录成功！凭证已以明文保存至 {TOKEN_FILE}（文件权限 0600）")
    return token_data


def sync_from_cloud(client: XiaomiHealthCloudClient, lookback_days: int = 7) -> dict:
    """从小米云端拉取最新健康数据并写入本地 SQLite 数据库"""
    init_local_dbs()
    now_ts = int(time.time())
    default_start = now_ts - lookback_days * 86400

    conn_fit = sqlite3.connect(FITNESS_DB)
    conn_sum = sqlite3.connect(SUMMARY_DB)
    stats = {}

    raw_table_map = [
        ("heart_rate", "hr_record"),
        ("single_heart_rate", "hr_record"),
        ("steps", "step_record"),
        ("calories", "calorie_record"),
        ("spo2", "spo2_record"),
        ("single_spo2", "spo2_record"),
        ("stress", "stress_record"),
        ("single_stress", "stress_record"),
        ("sleep", "sleep_segment"),
        ("valid_stand", "stand_record"),
        ("weight", "weight_item_record"),
        ("headset", "hearing_health_record"),
    ]

    for key, table in raw_table_map:
        start_ts = default_start
        items = client.fetch_raw_by_time(key, start_ts, now_ts + 3600)
        upserted = 0
        for item in items:
            sid = str(item.get("sid", "default"))
            ts = int(item["time"])
            val_str = item.get("value", "{}")
            z_off = int(item.get("zone_offset", 28800))
            z_name = item.get("zone_name", "Asia/Shanghai")
            time_in_0tz = ts + z_off
            is_del = 1 if item.get("deleted") else 0

            if table == "sleep_segment":
                conn_fit.execute(
                    """INSERT OR REPLACE INTO sleep_segment
                       (key, sid, time, isComplete, value, zoneOffsetInSec, zoneName, timeIn0Tz, isUpload, isDeleted, endTime, endTimeIn0Tz)
                       VALUES (?, ?, ?, 1, ?, ?, ?, ?, 1, ?, 0, 0)""",
                    (key, sid, ts, val_str, z_off, z_name, time_in_0tz, is_del),
                )
            elif table == "weight_item_record":
                try:
                    v_obj = json.loads(val_str) if val_str else {}
                except Exception:
                    v_obj = {}
                w_val = float(v_obj.get("weight", 0.0))
                bmi_val = v_obj.get("bmi")
                bf_val = v_obj.get("body_fat_rate")
                zero_t = ((ts + z_off) // 86400) * 86400 - z_off
                conn_fit.execute(
                    """INSERT OR REPLACE INTO weight_item_record
                       (key, sid, time, zeroTimeIn0Tz, weight, bmi, bodyFatRate, value, zoneOffsetInSec, zoneName, timeIn0Tz, isUpload, isDeleted, endTime, endTimeIn0Tz)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, 0)""",
                    (key, sid, ts, zero_t, w_val, bmi_val, bf_val, val_str, z_off, z_name, time_in_0tz, is_del),
                )
            else:
                conn_fit.execute(
                    f"""INSERT OR REPLACE INTO {table}
                        (key, sid, time, value, zoneOffsetInSec, zoneName, timeIn0Tz, isUpload, isDeleted, endTime, endTimeIn0Tz)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 0, 0)""",
                    (key, sid, ts, val_str, z_off, z_name, time_in_0tz, is_del),
                )
            upserted += 1
        stats[f"raw:{key}"] = upserted

    daily_key_map = [
        ("sleep", "SLEEP"),
        ("steps", "STEP"),
        ("calories", "CALORIE"),
        ("heart_rate", "HR"),
        ("spo2", "SPO2"),
        ("stress", "STRESS"),
        ("valid_stand", "VALID_STAND"),
    ]
    for cloud_key, db_type in daily_key_map:
        items = client.fetch_daily_report(cloud_key, default_start, now_ts + 86400)
        upserted = 0
        for item in items:
            sid = str(item.get("sid", "default"))
            t_zero = int(item["time"])
            upd_t = int(item.get("update_time", now_ts)) * 1000
            val_str = item.get("value", "{}")
            is_del = 1 if item.get("deleted") else 0
            conn_sum.execute(
                """INSERT OR REPLACE INTO daily_report
                   (sid, dataType, updateTime, timeInZero, viewTag, value, isUpload, isDeleted)
                   VALUES (?, ?, ?, ?, 'days', ?, 1, ?)""",
                (sid, db_type, upd_t, t_zero, val_str, is_del),
            )
            upserted += 1
        stats[f"daily:{db_type}"] = upserted

    wm_row = conn_fit.execute(
        "SELECT watermark FROM server_request WHERE action='incremental_pull' AND key='sport_report'"
    ).fetchone()
    last_wm = int(wm_row[0]) if wm_row and wm_row[0] else 0
    sport_items, new_wm = client.fetch_sport_records(last_wm)
    sport_upserted = 0
    for item in sport_items:
        sid = str(item.get("sid", "default"))
        key = str(item.get("key", ""))
        ts = int(item["time"])
        cat = item.get("category", "")
        z_off = int(item.get("zone_offset", 28800))
        z_name = item.get("zone_name", "Asia/Shanghai")
        val_str = item.get("value", "{}")
        time_in_0tz = ts + z_off
        is_del = 1 if item.get("deleted") else 0
        cheat = item.get("cheat_info")
        conn_fit.execute(
            """INSERT OR REPLACE INTO sport_report
               (sid, key, time, category, zoneOffsetInSec, zoneName, value, timeIn0Tz, isUpload, isDeleted, offlineCounted, antiCheckResult)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?)""",
            (sid, key, ts, cat, z_off, z_name, val_str, time_in_0tz, is_del, cheat),
        )
        sport_upserted += 1
    if new_wm > last_wm:
        conn_fit.execute(
            "UPDATE server_request SET watermark = ? WHERE action='incremental_pull' AND key='sport_report'",
            (new_wm,),
        )
    stats["sport_records"] = sport_upserted

    conn_fit.commit()
    conn_sum.commit()
    conn_fit.close()
    conn_sum.close()
    return stats


def export_all_csvs() -> dict:
    """将本地 SQLite 数据库导出为严格校正时区（UTC+8）的标准 CSV 文件"""
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    conn_fit = sqlite3.connect(FITNESS_DB)
    conn_sum = sqlite3.connect(SUMMARY_DB)
    counts = {}

    cal_map = {}
    for t_zero, val_str in conn_sum.execute(
        "SELECT timeInZero, value FROM daily_report WHERE dataType='CALORIE' AND isDeleted=0"
    ):
        try:
            d_str = datetime.fromtimestamp(t_zero, timezone.utc).strftime("%Y-%m-%d")
            cal_map[d_str] = json.loads(val_str).get("calories", 0)
        except Exception:
            pass

    step_rows = {}
    for t_zero, val_str in conn_sum.execute(
        "SELECT timeInZero, value FROM daily_report WHERE dataType='STEP' AND isDeleted=0 ORDER BY timeInZero"
    ):
        try:
            d_str = datetime.fromtimestamp(t_zero, timezone.utc).strftime("%Y-%m-%d")
            obj = json.loads(val_str)
            steps = int(obj.get("steps") or sum(r.get("steps", 0) for r in obj.get("stepRecords", [])))
            dist = int(obj.get("distance") or 0)
            act_cal = int(obj.get("calories") or 0)
            tot_cal = int(cal_map.get(d_str, act_cal))
            if steps > 0 or dist > 0:
                step_rows[d_str] = (d_str, steps, dist, act_cal, tot_cal)
        except Exception:
            pass

    for ts, val_str in conn_fit.execute(
        "SELECT time, value FROM step_record WHERE isDeleted=0 AND time >= ?",
        (int(time.time()) - 14 * 86400,),
    ):
        d_str = datetime.fromtimestamp(ts, TZ_BEIJING).strftime("%Y-%m-%d")
        if d_str not in step_rows:
            step_rows.setdefault(d_str, [d_str, 0, 0, 0, 0])
        if isinstance(step_rows[d_str], list):
            try:
                o = json.loads(val_str)
                step_rows[d_str][1] += int(o.get("steps", 0))
                step_rows[d_str][2] += int(o.get("distance", 0))
                step_rows[d_str][3] += round(float(o.get("calories", 0)))
                step_rows[d_str][4] = max(step_rows[d_str][3], int(cal_map.get(d_str, 0)))
            except Exception:
                pass

    with open(ANALYSIS_DIR / "steps_daily.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "steps", "distance_m", "active_cal", "total_cal"])
        for d_str in sorted(step_rows.keys()):
            r = step_rows[d_str]
            if r[1] > 0:
                w.writerow(r)
                counts["steps_daily"] = counts.get("steps_daily", 0) + 1

    sleep_by_date = {}
    for t_zero, val_str in conn_sum.execute(
        "SELECT timeInZero, value FROM daily_report WHERE dataType='SLEEP' AND isDeleted=0 ORDER BY timeInZero"
    ):
        try:
            d_str = datetime.fromtimestamp(t_zero, timezone.utc).strftime("%Y-%m-%d")
            o = json.loads(val_str)
            tot = int(o.get("total_duration") or o.get("duration") or 0)
            if tot <= 0:
                continue
            segs = o.get("segment_details") or []
            main_seg = max(segs, key=lambda s: s.get("duration", 0)) if segs else o
            bed_ts = main_seg.get("bedtime", 0)
            wake_ts = main_seg.get("wake_up_time", 0)
            bed_str = datetime.fromtimestamp(bed_ts, TZ_BEIJING).strftime("%Y-%m-%d %H:%M") if bed_ts else ""
            wake_str = datetime.fromtimestamp(wake_ts, TZ_BEIJING).strftime("%Y-%m-%d %H:%M") if wake_ts else ""
            sleep_by_date[d_str] = [
                d_str,
                tot,
                int(o.get("sleep_deep_duration", 0)),
                int(o.get("sleep_light_duration", 0)),
                int(o.get("sleep_rem_duration", 0)),
                int(o.get("sleep_awake_duration", 0)),
                int(o.get("sleep_score", 0)),
                int(o.get("avg_hr", 0)),
                int(o.get("avg_spo2", 0)),
                bed_str,
                wake_str,
            ]
        except Exception:
            pass

    for ts, val_str in conn_fit.execute(
        "SELECT time, value FROM sleep_segment WHERE isDeleted=0 ORDER BY time"
    ):
        try:
            o = json.loads(val_str)
            dur = int(o.get("duration", 0))
            if dur < 120:
                continue
            wake_ts = int(o.get("wake_up_time") or ts)
            bed_ts = int(o.get("bedtime") or (wake_ts - dur * 60))
            d_str = datetime.fromtimestamp(wake_ts, TZ_BEIJING).strftime("%Y-%m-%d")
            if d_str not in sleep_by_date or dur > sleep_by_date[d_str][1]:
                sleep_by_date[d_str] = [
                    d_str,
                    dur,
                    int(o.get("sleep_deep_duration", 0)),
                    int(o.get("sleep_light_duration", 0)),
                    int(o.get("sleep_rem_duration", 0)),
                    int(o.get("sleep_awake_duration", 0)),
                    sleep_by_date.get(d_str, [0] * 11)[6],
                    int(o.get("avg_hr", 0)),
                    int(o.get("avg_spo2", 0)),
                    datetime.fromtimestamp(bed_ts, TZ_BEIJING).strftime("%Y-%m-%d %H:%M"),
                    datetime.fromtimestamp(wake_ts, TZ_BEIJING).strftime("%Y-%m-%d %H:%M"),
                ]
        except Exception:
            pass

    with open(ANALYSIS_DIR / "sleep_daily.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "date", "total_min", "deep_min", "light_min", "rem_min", "awake_min",
            "sleep_score", "avg_hr", "avg_spo2", "bedtime", "wakeup"
        ])
        for d_str in sorted(sleep_by_date.keys()):
            w.writerow(sleep_by_date[d_str])
            counts["sleep_daily"] = counts.get("sleep_daily", 0) + 1

    with open(ANALYSIS_DIR / "hr_raw.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "date", "hour", "bpm"])
        seen_ts = set()
        for ts, val_str in conn_fit.execute(
            "SELECT time, value FROM hr_record WHERE isDeleted=0 ORDER BY time"
        ):
            if ts in seen_ts:
                continue
            try:
                bpm = int(json.loads(val_str).get("bpm", 0))
                if 30 <= bpm <= 220:
                    seen_ts.add(ts)
                    dt = datetime.fromtimestamp(ts, TZ_BEIJING)
                    w.writerow([dt.strftime("%Y-%m-%d %H:%M:%S"), dt.strftime("%Y-%m-%d"), dt.hour, bpm])
                    counts["hr_raw"] = counts.get("hr_raw", 0) + 1
            except Exception:
                pass

    with open(ANALYSIS_DIR / "spo2_raw.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "date", "hour", "spo2"])
        seen_ts = set()
        for ts, val_str in conn_fit.execute(
            "SELECT time, value FROM spo2_record WHERE isDeleted=0 ORDER BY time"
        ):
            if ts in seen_ts:
                continue
            try:
                spo2 = int(json.loads(val_str).get("spo2", 0))
                if 50 <= spo2 <= 100:
                    seen_ts.add(ts)
                    dt = datetime.fromtimestamp(ts, TZ_BEIJING)
                    w.writerow([dt.strftime("%Y-%m-%d %H:%M:%S"), dt.strftime("%Y-%m-%d"), dt.hour, spo2])
                    counts["spo2_raw"] = counts.get("spo2_raw", 0) + 1
            except Exception:
                pass

    with open(ANALYSIS_DIR / "stress_raw.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "date", "stress"])
        seen_ts = set()
        for ts, val_str in conn_fit.execute(
            "SELECT time, value FROM stress_record WHERE isDeleted=0 ORDER BY time"
        ):
            if ts in seen_ts:
                continue
            try:
                st = int(json.loads(val_str).get("stress", 0))
                if 0 < st <= 100:
                    seen_ts.add(ts)
                    dt = datetime.fromtimestamp(ts, TZ_BEIJING)
                    w.writerow([dt.strftime("%Y-%m-%d %H:%M:%S"), dt.strftime("%Y-%m-%d"), st])
                    counts["stress_raw"] = counts.get("stress_raw", 0) + 1
            except Exception:
                pass

    with open(ANALYSIS_DIR / "weight.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "weight", "bmi", "bodyFatRate"])
        for ts, weight, bmi, bfr in conn_fit.execute(
            "SELECT time, weight, bmi, bodyFatRate FROM weight_item_record WHERE isDeleted=0 ORDER BY time"
        ):
            dt = datetime.fromtimestamp(ts, TZ_BEIJING)
            w.writerow([dt.strftime("%Y-%m-%d %H:%M"), weight, bmi or "", bfr or ""])
            counts["weight"] = counts.get("weight", 0) + 1

    with open(ANALYSIS_DIR / "sport_records.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "category", "key", "duration_sec", "distance_m", "calories", "avg_hr", "max_hr", "steps"])
        for ts, cat, key, val_str in conn_fit.execute(
            "SELECT time, category, key, value FROM sport_report WHERE isDeleted=0 ORDER BY time"
        ):
            try:
                o = json.loads(val_str)
                dt = datetime.fromtimestamp(ts, TZ_BEIJING)
                w.writerow([
                    dt.strftime("%Y-%m-%d %H:%M:%S"),
                    cat,
                    key,
                    o.get("duration", 0),
                    o.get("distance", 0),
                    o.get("calories", 0),
                    o.get("avg_hrm", 0),
                    o.get("max_hrm", 0),
                    o.get("steps", 0),
                ])
                counts["sport_records"] = counts.get("sport_records", 0) + 1
            except Exception:
                pass

    with open(ANALYSIS_DIR / "hearing_health.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "date", "hour", "start_time", "end_time", "duration_sec", "high_volume"])
        for ts, val_str in conn_fit.execute(
            "SELECT time, value FROM hearing_health_record WHERE isDeleted=0 ORDER BY time"
        ):
            try:
                o = json.loads(val_str)
                st = int(o.get("start_time", ts))
                et = int(o.get("end_time", ts))
                dur = max(0, et - st)
                high = bool(o.get("high", False))
                dt = datetime.fromtimestamp(st, TZ_BEIJING)
                w.writerow([
                    dt.strftime("%Y-%m-%d %H:%M:%S"),
                    dt.strftime("%Y-%m-%d"),
                    dt.hour,
                    st,
                    et,
                    dur,
                    high,
                ])
                counts["hearing_health"] = counts.get("hearing_health", 0) + 1
            except Exception:
                pass

    conn_fit.close()
    conn_sum.close()
    return counts


def print_latest_summary():
    """打印最新同步后的健康状态速览"""
    conn_fit = sqlite3.connect(FITNESS_DB)
    print("\n" + "=" * 62)
    print("📊 Mi-Health-Exporter · 云端实时同步最新数据速览 (UTC+8)")
    print("=" * 62)

    hr_row = conn_fit.execute(
        "SELECT time, value FROM hr_record WHERE isDeleted=0 ORDER BY time DESC LIMIT 1"
    ).fetchone()
    if hr_row:
        dt = datetime.fromtimestamp(hr_row[0], TZ_BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        bpm = json.loads(hr_row[1]).get("bpm")
        print(f"❤️  最新心率记录 : {bpm} bpm ({dt})")

    slp_row = conn_fit.execute(
        "SELECT time, value FROM sleep_segment WHERE isDeleted=0 ORDER BY time DESC LIMIT 1"
    ).fetchone()
    if slp_row:
        o = json.loads(slp_row[1])
        dur = o.get("duration", 0)
        deep = o.get("sleep_deep_duration", 0)
        light = o.get("sleep_light_duration", 0)
        rem = o.get("sleep_rem_duration", 0)
        bed = datetime.fromtimestamp(o.get("bedtime", slp_row[0]), TZ_BEIJING).strftime("%m-%d %H:%M")
        wake = datetime.fromtimestamp(o.get("wake_up_time", slp_row[0]), TZ_BEIJING).strftime("%m-%d %H:%M")
        print(
            f"🌙 最新睡眠记录 : {bed} -> {wake} | 总计 {dur // 60}h{dur % 60:02d}m "
            f"(深睡 {deep}m / 浅睡 {light}m / REM {rem}m / 平均心率 {o.get('avg_hr', '-')} bpm)"
        )

    stp_row = conn_fit.execute(
        "SELECT MAX(time) FROM step_record WHERE isDeleted=0"
    ).fetchone()
    if stp_row and stp_row[0]:
        latest_dt = datetime.fromtimestamp(stp_row[0], TZ_BEIJING)
        day_start = int(latest_dt.replace(hour=0, minute=0, second=0).timestamp())
        tot_steps, tot_dist, tot_cal = 0, 0, 0.0
        for (val_str,) in conn_fit.execute(
            "SELECT value FROM step_record WHERE isDeleted=0 AND time >= ?", (day_start,)
        ):
            o = json.loads(val_str)
            tot_steps += int(o.get("steps", 0))
            tot_dist += int(o.get("distance", 0))
            tot_cal += float(o.get("calories", 0.0))
        print(
            f"🚶 最新单日步数 : {latest_dt.strftime('%Y-%m-%d')} 累计 {tot_steps:,} 步 "
            f"({tot_dist / 1000:.2f} km, 活动消耗 {tot_cal:.0f} kcal, 截至 {latest_dt.strftime('%H:%M')})"
        )

    hs_row = conn_fit.execute(
        "SELECT time, value FROM hearing_health_record WHERE isDeleted=0 ORDER BY time DESC LIMIT 1"
    ).fetchone()
    if hs_row:
        dt = datetime.fromtimestamp(hs_row[0], TZ_BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        high = json.loads(hs_row[1]).get("high", False)
        print(f"🎧 最新耳机记录 : {dt} (高音量告警: {'是 ⚠️' if high else '否 ✅'})")

    print("=" * 62 + "\n")
    conn_fit.close()


def main():
    parser = argparse.ArgumentParser(description="Mi-Health-Exporter: 小米运动健康云端增量同步与 CSV 导出工具")
    parser.add_argument("--days", type=int, default=7, help="回溯同步天数 (默认: 7 天；可用 --days 365 请求过去一年，实际范围取决于接口)")
    parser.add_argument("--login", action="store_true", help="使用小米账号密码交互式登录获取 Token")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days 必须是大于 0 的整数")

    if args.login or not TOKEN_FILE.exists():
        token_data = interactive_xiaomi_login()
    else:
        token_data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))

    print(f"[*] 正在连接小米健康云端 (https://hlth.io.mi.com, userId={token_data.get('userId')})...")
    client = XiaomiHealthCloudClient(token_data)

    print(f"[*] 正在拉取最近 {args.days} 天的云端增量健康数据...")
    sync_stats = sync_from_cloud(client, lookback_days=args.days)
    non_zero = {k: v for k, v in sync_stats.items() if v > 0}
    print(f"[+] 云端增量拉取完成，写入/更新本地数据库记录: {non_zero}")

    print("[*] 正在导出时区校准后的 analysis/*.csv 文件...")
    csv_counts = export_all_csvs()
    print(f"[+] CSV 导出完成: {csv_counts}")

    print_latest_summary()


if __name__ == "__main__":
    main()
