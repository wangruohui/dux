from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .service import DuvizService
from .tui import run_ui


def _human_bytes(size: int) -> str:
    value = float(size)
    units = ["B", "K", "M", "G", "T", "P"]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024.0
    return f"{int(size)}B"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dux")
    parser.add_argument("--db", default=None, help="path to sqlite database")
    parser.add_argument("--workers", type=int, default=8, help="scanner worker threads (default: 8)")
    parser.add_argument("--progress-interval", type=int, default=10000, help="print one scanned file path per N files")

    sub = parser.add_subparsers(dest="command", required=True)

    index = sub.add_parser("index")
    index.add_argument("path")

    ls_cmd = sub.add_parser("ls")
    ls_cmd.add_argument("path")
    ls_cmd.add_argument("--sort", choices=["size", "count", "mtime", "name"], default="size")

    delete = sub.add_parser("delete")
    delete.add_argument("path")
    group = delete.add_mutually_exclusive_group(required=True)
    group.add_argument("--trash", action="store_true")
    group.add_argument("--permanent", action="store_true")

    ui = sub.add_parser("ui")
    ui.add_argument("path")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "ui":
        run_ui(args.db, args.path, args.workers)
        return 0

    service = DuvizService(db_path=args.db, max_workers=args.workers)
    try:
        if args.command == "index":
            def report_progress(count: int, path: str) -> None:
                print(f"scanned_files={count} current={path}", file=sys.stderr, flush=True)

            result = service.index_path(
                args.path,
                progress=report_progress,
                progress_interval=args.progress_interval,
            )
            root = result.root
            elapsed = max(result.scan.elapsed_seconds, 0.000001)
            file_rate = result.scan.scanned_files / elapsed
            dir_rate = result.scan.scanned_dirs / elapsed
            print(
                "indexed "
                f"{root.path} size={root.size_bytes} files={root.file_count} dirs={root.dir_count} "
                f"elapsed={elapsed:.3f}s files_per_sec={file_rate:.1f} dirs_per_sec={dir_rate:.1f}"
            )
            return 0
        if args.command == "delete":
            dst = service.delete_path(args.path, permanent=args.permanent, trash=args.trash)
            if args.trash:
                print(f"moved_to_trash {dst}")
            else:
                print(f"deleted {service.canonical(args.path)}")
            return 0
        if args.command == "ls":
            path = service.canonical(args.path)
            mtimes = service.stat_visible_children(path)
            rows = service.list_children(path, sort_by="size" if args.sort == "mtime" else args.sort, reverse=args.sort != "name")
            if args.sort == "mtime":
                rows = sorted(rows, key=lambda row: (mtimes.get(row["path"], 0.0), row["name"]), reverse=True)
            for row in rows:
                mtime = mtimes.get(row["path"], 0.0)
                date = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "-"
                name = row["name"] + ("/" if row["is_dir"] else "")
                print(f"{_human_bytes(row['size_bytes']):>8} {row['file_count']:>8} {date} {name}")
            return 0
    finally:
        service.close()

    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
