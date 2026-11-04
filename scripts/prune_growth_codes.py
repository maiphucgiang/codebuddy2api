#!/usr/bin/env python3
"""Remove automation switches of retired growth task codes from control.sqlite3.

v1.5.2 removed twelve retired task codes from the growth task groups, but stores
written by earlier versions still hold their switches and the strict load-time
check then rejects the whole file, so the gateway cannot start. This script is
standalone (stdlib only) so it also runs against a v1.5.2 installation: stop the
gateway, run it, and start again. v1.5.3 and later drop these codes on load, so
the script only matters for a database that still has to be cleaned by hand.
"""
import json
import os
import shutil
import sqlite3
import sys
import time

RETIRED = {
    "Buddy_App_QQ", "Hp_Appearance", "Model_chat_GLM5.2", "black_cat", "create_canvas",
    "school_season", "Sequential_Tasks_2", "Sequential_Tasks_3", "Sequential_Tasks_4",
    "Sequential_Tasks_5", "Sequential_Tasks_6", "Sequential_Tasks_7",
}


def locate(argument):
    """Return the store path from the argument, CODEBUDDY_AUTH_DIR or the known data directories."""
    candidates = [argument] if argument else []
    if not argument and os.environ.get("CODEBUDDY_AUTH_DIR"):
        candidates.append(os.environ["CODEBUDDY_AUTH_DIR"])
    if not argument:
        candidates += ["/data/auth", "auth"]
        if "__file__" in globals():     # stdin runs (`python - < script`) have no script path.
            candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "auth"))
    for candidate in candidates:
        path = os.path.join(candidate, "control.sqlite3") if os.path.isdir(candidate) else candidate
        if os.path.isfile(path):
            return path
    sys.exit("找不到 control.sqlite3，请把数据目录或数据库路径作为参数传入: python3 prune_growth_codes.py /path/to/auth")


def backup_path(path):
    """Name the backup without overwriting one written in the same second."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target, counter = f"{path}.bak-{stamp}", 1
    while os.path.exists(target):
        target, counter = f"{path}.bak-{stamp}-{counter}", counter + 1
    return target


def prune(path):
    """Drop retired switches in one transaction and return the removed count."""
    backup = backup_path(path)
    shutil.copy2(path, backup)
    try:
        db = sqlite3.connect(path, isolation_level=None, timeout=1)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision,payload FROM control WHERE id=1").fetchone()
            if row is None:
                sys.exit("control 表中没有配置记录，未做修改")
            revision, data = row[0], json.loads(row[1])
            removed = 0
            for metadata in data["credentials"].values():
                growth = metadata.get("growth_auto")
                if isinstance(growth, dict) and set(growth) & RETIRED:
                    kept = {code: on for code, on in growth.items() if code not in RETIRED}
                    removed += len(growth) - len(kept)
                    if kept:
                        metadata["growth_auto"] = kept
                    else:
                        metadata.pop("growth_auto")
            if removed:
                db.execute("UPDATE control SET revision=?, payload=? WHERE id=1",
                           (revision + 1, json.dumps(data, ensure_ascii=False, allow_nan=False)))
                db.execute("COMMIT")
            else:
                db.execute("ROLLBACK")
            return removed, revision, backup
        finally:
            db.close()
    except sqlite3.OperationalError as error:
        if "locked" in str(error):
            sys.exit(f"数据库被占用，请先停止网关再运行（{backup} 是本次备份，可删）")
        raise


def main():
    path = locate(sys.argv[1] if len(sys.argv) > 1 else "")
    removed, revision, backup = prune(path)
    if removed:
        print(f"已清理 {removed} 个下线任务开关，备份 {backup}，revision {revision} -> {revision + 1}")
    else:
        print(f"没有需要清理的残留开关，未改库（备份 {backup} 可删）")


if __name__ == "__main__":
    main()
