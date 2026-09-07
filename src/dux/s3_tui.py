from __future__ import annotations

import threading
import time

from .s3 import S3Browser, S3DeleteCancelled, S3Entry, canonical_s3_uri, s3_parent
from .s3_index import S3IndexStore


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if value < 1024.0 or unit == "P":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024.0
    return f"{size}B"


def _sort_entries(
    entries: tuple[S3Entry, ...], sort_by: str, reverse: bool
) -> tuple[S3Entry, ...]:
    attributes = {
        "size": "size_bytes",
        "count": "object_count",
        "mtime": "mtime",
    }
    attribute = attributes[sort_by]
    known = [entry for entry in entries if getattr(entry, attribute) is not None]
    missing = [entry for entry in entries if getattr(entry, attribute) is None]
    known.sort(key=lambda entry: entry.name.casefold())
    known.sort(key=lambda entry: getattr(entry, attribute), reverse=reverse)
    missing.sort(key=lambda entry: entry.name.casefold())
    return tuple(known + missing)


def run_s3_ui(
    uri: str,
    workers: int,
    *,
    browser: S3Browser | None = None,
    stats_db_path: str | None = None,
) -> None:
    try:
        from rich.text import Text
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Container
        from textual.screen import ModalScreen
        from textual.widgets import DataTable, Footer, Header, Label, Static
    except ImportError as exc:
        raise SystemExit("textual is required for `dux ui`; install project dependencies first") from exc

    s3_browser = (
        browser
        if browser is not None
        else S3Browser(
            max_workers=workers,
            stats_store=S3IndexStore(stats_db_path),
        )
    )

    class ConfirmScreen(ModalScreen[bool]):
        def __init__(self, message: str) -> None:
            super().__init__()
            self.message = message

        def compose(self) -> ComposeResult:
            yield Container(
                Static(self.message, markup=False),
                Label("Press y to confirm, n or Esc to cancel"),
                id="dialog",
            )

        def key_y(self) -> None:
            self.dismiss(True)

        def key_n(self) -> None:
            self.dismiss(False)

        def key_escape(self) -> None:
            self.dismiss(False)

    class S3Table(DataTable):
        def on_key(self, event) -> None:
            actions = {
                "enter": self.app.action_open_selected,
                "right": self.app.action_open_selected,
                "backspace": self.app.action_go_parent,
                "space": self.app.action_toggle_select,
                "delete": self.app.action_delete_requested,
                "x": self.app.action_cancel_delete,
                "X": self.app.action_cancel_delete,
                "s": self.app.action_sort_size,
                "c": self.app.action_sort_count,
                "m": self.app.action_sort_mtime,
                "q": self.app.action_request_quit,
                "ctrl+c": self.app.action_request_quit,
            }
            action = actions.get(event.key)
            if action is not None:
                event.stop()
                action()

    class S3App(App):
        CSS = """
        Screen {
            background: #071a1d;
            color: #d7e8df;
        }
        Header, Footer {
            background: #123b3d;
            color: #f5e6b3;
        }
        DataTable {
            height: 1fr;
        }
        #status {
            height: 1;
            background: #10282b;
            color: #facc15;
            padding: 0 1;
        }
        #dialog {
            width: 94%;
            max-height: 85%;
            margin: 2 3;
            padding: 1 2;
            border: heavy #d97706;
            background: #10282b;
        }
        """
        BINDINGS = [
            Binding("q", "request_quit", "Quit"),
            Binding("ctrl+c", "request_quit", "Quit"),
            Binding("enter", "open_selected", "Open"),
            Binding("backspace", "go_parent", "Parent"),
            Binding("alt+left", "go_back", "Back"),
            Binding("alt+right", "go_forward", "Forward"),
            Binding("r", "refresh", "Refresh"),
            Binding("space", "toggle_select", "Select"),
            Binding("delete", "delete_requested", "Delete"),
            Binding("x", "cancel_delete", "Cancel"),
            Binding("shift+x", "cancel_delete", "Cancel", show=False),
            Binding("s", "sort_size", "Sort Size"),
            Binding("c", "sort_count", "Sort Count"),
            Binding("m", "sort_mtime", "Sort Date"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.browser = s3_browser
            self.current_uri = canonical_s3_uri(uri)
            self.back_stack: list[str] = []
            self.forward_stack: list[str] = []
            self.entries: tuple[S3Entry, ...] = ()
            self.rows_by_uri: dict[str, S3Entry] = {}
            self.marked_uris: set[str] = set()
            self.sort_by = "size"
            self.reverse = True
            self.last_sort_key: str | None = None
            self.load_generation = 0
            self.delete_active = False
            self.delete_cancel_event: threading.Event | None = None

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("Ready", id="status")
            yield S3Table(id="table")
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one(DataTable)
            table.cursor_type = "row"
            table.add_columns("Type", "Size", "Objects", "Date", "Name")
            self._load_current()

        def _set_status(self, message: str) -> None:
            self.query_one("#status", Static).update(message)

        def _load_current(self, *, force: bool = False, focus_uri: str | None = None) -> None:
            self.load_generation += 1
            generation = self.load_generation
            current = self.current_uri
            table = self.query_one(DataTable)
            table.clear()
            table.add_row("", "", "", "", "Loading...", key="__loading__")
            self.rows_by_uri.clear()
            self.title = current
            self._set_status(f"Loading {current}...")

            def worker() -> None:
                try:
                    listing = self.browser.list_children(current, force=force)
                    self.call_from_thread(
                        self._finish_load, current, generation, listing.entries, listing.cached, focus_uri, None
                    )
                except BaseException as exc:
                    self.call_from_thread(
                        self._finish_load, current, generation, (), False, focus_uri, exc
                    )

            self.run_worker(worker, thread=True, exclusive=False)

        def _finish_load(
            self,
            loaded_uri: str,
            generation: int,
            entries: tuple[S3Entry, ...],
            cached: bool,
            focus_uri: str | None,
            error: BaseException | None,
        ) -> None:
            if generation != self.load_generation or loaded_uri != self.current_uri:
                return
            table = self.query_one(DataTable)
            table.clear()
            self.rows_by_uri.clear()
            if error is not None:
                table.add_row("", "", "", "", f"Error: {error}", key="__error__")
                self._set_status(f"Unable to list {loaded_uri}: {error}")
                self.notify(str(error), severity="error")
                return
            self.entries = _sort_entries(entries, self.sort_by, self.reverse)
            focus_row: int | None = None
            for index, entry in enumerate(self.entries):
                marked = entry.uri in self.marked_uris
                style = "bold black on yellow" if marked else ""
                kind = "DIR" if entry.is_dir else "OBJECT"
                size = "-" if entry.size_bytes is None else _human_bytes(entry.size_bytes)
                if entry.is_dir and entry.size_bytes is not None and not entry.size_complete:
                    size = f">={size}"
                objects = "-" if entry.object_count is None else str(entry.object_count)
                if entry.is_dir and entry.object_count is not None and not entry.size_complete:
                    objects = f">={objects}"
                date = (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime))
                    if entry.mtime is not None
                    else "-"
                )
                name = entry.name + ("/" if entry.is_dir else "")
                table.add_row(
                    Text(kind, style=style),
                    Text(size, style=style),
                    Text(objects, style=style),
                    Text(date, style=style),
                    Text(name, style=style),
                    key=entry.uri,
                )
                self.rows_by_uri[entry.uri] = entry
                if entry.uri == focus_uri:
                    focus_row = index
            if not self.entries:
                table.add_row("", "", "", "", "(empty)", key="__empty__")
            if focus_row is not None:
                table.move_cursor(row=focus_row, column=0, animate=False)
            source = "cache" if cached else "remote"
            sort_name = {"size": "size", "count": "objects", "mtime": "date"}[
                self.sort_by
            ]
            direction = "desc" if self.reverse else "asc"
            self._set_status(
                f"{len(self.entries)} item(s) from {source} | "
                f"sort={sort_name} {direction} | unindexed last"
            )

        def _selected_entry(self) -> S3Entry | None:
            table = self.query_one(DataTable)
            if table.row_count == 0 or table.cursor_row < 0:
                return None
            key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            return self.rows_by_uri.get(key)

        def _redraw(self, focus_uri: str | None = None) -> None:
            self._finish_load(
                self.current_uri,
                self.load_generation,
                self.entries,
                True,
                focus_uri,
                None,
            )

        def action_toggle_select(self) -> None:
            entry = self._selected_entry()
            if entry is None:
                return
            if entry.uri in self.marked_uris:
                self.marked_uris.remove(entry.uri)
            else:
                self.marked_uris.add(entry.uri)
            self._redraw(entry.uri)

        def action_open_selected(self) -> None:
            entry = self._selected_entry()
            if entry is not None and entry.is_dir:
                self._navigate_to(entry.uri)

        def _navigate_to(self, destination: str, *, remember: bool = True) -> None:
            destination = canonical_s3_uri(destination)
            if destination == self.current_uri:
                return
            previous = self.current_uri
            if remember:
                self.back_stack.append(previous)
                self.forward_stack.clear()
            self.current_uri = destination
            self.marked_uris.clear()
            focus = previous if s3_parent(previous) == destination else None
            self._load_current(focus_uri=focus)

        def action_go_parent(self) -> None:
            parent = s3_parent(self.current_uri)
            if parent == self.current_uri:
                self.notify("Already at the bucket root.")
                return
            self._navigate_to(parent)

        def action_go_back(self) -> None:
            if not self.back_stack:
                self.notify("No backward history.")
                return
            destination = self.back_stack.pop()
            self.forward_stack.append(self.current_uri)
            self._navigate_to(destination, remember=False)

        def action_go_forward(self) -> None:
            if not self.forward_stack:
                self.notify("No forward history.")
                return
            destination = self.forward_stack.pop()
            self.back_stack.append(self.current_uri)
            self._navigate_to(destination, remember=False)

        def action_refresh(self) -> None:
            self.browser.invalidate(self.current_uri)
            self._load_current(force=True)

        def action_sort_size(self) -> None:
            self._apply_sort("size")

        def action_sort_count(self) -> None:
            self._apply_sort("count")

        def action_sort_mtime(self) -> None:
            self._apply_sort("mtime")

        def _apply_sort(self, sort_by: str) -> None:
            if self.last_sort_key == sort_by:
                self.reverse = not self.reverse
            else:
                self.reverse = True
            self.sort_by = sort_by
            self.last_sort_key = sort_by
            focus = self._selected_entry()
            self._redraw(focus.uri if focus is not None else None)

        def action_delete_requested(self) -> None:
            if self.delete_active:
                self.notify("An S3 delete is already running.", severity="warning")
                return
            selected = [
                entry for entry in self.entries if entry.uri in self.marked_uris
            ]
            if not selected:
                current = self._selected_entry()
                if current is not None:
                    selected = [current]
            if not selected:
                return
            lines = [f"src: {entry.uri}\ndst: PERMANENT DELETE" for entry in selected[:20]]
            suffix = "" if len(selected) <= 20 else f"\n... and {len(selected) - 20} more"
            message = (
                f"Permanently delete {len(selected)} S3 item(s)?\n\n"
                + "\n\n".join(lines)
                + suffix
            )

            def after(confirm: bool) -> None:
                if confirm:
                    self._start_delete(selected)

            self.push_screen(ConfirmScreen(message), after)

        def _start_delete(self, entries: list[S3Entry]) -> None:
            self.delete_active = True
            cancel_event = threading.Event()
            self.delete_cancel_event = cancel_event
            self._set_status(f"Preparing to delete {len(entries)} selected item(s)...")

            def worker() -> None:
                try:
                    def progress(phase: str, completed: int, total: int | None, current: str) -> None:
                        if total is None:
                            message = f"Delete listing prefixes={completed} current={current}"
                        else:
                            message = f"Deleting {completed}/{total} current={current}"
                        self.call_from_thread(self._set_status, message)

                    deleted = self.browser.delete_entries(
                        entries,
                        cancel_event=cancel_event,
                        progress=progress,
                    )
                    self.call_from_thread(self._finish_delete, deleted, None)
                except S3DeleteCancelled as exc:
                    self.call_from_thread(self._finish_delete, 0, exc)
                except BaseException as exc:
                    self.call_from_thread(self._finish_delete, 0, exc)

            self.run_worker(worker, thread=True, exclusive=False)

        def _finish_delete(self, deleted: int, error: BaseException | None) -> None:
            self.delete_active = False
            self.delete_cancel_event = None
            self.marked_uris.clear()
            self.browser.invalidate(self.current_uri)
            if isinstance(error, S3DeleteCancelled):
                self.notify("S3 delete cancelled; refreshing the current prefix.")
            elif error is not None:
                self.notify(f"S3 delete failed: {error}", severity="error")
            else:
                self.notify(f"Deleted {deleted} S3 object(s).")
            self._load_current(force=True)

        def action_cancel_delete(self) -> None:
            if self.delete_cancel_event is None:
                self.notify("No S3 delete is running.")
                return
            self.delete_cancel_event.set()
            self._set_status("Cancelling S3 delete...")

        def action_request_quit(self) -> None:
            if self.delete_active:
                self.notify("S3 delete is still running; cancel or wait before quitting.", severity="warning")
                return
            self.exit()

    S3App().run(mouse=False)
