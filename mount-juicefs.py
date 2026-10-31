#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VOL_NAME = "juicefs"
MOUNTPOINT = Path("/mnt/juicefs")
READ_CACHE_DIR = Path("/dev/shm/juicefs-cache")
WRITE_CACHE_DIR = Path("/var/cache/juicefs-write-cache")
LOG_FILE = Path("/var/log/juicefs.log")
SQLITE_META_DB = Path("/var/lib/juicefs/juicefs.db")
STEAMAPPS_DIR = MOUNTPOINT / "steam" / "steamapps"
BIND_ROOT = Path("/var/lib/steam-local/steamapps")
BIND_DIRS = ("compatdata", "shadercache", "temp")
MAX_UPLOADS = "256"
BUFFER_SIZE = "1024"
READ_CACHE_SIZE = "16G"
WRITE_CACHE_SIZE = "64G"
ENV_FILE = Path(os.environ.get("JUICEFS_ENV_FILE", "/mnt/.env"))
MODE_STATE_FILE = Path("/run/juicefs-mount-mode")
PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def die(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(1)


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        die(f"环境文件不存在：{path}")
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            die(f"{path}:{lineno}: 无效行，应为 KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not ENV_KEY_RE.fullmatch(key):
            die(f"{path}:{lineno}: 无效变量名 {key!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def required(values: dict[str, str], key: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        die(f"{ENV_FILE} 缺少必填项：{key}")
    return value


def parse_port(value: str, key: str) -> int:
    try:
        port = int(value)
    except ValueError:
        die(f"{ENV_FILE} 的 {key} 不是有效端口：{value}")
    if not 1 <= port <= 65535:
        die(f"{ENV_FILE} 的 {key} 超出端口范围：{port}")
    return port


def clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in PROXY_KEYS}


def run_checked(command: list[str], env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, env=env, text=True, check=False)
    if result.returncode != 0:
        die(f"命令失败（退出码 {result.returncode}）：{' '.join(command)}")


def find_current_mode() -> str | None:
    mountpoint = str(MOUNTPOINT)
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            raw = (proc_dir / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        args = [item.decode(errors="replace") for item in raw.split(b"\0") if item]
        if "juicefs" not in " ".join(args):
            continue
        if "mount" not in args or mountpoint not in args:
            continue
        return "write" if "--writeback" in args else "read"
    return None


def read_state_mode() -> str | None:
    try:
        mode = MODE_STATE_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return mode if mode in {"read", "write"} else None


def write_state_mode(mode: str) -> None:
    MODE_STATE_FILE.write_text(f"{mode}\n", encoding="utf-8")
    MODE_STATE_FILE.chmod(0o600)


def setup_bind_mounts() -> None:
    for name in BIND_DIRS:
        source = BIND_ROOT / name
        target = STEAMAPPS_DIR / name
        source.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.mkdir(parents=True, exist_ok=True, mode=0o755)
        if os.path.ismount(target):
            print(f"绑定挂载已存在：{source} -> {target}")
            continue
        run_checked(["mount", "--bind", str(source), str(target)])
        print(f"已创建绑定挂载：{source} -> {target}")


def prepare_paths(mode: str) -> tuple[Path, str, str]:
    cache_dir = WRITE_CACHE_DIR if mode == "write" else READ_CACHE_DIR
    cache_size = WRITE_CACHE_SIZE if mode == "write" else READ_CACHE_SIZE
    free_space_ratio = "0.05" if mode == "write" else "0.1"
    for path in (cache_dir, MOUNTPOINT, SQLITE_META_DB.parent):
        path.mkdir(parents=True, exist_ok=True)
    cache_dir.chmod(0o700)
    SQLITE_META_DB.parent.chmod(0o700)
    return cache_dir, cache_size, free_space_ratio


def build_meta(values: dict[str, str], juicefs: str) -> tuple[str, dict[str, str], str]:
    backend = values.get("META_BACKEND", "redis").strip().lower()
    env = clean_env()
    if backend == "redis":
        host = required(values, "REDIS_HOST")
        port = parse_port(required(values, "REDIS_PORT"), "REDIS_PORT")
        db = required(values, "REDIS_DB")
        if not db.isdigit():
            die(f"{ENV_FILE} 的 REDIS_DB 必须是非负整数：{db}")
        password = required(values, "REDIS_PASSWORD")
        meta_url = f"redis://{host}:{port}/{db}"
        env["META_PASSWORD"] = password
    elif backend == "sqlite":
        meta_url = f"sqlite3://{SQLITE_META_DB}"
    else:
        die(f"未知 META_BACKEND：{backend}（可选 redis 或 sqlite）")
    return meta_url, env, backend


def ensure_meta(juicefs: str, values: dict[str, str], meta_url: str, env: dict[str, str], backend: str) -> None:
    if backend == "redis":
        result = subprocess.run(
            [juicefs, "status", meta_url],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            if result.stdout:
                sys.stderr.write(result.stdout)
            if result.stderr:
                sys.stderr.write(result.stderr)
            die(f"Redis 元数据不可用；拒绝自动格式化，请先检查 {meta_url}")
        print(f"检测到 Redis 元数据，跳过格式化：{meta_url}")
        return
    if SQLITE_META_DB.is_file():
        print(f"检测到已有 SQLite 元数据库，跳过格式化：{SQLITE_META_DB}")
        return
    bucket = required(values, "S3_BUCKET")
    access_key = required(values, "S3_ACCESS_KEY")
    secret_key = required(values, "S3_SECRET_KEY")
    print("首次初始化 SQLite 元数据并格式化卷...")
    run_checked(
        [
            juicefs,
            "format",
            "--storage",
            "s3",
            "--bucket",
            bucket,
            "--access-key",
            access_key,
            "--secret-key",
            secret_key,
            meta_url,
            VOL_NAME,
        ],
        env=env,
    )


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="挂载 JuiceFS")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--read", action="store_const", dest="mode", const="read", help="tmpfs 读缓存（默认）")
    group.add_argument("--write", action="store_const", dest="mode", const="write", help="根分区写缓存并启用 writeback")
    parser.set_defaults(mode="read")
    args = parser.parse_args()

    if os.geteuid() != 0:
        die("需要 root 权限运行。")
    juicefs = shutil.which("juicefs")
    if juicefs is None:
        die("找不到 juicefs 可执行文件。")

    mode: str = args.mode
    values = load_env(ENV_FILE)
    meta_url, env, backend = build_meta(values, juicefs)
    cache_dir, cache_size, free_space_ratio = prepare_paths(mode)

    if os.path.ismount(MOUNTPOINT):
        current_mode = read_state_mode() or find_current_mode() or "read"
        write_state_mode(current_mode)
        if current_mode != mode:
            die(f"当前已以 {current_mode} 模式挂载；切换模式前先运行 /mnt/umount-juicefs.sh")
        print(f"JuiceFS 已以 {mode} 模式挂载在 {MOUNTPOINT}，无需重复挂载。")
        run_checked(["findmnt", "-T", str(MOUNTPOINT)])
        setup_bind_mounts()
        return

    ensure_meta(juicefs, values, meta_url, env, backend)
    print(f"模式：{mode}，缓存目录：{cache_dir}，缓存上限：{cache_size}")
    if mode == "write":
        print("警告：writeback 模式必须使用 /mnt/umount-juicefs.sh 安全卸载并等待上传完成。")
    print(f"开始挂载 {VOL_NAME} 到 {MOUNTPOINT} ...")

    command = [
        juicefs,
        "mount",
        meta_url,
        str(MOUNTPOINT),
        "-d",
        "--log",
        str(LOG_FILE),
        "--check-storage",
        "--cache-dir",
        str(cache_dir),
        "--cache-size",
        cache_size,
        "--free-space-ratio",
        free_space_ratio,
        "--max-uploads",
        MAX_UPLOADS,
        "--buffer-size",
        BUFFER_SIZE,
        "--prefetch",
        "1",
    ]
    if mode == "read":
        command.extend(
            [
                "--attr-cache",
                "10",
                "--entry-cache",
                "10",
                "--dir-entry-cache",
                "10",
                "--negative-entry-cache",
                "10",
                "--open-cache",
                "10",
                "--open-cache-limit",
                "65536",
                "--readdir-cache",
                "--fast-statfs",
                "--backup-meta",
                "24h",
            ]
        )
    else:
        command.append("--writeback")

    run_checked(command, env=env)
    for _ in range(30):
        if os.path.ismount(MOUNTPOINT):
            write_state_mode(mode)
            print(f"挂载成功：{MOUNTPOINT}")
            run_checked(["df", "-hT", str(MOUNTPOINT)])
            setup_bind_mounts()
            return
        time.sleep(1)

    print("挂载超时，最近日志如下：", file=sys.stderr)
    try:
        tail = subprocess.run(
            ["tail", "-n", "80", str(LOG_FILE)],
            text=True,
            capture_output=True,
            check=False,
        )
        if tail.stdout:
            sys.stderr.write(tail.stdout)
    except OSError:
        pass
    raise SystemExit(1)


if __name__ == "__main__":
    main()
