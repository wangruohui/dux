from __future__ import annotations

import threading
import time
from pathlib import Path

from . import db
from .service import DeleteCancelled, DuxService, FilterCancelled, FilterEntry


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


def _eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def run_ui(db_path: str | None, path: str, workers: int) -> None:
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Container
        from textual.screen import ModalScreen
        from textual.widgets import DataTable, Footer, Header, Input, Label, Static
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
            elif event.key == "space":
                event.stop()
                self.app.action_toggle_select()
            elif event.key in {"delete", "shift+delete"}:
                event.stop()
                self.app.action_delete_requested()

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

    class FilterQueryScreen(ModalScreen[tuple[str, str] | None]):
        def compose(self) -> ComposeResult:
            yield Container(
                Static("Recursive filter from the current directory", classes="dialog-title"),
                Label("Name pattern to find"),
                Input(placeholder="required basename glob, e.g. a*", id="filter-keyword"),
                Label("Exclude paths containing"),
                Input(placeholder="optional; matching directories are pruned", id="filter-exclude"),
                Label("Enter: next/start    Esc: cancel"),
                id="filter-dialog",
            )

        def on_mount(self) -> None:
            self.query_one("#filter-keyword", Input).focus()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            if event.input.id == "filter-keyword":
                self.query_one("#filter-exclude", Input).focus()
                return
            keyword = self.query_one("#filter-keyword", Input).value
            if not keyword:
                self.app.notify("Filter keyword must not be empty.", severity="warning")
                self.query_one("#filter-keyword", Input).focus()
                return
            exclude = self.query_one("#filter-exclude", Input).value
            self.dismiss((keyword, exclude))

        def key_escape(self) -> None:
            self.dismiss(None)

    class FilterResultsTable(DataTable):
        def on_key(self, event) -> None:
            if event.key == "space":
                event.stop()
                self.screen.action_toggle_result()
            elif event.key == "a":
                event.stop()
                self.screen.action_toggle_all_results()
            elif event.key == "enter":
                event.stop()
                self.screen.action_accept_results()
            elif event.key in {"escape", "q"}:
                event.stop()
                self.screen.dismiss(None)

    class FilterResultsScreen(ModalScreen[list[str] | None]):
        def __init__(self, root: str, entries: list[FilterEntry]) -> None:
            super().__init__()
            self.root = root
            self.entries = entries
            self.paths = [entry.path for entry in entries]
            self.entries_by_path = {entry.path: entry for entry in entries}
            self.selected_paths: set[str] = set()

        def compose(self) -> ComposeResult:
            yield Container(
                Static(f"Filter results under {self.root}", classes="dialog-title"),
                Static("Space: select    a: all/none    Enter: delete selected    Esc/q: cancel"),
                Static(f"0/{len(self.paths)} selected", id="filter-result-status"),
                FilterResultsTable(id="filter-results"),
                id="results-dialog",
            )

        def on_mount(self) -> None:
            table = self.query_one("#filter-results", DataTable)
            table.cursor_type = "row"
            table.add_column("Size", key="size")
            table.add_column("Files", key="files")
            table.add_column("Date", key="date")
            table.add_column("Name", key="name")
            for entry in self.entries:
                table.add_row(*self._row_values(entry, selected=False), key=entry.path)
            table.focus()

        def _row_values(self, entry: FilterEntry, selected: bool) -> tuple[str | Text, ...]:
            if entry.size_bytes is None:
                size = "unindexed"
            else:
                size = _human_bytes(entry.size_bytes)
                if not entry.indexed:
                    size = f">={size}"
            if entry.file_count is None:
                files = "-"
            else:
                files = str(entry.file_count)
                if not entry.indexed:
                    files = f">={files}"
            date = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime))
                if entry.mtime
                else "-"
            )
            name = str(Path(entry.path).relative_to(self.root))
            if entry.is_dir:
                name += "/"
            values = (size, files, date, name)
            if not selected:
                return values
            return tuple(Text(value, style="bold black on yellow") for value in values)

        def _current_result(self) -> str | None:
            table = self.query_one("#filter-results", DataTable)
            if table.cursor_row < 0 or table.row_count == 0:
                return None
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            return str(cell_key.row_key.value)

        def _update_result(self, path: str) -> None:
            table = self.query_one("#filter-results", DataTable)
            values = self._row_values(
                self.entries_by_path[path],
                selected=path in self.selected_paths,
            )
            for column, value in zip(("size", "files", "date", "name"), values):
                table.update_cell(path, column, value)

        def _update_status(self) -> None:
            self.query_one("#filter-result-status", Static).update(
                f"{len(self.selected_paths)}/{len(self.paths)} selected"
            )

        def action_toggle_result(self) -> None:
            path = self._current_result()
            if path is None:
                return
            if path in self.selected_paths:
                self.selected_paths.remove(path)
            else:
                self.selected_paths.add(path)
            self._update_result(path)
            self._update_status()

        def action_toggle_all_results(self) -> None:
            if len(self.selected_paths) == len(self.paths):
                self.selected_paths.clear()
            else:
                self.selected_paths = set(self.paths)
            for path in self.paths:
                self._update_result(path)
            self._update_status()

        def action_accept_results(self) -> None:
            if not self.selected_paths:
                self.app.notify("Select at least one filter result.", severity="warning")
                return
            selected = sorted(self.selected_paths)
            preview = "\n".join(selected[:20])
            suffix = "" if len(selected) <= 20 else f"\n... and {len(selected) - 20} more"
            message = (
                f"Permanently delete {len(selected)} filtered item(s)?\n"
                f"{preview}{suffix}\n\n"
                "Press y to confirm, n or Esc to return to selection."
            )

            def after_confirm(confirm: bool) -> None:
                if confirm:
                    self.dismiss(selected)

            self.app.push_screen(ConfirmScreen(message), after_confirm)

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
        #filter-dialog {
            width: 76%;
            height: auto;
            background: #16212d;
            border: round #7dd3fc;
            padding: 1 2;
            align: center middle;
        }
        #results-dialog {
            width: 94%;
            height: 86%;
            background: #16212d;
            border: round #7dd3fc;
            padding: 1 2;
        }
        .dialog-title {
            color: #7dd3fc;
            text-style: bold;
            margin-bottom: 1;
        }
        #filter-result-status {
            color: #facc15;
        }
        #filter-results {
            height: 1fr;
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
            Binding("backspace", "go_parent", "Parent"),
            Binding("alt+left", "go_back", "Back"),
            Binding("alt+right", "go_forward", "Forward"),
            Binding("r", "refresh_current", "Refresh"),
            Binding("f", "filter_paths", "Filter"),
            Binding("x", "cancel_delete", "Cancel Active"),
            Binding("space", "toggle_select", "Select"),
            Binding("delete", "delete_requested", "Delete"),
            Binding("shift+delete", "delete_requested", "Delete"),
            Binding("s", "sort_size", "Sort Size"),
            Binding("c", "sort_count", "Sort Count"),
            Binding("m", "sort_mtime", "Sort Date"),
            Binding("n", "sort_name", "Sort Name"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.service = DuxService(db_path=db_path, max_workers=workers, read_only=True)
            self.current_path = self.service.canonical(path)
            self.navigation_back_stack: list[str] = []
            self.navigation_forward_stack: list[str] = []
            self.sort_by = "size"
            self.reverse = True
            self.rows_by_key: dict[str, bool] = {}
            self.marked_paths: set[str] = set()
            self.delete_slots = threading.BoundedSemaphore(256)
            self.delete_jobs: dict[int, str] = {}
            self.delete_cancel_events: dict[int, threading.Event] = {}
            self.deleting_paths: set[str] = set()
            self.next_delete_job_id = 1
            self.delete_jobs_lock = threading.Lock()
            self.filter_active = False
            self.filter_cancel_event: threading.Event | None = None
            self.refresh_active = False
            self.refresh_path: str | None = None

        @property
        def delete_active(self) -> bool:
            return bool(self.delete_jobs)

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("Ready", id="status")
            yield DuxTable(id="table")
            yield Footer()

        def action_request_quit(self) -> None:
            if self.delete_active:
                self.notify("Delete is still running; wait for it to finish before quitting.", severity="warning")
                return
            if self.filter_active:
                self.notify("Filter is still running; wait for it to finish before quitting.", severity="warning")
                return
            if self.refresh_active:
                self.notify("Refresh is still running; wait for it to finish before quitting.", severity="warning")
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
            if self.service.readonly_warning:
                self._set_status(self.service.readonly_warning)
                self.notify(self.service.readonly_warning, severity="warning")

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
            visible_marked = self.marked_paths.intersection(self.rows_by_key)
            for path in sorted(visible_marked, key=lambda item: (item.count("/"), item)):
                if any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots):
                    continue
                roots.append(path)
            return roots

        def action_open_selected(self) -> None:
            selected = self._selected_path()
            if not selected:
                return
            if self.rows_by_key.get(selected):
                self._navigate_to(selected)

        def _navigate_to(self, destination: str, *, remember: bool = True) -> None:
            destination = self.service.canonical(destination)
            if destination == self.current_path:
                return
            previous = self.current_path
            if remember:
                self.navigation_back_stack.append(previous)
                self.navigation_forward_stack.clear()
            self.marked_paths.clear()
            self.current_path = destination
            focus_path = previous if str(Path(previous).parent) == destination else None
            self._reload_table(focus_path=focus_path)

        def action_go_parent(self) -> None:
            parent = str(Path(self.current_path).parent)
            if parent != self.current_path:
                self._navigate_to(parent)

        def action_go_back(self) -> None:
            if not self.navigation_back_stack:
                self.notify("No backward history.", severity="warning")
                return
            destination = self.navigation_back_stack.pop()
            self.navigation_forward_stack.append(self.current_path)
            self._navigate_to(destination, remember=False)

        def action_go_forward(self) -> None:
            if not self.navigation_forward_stack:
                self.notify("No forward history.", severity="warning")
                return
            destination = self.navigation_forward_stack.pop()
            self.navigation_back_stack.append(self.current_path)
            self._navigate_to(destination, remember=False)

        def action_refresh_current(self) -> None:
            if self.refresh_active:
                self.notify(f"Refresh already running: {self.refresh_path}", severity="warning")
                return
            refresh_path = self.current_path
            self.refresh_active = True
            self.refresh_path = refresh_path
            self._set_status(f"Background refresh started: {refresh_path}")
            self.notify(f"Refreshing {refresh_path} in the background")
            self.run_worker(lambda: self._refresh_current_worker(refresh_path), thread=True)

        def _refresh_current_worker(self, refresh_path: str) -> None:
            refresh_service: DuxService | None = None
            started_at = time.monotonic()
            try:
                refresh_service = DuxService(
                    db_path=self.service.db_path,
                    max_workers=self.service.max_workers,
                )
                refresh_service.index_path(
                    refresh_path,
                    progress=lambda count, current: self.call_from_thread(
                        self._set_status,
                        f"Refreshing {refresh_path}: {count} entries "
                        f"({count / max(time.monotonic() - started_at, 0.001):.0f}/s) current={current}",
                    ),
                    lock_status=lambda owner: self.call_from_thread(
                        self._set_status, f"Refresh waiting for database writer: {owner}"
                    ),
                )
                self.call_from_thread(self._finish_refresh, refresh_path, None)
            except Exception as exc:
                self.call_from_thread(self._finish_refresh, refresh_path, exc)
            finally:
                if refresh_service is not None:
                    refresh_service.close()

        def _finish_refresh(self, refresh_path: str, error: Exception | None) -> None:
            self.refresh_active = False
            self.refresh_path = None
            if error is not None:
                self._set_status(f"Refresh failed: {error}")
                self.notify(f"Refresh failed: {error}", severity="error")
                return
            self._reopen_read_service_after_write()
            if self.current_path == refresh_path:
                self._reload_table()
            self._set_status(f"Background refresh finished: {refresh_path}")
            self.notify(f"Refresh finished: {refresh_path}")

        def _reopen_read_service_after_write(self) -> None:
            if not self.service.immutable_fallback:
                return
            try:
                replacement = DuxService(
                    db_path=self.service.db_path,
                    max_workers=self.service.max_workers,
                    read_only=True,
                )
            except Exception:
                return
            if replacement.immutable_fallback:
                replacement.close()
                return
            previous = self.service
            self.service = replacement
            previous.close()

        def action_filter_paths(self) -> None:
            if self.filter_active:
                self.notify("Filter is already running.", severity="warning")
                return
            def after_query(query: tuple[str, str] | None) -> None:
                if query is None:
                    return
                keyword, exclude = query
                root = self.current_path
                self.filter_active = True
                cancel_event = threading.Event()
                self.filter_cancel_event = cancel_event
                self._set_status(f"Filtering {root} pattern={keyword!r} exclude={exclude!r}...")
                self.run_worker(
                    lambda: self._filter_worker(root, keyword, exclude, cancel_event),
                    thread=True,
                    exclusive=False,
                )

            self.push_screen(FilterQueryScreen(), after_query)

        def _filter_worker(
            self,
            root: str,
            keyword: str,
            exclude: str,
            cancel_event: threading.Event,
        ) -> None:
            try:
                started_at = time.monotonic()

                def progress(scanned_dirs: int, matches: int, current: str) -> None:
                    elapsed = max(time.monotonic() - started_at, 0.001)
                    self.call_from_thread(
                        self._set_status,
                        f"Filtering dirs={scanned_dirs} matches={matches} "
                        f"dirs/s={scanned_dirs / elapsed:.1f} current={current}",
                    )

                result = self.service.filter_paths(
                    root,
                    keyword,
                    exclude=exclude,
                    progress=progress,
                    cancel_event=cancel_event,
                )
                self.call_from_thread(
                    self._finish_filter,
                    root,
                    keyword,
                    exclude,
                    result.paths,
                    result.entries,
                    result.scanned_dirs,
                    result.elapsed_seconds,
                    result.indexed_matches,
                    result.live_only_matches,
                    result.stale_index_matches,
                    None,
                )
            except FilterCancelled:
                self.call_from_thread(
                    self._finish_filter,
                    root,
                    keyword,
                    exclude,
                    [],
                    [],
                    0,
                    0.0,
                    0,
                    0,
                    0,
                    None,
                    True,
                )
            except Exception as exc:
                self.call_from_thread(
                    self._finish_filter,
                    root,
                    keyword,
                    exclude,
                    [],
                    [],
                    0,
                    0.0,
                    0,
                    0,
                    0,
                    exc,
                    False,
                )

        def _finish_filter(
            self,
            root: str,
            keyword: str,
            exclude: str,
            paths: list[str],
            entries: list[FilterEntry],
            scanned_dirs: int,
            elapsed: float,
            indexed_matches: int,
            live_only_matches: int,
            stale_index_matches: int,
            error: Exception | None,
            cancelled: bool = False,
        ) -> None:
            self.filter_active = False
            self.filter_cancel_event = None
            if cancelled:
                self._set_status("Filter cancelled.")
                self.notify("Filter cancelled.")
                return
            if error is not None:
                self._set_status(f"Filter failed: {error}")
                self.notify(f"Filter failed: {error}", severity="error")
                return
            self._set_status(
                f"Filter complete: {len(paths)} match(es), indexed-live={indexed_matches} "
                f"live-only={live_only_matches} stale-db={stale_index_matches}, "
                f"{scanned_dirs} dirs in {elapsed:.1f}s"
            )
            if stale_index_matches:
                self.notify(
                    f"Skipped {stale_index_matches} stale database match(es) missing from the filesystem.",
                    severity="warning",
                )
            if not paths:
                self.notify(
                    f"No paths matching {keyword!r} under {root}; exclude={exclude!r}",
                    severity="warning",
                )
                return

            def after_results(selected: list[str] | None) -> None:
                if not selected:
                    return
                self._start_delete(selected, permanent=True, trash=False)

            self.push_screen(FilterResultsScreen(root, entries), after_results)

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
            self.reverse = True
            self._reload_table()

        def action_delete_requested(self) -> None:
            targets = self._marked_delete_roots()
            deleting_marked = bool(targets)
            if not targets:
                selected = self._selected_path()
                if selected:
                    targets = [selected]
            if not targets:
                return
            preview = "\n".join(targets[:20])
            suffix = "" if len(targets) <= 20 else f"\n... and {len(targets) - 20} more"
            target_text = "selected item(s)" if deleting_marked else "current item"
            message = (
                f"Permanently delete {len(targets)} {target_text}?\n"
                f"{preview}{suffix}\n\n"
                "Press y to confirm, n or Esc to cancel."
            )

            def after(confirm: bool) -> None:
                if not confirm:
                    return
                self._start_delete(targets, permanent=True, trash=False)

            self.push_screen(ConfirmScreen(message), after)

        def _start_delete(self, targets: list[str], *, permanent: bool, trash: bool) -> None:
            with self.delete_jobs_lock:
                conflicts = [
                    target
                    for target in targets
                    if any(
                        target == active
                        or target.startswith(active.rstrip("/") + "/")
                        or active.startswith(target.rstrip("/") + "/")
                        for active in self.deleting_paths
                    )
                ]
                if conflicts:
                    self.notify(
                        f"Already deleting overlapping path: {conflicts[0]}",
                        severity="warning",
                    )
                    return
                job_id = self.next_delete_job_id
                self.next_delete_job_id += 1
                cancel_event = threading.Event()
                self.deleting_paths.update(targets)
                self.delete_jobs[job_id] = f"queued {len(targets)} item(s)"
                self.delete_cancel_events[job_id] = cancel_event
            action = "Moving to trash" if trash else "Deleting"
            self._show_delete_job_status(job_id, f"{action} {len(targets)} item(s)...")
            self.notify(
                f"Delete job {job_id}: {action.lower()} {len(targets)} item(s). UI remains responsive."
            )
            self.run_worker(
                lambda: self._delete_worker(
                    job_id,
                    targets,
                    cancel_event,
                    permanent=permanent,
                    trash=trash,
                ),
                thread=True,
                exclusive=False,
            )

        def _show_delete_job_status(self, job_id: int, message: str) -> None:
            with self.delete_jobs_lock:
                if job_id in self.delete_jobs:
                    self.delete_jobs[job_id] = message
            self._render_delete_status()

        def _render_delete_status(self) -> bool:
            with self.delete_jobs_lock:
                if not self.delete_jobs:
                    return False
                cancellable = [
                    job_id
                    for job_id, cancel_event in self.delete_cancel_events.items()
                    if not cancel_event.is_set()
                ]
                focus_job_id = max(cancellable) if cancellable else max(self.delete_jobs)
                message = self.delete_jobs[focus_job_id]
                active_count = len(self.delete_jobs)
                cancelling_count = sum(
                    cancel_event.is_set() for cancel_event in self.delete_cancel_events.values()
                )
            cancelling = f" cancelling={cancelling_count}" if cancelling_count else ""
            self._set_status(
                f"Delete jobs={active_count}{cancelling} | job {focus_job_id}: {message}"
            )
            return True

        def action_cancel_delete(self) -> None:
            if self.filter_active:
                cancel_event = self.filter_cancel_event
                if cancel_event is not None and not cancel_event.is_set():
                    cancel_event.set()
                    self._set_status("Cancelling filter...")
                    self.notify("Filter cancellation requested.")
                else:
                    self.notify("Filter is already cancelling.", severity="warning")
                return
            with self.delete_jobs_lock:
                cancellable = [
                    job_id
                    for job_id, cancel_event in self.delete_cancel_events.items()
                    if not cancel_event.is_set()
                ]
                active_count = len(self.delete_jobs)
                job_id = max(cancellable) if cancellable else None
                if job_id is not None:
                    self.delete_cancel_events[job_id].set()
                    self.delete_jobs[job_id] = "cancel requested; synchronizing index"
            if not active_count:
                self.notify("No active delete jobs.", severity="warning")
                return
            if job_id is None:
                self.notify("All active delete jobs are already cancelling.", severity="warning")
                return
            self._render_delete_status()
            self.notify(f"Cancellation requested for latest delete job {job_id}.")

        def _delete_worker(
            self,
            job_id: int,
            targets: list[str],
            cancel_event: threading.Event,
            *,
            permanent: bool,
            trash: bool,
        ) -> None:
            completed: list[str] = []
            delete_service: DuxService | None = None
            try:
                action = "Moving" if trash else "Deleting"
                target_workers = min(8, max(1, len(targets)))
                unlink_workers = max(1, 256 // target_workers)
                self.call_from_thread(
                    self._show_delete_job_status,
                    job_id,
                    f"{action} {len(targets)} item(s), concurrency=256",
                )
                totals = {target: self._delete_total(self.service, target) for target in targets}
                started_at = time.monotonic()
                latest_counts = {target: 0 for target in targets}
                progress_lock = threading.Lock()

                def progress(target: str, count: int, path: str) -> None:
                    with progress_lock:
                        latest_counts[target] = max(latest_counts[target], count)
                        processed = sum(latest_counts.values())
                        elapsed = max(time.monotonic() - started_at, 0.001)
                        rate = processed / elapsed
                        known_total = all(total is not None for total in totals.values())
                        total = sum(int(value) for value in totals.values()) if known_total else None
                    total_text = "?" if total is None else str(total)
                    pct_text = "" if total is None else f" {min(100.0, processed * 100.0 / total):5.1f}%"
                    eta_text = " ETA=?" if total is None or rate <= 0 else f" ETA={_eta((total - processed) / rate)}"
                    self.call_from_thread(
                        self._show_delete_job_status,
                        job_id,
                        f"Deleting {_progress_bar(processed, total)}{pct_text} "
                        f"{processed}/{total_text} {rate:.1f}/s{eta_text} current={path}",
                    )

                def status(target: str, phase: str) -> None:
                    if phase.startswith("waiting-lock:"):
                        owner = phase.removeprefix("waiting-lock:")
                        self.call_from_thread(
                            self._show_delete_job_status,
                            job_id,
                            f"Waiting for database writer: {owner}",
                        )
                    elif phase == "flushing-index":
                        self.call_from_thread(
                            self._show_delete_job_status, job_id, f"Flushing index updates: {target}"
                        )
                    elif phase == "syncing-index-after-delete":
                        self.call_from_thread(
                            self._show_delete_job_status,
                            job_id,
                            f"Files deleted; synchronizing index: {target}",
                        )
                    elif phase.startswith("index-sync-deferred:"):
                        reason = phase.removeprefix("index-sync-deferred:")
                        self.call_from_thread(
                            self._show_delete_job_status,
                            job_id,
                            f"Files deleted; index refresh required: {reason}",
                        )
                    elif phase == "index-synced":
                        self.call_from_thread(
                            self._show_delete_job_status, job_id, f"Index synchronized: {target}"
                        )

                filesystem_first = self.service.immutable_fallback
                if not filesystem_first:
                    try:
                        delete_service = DuxService(
                            db_path=self.service.db_path,
                            max_workers=self.service.max_workers,
                            delete_slots=self.delete_slots,
                        )
                    except Exception as exc:
                        if not permanent or not db.is_storage_full_error(exc):
                            raise
                        filesystem_first = True

                if filesystem_first:
                    self.call_from_thread(
                        self._show_delete_job_status,
                        job_id,
                        "Database storage is full; deleting files before index synchronization",
                    )
                    result = self.service.delete_paths_filesystem_first(
                        targets,
                        progress=progress,
                        status=status,
                        progress_interval=1000,
                        workers=target_workers,
                        unlink_workers=unlink_workers,
                        cancel_event=cancel_event,
                    )
                    completed = result.completed_targets
                    self.call_from_thread(
                        self._finish_delete,
                        job_id,
                        targets,
                        completed,
                        result.error,
                        result.cancelled and result.error is None,
                        result.index_synchronized,
                    )
                    return

                delete_service.delete_paths(
                    targets,
                    permanent=permanent,
                    trash=trash,
                    progress=None if trash else progress,
                    status=status,
                    progress_interval=1000,
                    workers=target_workers,
                    unlink_workers=unlink_workers,
                    cancel_event=cancel_event,
                )
                completed = targets
                self.call_from_thread(
                    self._finish_delete, job_id, targets, completed, None, False, True
                )
            except DeleteCancelled as exc:
                self.call_from_thread(
                    self._finish_delete,
                    job_id,
                    targets,
                    exc.completed_targets,
                    None,
                    True,
                    True,
                )
            except Exception as exc:
                self.call_from_thread(
                    self._finish_delete, job_id, targets, completed, exc, False, True
                )
            finally:
                if delete_service is not None:
                    delete_service.close()

        def _delete_total(self, service: DuxService, target: str) -> int | None:
            row = service.get_node(target)
            if row is None:
                return None
            return int(row["file_count"]) + int(row["dir_count"]) + 1

        def _finish_delete(
            self,
            job_id: int,
            targets: list[str],
            completed: list[str],
            error: Exception | None,
            cancelled: bool,
            index_synchronized: bool,
        ) -> None:
            with self.delete_jobs_lock:
                self.delete_jobs.pop(job_id, None)
                self.delete_cancel_events.pop(job_id, None)
                self.deleting_paths.difference_update(targets)
                remaining_jobs = len(self.delete_jobs)
            for target in completed:
                self.marked_paths.discard(target)
            self.marked_paths = {
                path
                for path in self.marked_paths
                if not any(path == target or path.startswith(target.rstrip("/") + "/") for target in completed)
            }
            if cancelled:
                suffix = "index synchronized" if index_synchronized else "index refresh required"
                outcome = f"Delete job {job_id} cancelled; {suffix}."
                self.notify(outcome, severity="warning" if not index_synchronized else "information")
            elif error is not None:
                if completed and not index_synchronized:
                    outcome = (
                        f"Delete job {job_id} removed {len(completed)} item(s), "
                        f"but index refresh is required: {error}"
                    )
                    self.notify(outcome, severity="warning")
                else:
                    outcome = f"Delete job {job_id} failed: {error}"
                    self.notify(outcome, severity="error")
            else:
                suffix = "" if index_synchronized else "; index refresh required"
                outcome = f"Delete job {job_id} finished: {len(completed)} item(s){suffix}"
                self.notify(outcome, severity="warning" if not index_synchronized else "information")
            if completed:
                self._reopen_read_service_after_write()
            if remaining_jobs:
                self._render_delete_status()
            else:
                self._set_status(outcome)
            self._reload_table()

    DuxApp().run()
