from __future__ import annotations

import argparse
import ftplib
import os
from pathlib import Path


SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "env", "deploy"}
SKIP_FILES = {"flask_server.log", "flask_server.err.log", "sait.rar", "sait-deploy.zip"}
KEEP_REMOTE = {"cgi-bin", "old_index.php", "public_html"}
BACKUP_PREFIX = "рез"


def should_skip(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    parts = set(relative.parts)
    if parts & SKIP_DIRS:
        return True
    if any(part.startswith(BACKUP_PREFIX) for part in relative.parts):
        return True
    return path.name in SKIP_FILES


def ftp_mkdirs(ftp: ftplib.FTP, remote_dir: str) -> None:
    current = ""
    for part in [p for p in remote_dir.split("/") if p]:
        current = f"{current}/{part}" if current else part
        try:
            ftp.mkd(current)
        except ftplib.error_perm as exc:
            if not str(exc).startswith("550"):
                raise


def ftp_list(ftp: ftplib.FTP, remote_dir: str) -> list[tuple[str, bool]]:
    rows: list[tuple[str, bool]] = []

    def collect(line: str) -> None:
        parts = line.split(maxsplit=8)
        if len(parts) < 9:
            return
        name = parts[8]
        if name in {".", ".."}:
            return
        rows.append((name, line.startswith("d")))

    ftp.retrlines(f"LIST {remote_dir}", collect)
    return rows


def ftp_delete_tree(ftp: ftplib.FTP, remote_path: str) -> None:
    try:
        items = ftp_list(ftp, remote_path)
    except ftplib.error_perm:
        ftp.delete(remote_path)
        return

    for name, is_dir in items:
        child = f"{remote_path}/{name}"
        if is_dir:
            ftp_delete_tree(ftp, child)
        else:
            ftp.delete(child)
    ftp.rmd(remote_path)


def clean_remote(ftp: ftplib.FTP, remote_root: str) -> None:
    for name, is_dir in ftp_list(ftp, remote_root):
        if name in KEEP_REMOTE:
            continue
        path = f"{remote_root}/{name}"
        if is_dir:
            ftp_delete_tree(ftp, path)
        else:
            ftp.delete(path)


def upload_project(ftp: ftplib.FTP, local_root: Path, remote_root: str) -> None:
    for local_path in local_root.rglob("*"):
        if should_skip(local_path, local_root) or local_path.is_dir():
            continue

        relative = local_path.relative_to(local_root).as_posix()
        remote_path = f"{remote_root}/{relative}"
        remote_dir = remote_path.rsplit("/", 1)[0]
        ftp_mkdirs(ftp, remote_dir)

        with local_path.open("rb") as file:
            ftp.storbinary(f"STOR {remote_path}", file)

    ftp_mkdirs(ftp, f"{remote_root}/tmp")
    restart_path = f"{remote_root}/tmp/restart.txt"
    from io import BytesIO

    ftp.storbinary(f"STOR {restart_path}", BytesIO(b"restart\n"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", default=os.environ.get("FTP_PASSWORD"))
    parser.add_argument("--remote-root", default="/estemigk.beget.tech/public_html")
    parser.add_argument("--local-root", default=".")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--skip-vendor", action="store_true")
    args = parser.parse_args()

    if not args.password:
        raise SystemExit("FTP password is required")

    local_root = Path(args.local_root).resolve()

    with ftplib.FTP(args.host, timeout=30) as ftp:
        ftp.login(args.user, args.password)
        ftp.set_pasv(True)
        if args.clean:
            clean_remote(ftp, args.remote_root)
        if args.skip_vendor:
            SKIP_DIRS.add("_vendor")
        upload_project(ftp, local_root, args.remote_root)

    print("FTP deploy done")


if __name__ == "__main__":
    main()
