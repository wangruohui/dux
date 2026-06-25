from __future__ import annotations

import time
from pathlib import Path

from .service import DuxService


def _human_bytes(size: int) -> str:
    value = float(size)
    units = ["B", "K", "M", "G", "T", "P"]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024.0
    return f"{int(size)}B"


def _bar(value: int, max_value: int, width: int = 18) -> str:
    if max_value <= 0 or value <= 0:
        filled = 0
    else:
        filled = max(1, int(value * width / max_value))
    return "[" + ("=" * filled).ljust(width) + "]"


def _progress_bar(value: int, total: int | None, width: int = 20) -> str:
    if total is None or total <= 0:
        return "[" + ("?" * min(width, 3)).ljust(width) + "]"
    filled = min(width, max(0, int(value * width / total)))
    return "[" + ("#" * filled).ljust(width) + "]"


def run_ui(db_path: str | None, path: str, workers: int) -> None:
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Container
        from textual.screen import ModalScreen
        from textual.widgets import DataTable, Footer, Header, Label, Static
        from rich.text import Text
    except ImportError as exc:
        raise SystemExit("textual is required for `dux ui`; install project dependencies first") from exc

    class DuxTable(DataTable):
        def on_key(self, event) -> None:
            if event.key in {"q", "ctrl+c"}:
                event.stop()
                self.app.action_request_quit()
            elif event.key == "enter":
                event.stop()
                self.app.action_open_selected()
            elif event.key == "backspace":
                event.stop()
                self.app.action_go_parent()
            elif event.key == "right":
                event.stop()
                self.app.action_open_selected()
            elif event.key == "left":
                event.stop()
                self.app.action_go_parent()
            elif event.key == "space":
                event.stop()
                self.app.action_toggle_select()
            elif event.key == "shift+delete":
                event.stop()
                self.app.action_delete_marked()

    class ConfirmScreen(ModalScreen[bool]):
        def __init__(self, message: str) -> None:
            super().__init__()
            self.message = message

        def compose(self) -> ComposeResult:
            yield Container(
                Static(self.message, id="message"),
                Label("Press y to confirm, n or Esc to cancel"),
                id="dialog",
            )

        def key_y(self) -> None:
            self.dismiss(True)

        def key_n(self) -> None:
            self.dismiss(False)

        def key_escape(self) -> None:
            self.dismiss(False)

    class DuxApp(App[None]):
        CSS = """
        Screen {
            background: #0f1720;
            color: #d8e1ea;
        }
        #dialog {
            width: 70%;
            height: auto;
            background: #16212d;
            border: round #7dd3fc;
            padding: 1 2;
            align: center middle;
        }
        DataTable {
            height: 1fr;
        }
        #status {
            height: 1;
            background: #15202b;
            color: #facc15;
            padding: 0 1;
        }
        """
        BINDINGS = [
            Binding("q", "request_quit", "Quit"),
            Binding("ctrl+c", "request_quit", "Quit"),
            Binding("enter", "open_selected", "Open"),
            Binding("backspace", "go_parent", "Up"),
            Binding("r", "refresh_current", "Refresh"),
            Binding("space", "toggle_select", "Select"),
            Binding("d", "trash_selected", "Trash"),
            Binding("D", "delete_selected", "Delete"),
            Binding("shift+delete", "delete_marked", "Delete Marked"),
            Binding("s", "sort_size", "Sort Size"),
            Binding("c", "sort_count", "Sort Count"),
            Binding("m", "sort_mtime", "Sort Date"),
            Binding("n", "sort_name", "Sort Name"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.service = DuxService(db_path=db_path, max_workers=workers)
            self.current_path = self.service.canonical(path)
            self.service.ensure_navigation_path(self.current_path)
            self.sort_by = "size"
            self.reverse = True
            self.rows_by_key: dict[str, bool] = {}
            self.marked_paths: set[str] = set()
            self.delete_active = False

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("Ready", id="status")
            yield DuxTable(id="table")
            yield Footer()

        def action_request_quit(self) -> None:
            if self.delete_active:
                self.notify("Delete is still running; wait for it to finish before quitting.", severity="warning")
                return
            self.service.close()
            self.exit()

        def key_q(self) -> None:
            self.action_request_quit()

        def on_mount(self) -> None:
            table = self.query_one(DataTable)
            table.cursor_type = "row"
            table.add_columns("Size", "Files", "Date", "Name", "Graph")
            self._reload_table()

        def _reload_table(self, focus_path: str | None = None) -> None:
            table = self.query_one(DataTable)
            table.clear()
            self.rows_by_key.clear()
            row_index_by_key: dict[str, int] = {}
            root = self.service.get_node(self.current_path)
            root_indexed = bool(root and root["indexed"])
            rows, truncated = self.service.list_visible_children(
                self.current_path,
                sort_by=self.sort_by,
                reverse=self.reverse,
            )
            if root is None and not rows:
                table.add_row(
                    "-",
                    "-",
                    "-",
                    f"Not indexed. Run: dux index {self.current_path}",
                    "",
                    key="__not_indexed__",
                )
                self.title = f"{self.current_path} (not indexed)"
                return
            if not rows:
                table.add_row("-", "0", "-", "(empty or no indexed children)", "", key="__empty__")
                self.title = self.current_path
                return
            metric = "file_count" if self.sort_by == "count" else "size_bytes"
            max_metric = max((int(row[metric] or 0) for row in rows), default=0)
            for row in rows:
                mtime = float(row["mtime"] or 0.0)
                date = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "-"
                name = row["name"] + ("/" if row["is_dir"] else "")
                size = self._format_size(row)
                files = self._format_files(row)
                key = str(row["path"])
                metric_value = int(row[metric] or 0)
                marked = key in self.marked_paths
                display_name = f"[x] {name}" if marked else name
                table.add_row(
                    self._style_cell(size, marked),
                    self._style_cell(files, marked),
                    self._style_cell(date, marked),
                    self._style_cell(display_name, marked),
                    self._style_cell(_bar(metric_value, max_metric) if metric_value else "", marked),
                    key=key,
                )
                self.rows_by_key[key] = bool(row["is_dir"])
                row_index_by_key[key] = len(row_index_by_key)
            if truncated:
                table.add_row(
                    "-",
                    "-",
                    "-",
                    f"(showing first 200 live entries; indexed entries are always shown)",
                    "",
                    key="__truncated__",
                )
            if root_indexed:
                self.title = self.current_path
            elif root is not None:
                self.title = f"{self.current_path} (partial index)"
            else:
                self.title = f"{self.current_path} (live, unindexed)"
            self._restore_cursor(table, focus_path, row_index_by_key)

        def _style_cell(self, value: str, marked: bool) -> str | Text:
            if not marked:
                return value
            return Text(value, style="bold black on yellow")

        def _restore_cursor(self, table: DataTable, focus_path: str | None, row_index_by_key: dict[str, int]) -> None:
            if focus_path is None:
                return
            row_index = row_index_by_key.get(focus_path)
            if row_index is not None:
                table.move_cursor(row=row_index, column=0, animate=False)

        def _set_status(self, message: str) -> None:
            self.query_one("#status", Static).update(message)

        def _format_size(self, row: dict[str, object]) -> str:
            size = row["size_bytes"]
            if size is None:
                return "unindexed"
            text = _human_bytes(int(size))
            return text if row["indexed"] else f">={text}"

        def _format_files(self, row: dict[str, object]) -> str:
            files = row["file_count"]
            if files is None:
                return "-"
            text = str(files)
            return text if row["indexed"] else f">={text}"

        def _selected_path(self) -> str | None:
            table = self.query_one(DataTable)
            if table.cursor_row < 0 or table.row_count == 0:
                return None
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            selected = str(cell_key.row_key.value)
            if selected.startswith("__"):
                return None
            return selected

        def action_toggle_select(self) -> None:
            selected = self._selected_path()
            if not selected:
                return
            if selected in self.marked_paths:
                self.marked_paths.remove(selected)
            else:
                self.marked_paths.add(selected)
            self._reload_table(focus_path=selected)

        def _marked_delete_roots(self) -> list[str]:
            roots: list[str] = []
            for path in sorted(self.marked_paths, key=lambda item: (item.count("/"), item)):
                if any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots):
                    continue
                roots.append(path)
            return roots

        def action_open_selected(self) -> None:
            selected = self._selected_path()
            if not selected:
                return
            if self.rows_by_key.get(selected):
                self.current_path = selected
                self._reload_table()

        def action_go_parent(self) -> None:
            parent = str(Path(self.current_path).parent)
            if parent != self.current_path:
                self.current_path = parent
                self._reload_table()

        def action_refresh_current(self) -> None:
            self.notify(f"Refreshing {self.current_path}")
            self.run_worker(self._refresh_current_worker, thread=True)

        def _refresh_current_worker(self) -> None:
            self.service.index_path(self.current_path)
            self.call_from_thread(self._reload_table)

        def action_sort_size(self) -> None:
            self.sort_by = "size"
            self._reload_table()

        def action_sort_count(self) -> None:
            self.sort_by = "count"
            self._reload_table()

        def action_sort_mtime(self) -> None:
            self.sort_by = "mtime"
            self._reload_table()

        def action_sort_name(self) -> None:
            self.sort_by = "name"
            self.reverse = False
            self._reload_table()

        def action_trash_selected(self) -> None:
            selected = self._selected_path()
            if not selected:
                return
            if self.delete_active:
                self.notify("Delete is already running.", severity="warning")
                return

            def after(confirm: bool) -> None:
                if confirm:
                    self._start_delete([selected], permanent=False, trash=True)

            self.push_screen(ConfirmScreen(f"Move to trash?\n{selected}"), after)

        def action_delete_selected(self) -> None:
            selected = self._selected_path()
            if not selected:
                return
            if self.delete_active:
                self.notify("Delete is already running.", severity="warning")
                return

            def after(confirm: bool) -> None:
                if confirm:
                    self._start_delete([selected], permanent=True, trash=False)

            self.push_screen(ConfirmScreen(f"Permanently delete?\n{selected}"), after)

        def action_delete_marked(self) -> None:
            targets = self._marked_delete_roots()
            if not targets:
                return
            if self.delete_active:
                self.notify("Delete is already running.", severity="warning")
                return
            preview = "\n".join(targets[:20])
            suffix = "" if len(targets) <= 20 else f"\n... and {len(targets) - 20} more"
            message = (
                f"Permanently delete {len(targets)} selected item(s)?\n"
                f"{preview}{suffix}\n\n"
                "Press y to confirm, n or Esc to cancel."
            )

            def after(confirm: bool) -> None:
                if not confirm:
                    return
                self._start_delete(targets, permanent=True, trash=False)

            self.push_screen(ConfirmScreen(message), after)

        def _start_delete(self, targets: list[str], *, permanent: bool, trash: bool) -> None:
            self.delete_active = True
            action = "Moving to trash" if trash else "Deleting"
            self._set_status(f"{action} {len(targets)} item(s)...")
            self.notify(f"{action} {len(targets)} item(s). UI remains responsive.")
            self.run_worker(
                lambda: self._delete_worker(targets, permanent=permanent, trash=trash),
                thread=True,
            )

        def _delete_worker(self, targets: list[str], *, permanent: bool, trash: bool) -> None:
            completed: list[str] = []
            try:
                action = "Moving" if trash else "Deleting"
                self.call_from_thread(self._set_status, f"{action} {len(targets)} item(s) with 2 workers...")
                totals = {target: self._delete_total(target) for target in targets}
                started_at = time.monotonic()

                def progress(target: str, count: int, path: str) -> None:
                    elapsed = max(time.monotonic() - started_at, 0.001)
                    rate = count / elapsed
                    total = totals.get(target)
                    total_text = "?" if total is None else str(total)
                    pct_text = "" if total is None else f" {min(100.0, count * 100.0 / total):5.1f}%"
                    self.call_from_thread(
                        self._set_status,
                        f"Deleting {_progress_bar(count, total)}{pct_text} {count}/{total_text} {rate:.1f}/s current={path}",
                    )

                def status(target: str, phase: str) -> None:
                    if phase == "updating-index":
                        self.call_from_thread(self._set_status, f"Updating index after delete: {target}")
                    elif phase == "index-updated":
                        self.call_from_thread(self._set_status, f"Index updated after delete: {target}")

                self.service.delete_paths(
                    targets,
                    permanent=permanent,
                    trash=trash,
                    progress=None if trash else progress,
                    status=status,
                    progress_interval=1000,
                    workers=2,
                    unlink_workers=16,
                )
                completed = targets
                self.call_from_thread(self._finish_delete, targets, completed, None)
            except Exception as exc:
                self.call_from_thread(self._finish_delete, targets, completed, exc)

        def _delete_total(self, target: str) -> int | None:
            row = self.service.get_node(target)
            if row is None:
                return None
            return int(row["file_count"]) + int(row["dir_count"]) + 1

        def _finish_delete(self, targets: list[str], completed: list[str], error: Exception | None) -> None:
            self.delete_active = False
            for target in completed:
                self.marked_paths.discard(target)
            self.marked_paths = {
                path
                for path in self.marked_paths
                if not any(path == target or path.startswith(target.rstrip("/") + "/") for target in completed)
            }
            if error is not None:
                self._set_status(f"Delete failed after {len(completed)}/{len(targets)} item(s): {error}")
                self.notify(f"Delete failed: {error}", severity="error")
            else:
                self._set_status(f"Delete finished: {len(completed)} item(s)")
                self.notify(f"Delete finished: {len(completed)} item(s)")
            self._reload_table()

    DuxApp().run()
