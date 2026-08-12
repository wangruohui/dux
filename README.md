# dux

Fast disk usage indexing, terminal visualization, and cleanup management.

快速磁盘占用索引、终端可视化浏览和空间清理管理工具。

```text
dux ui /data/project

Path: /data/project                         Sort: size
┌──────────┬───────────┬────────────┬──────────────────────────────┬────────────────────┐
│ Size     │ Files     │ Modified   │ Name                         │ Usage              │
├──────────┼───────────┼────────────┼──────────────────────────────┼────────────────────┤
│ 812.4G   │ 1,204,882 │ 2026-06-21 │ checkpoints/                 │ [================] │
│ 243.8G   │    88,430 │ 2026-06-18 │ datasets/                    │ [====            ] │
│  38.2G   │ 3,918,221 │ 2026-06-20 │ [x] logs/                    │ [=               ] │
│   9.7G   │    14,006 │ 2026-06-22 │ tmp/                         │ [                ] │
└──────────┴───────────┴────────────┴──────────────────────────────┴────────────────────┘

Enter open  Backspace parent  Space select  Del/Shift+Del delete
s size  c count  m date  n name  r refresh  f filter  x cancel latest  q quit
```

`dux` answers the cleanup question quickly: what is using space, how many files are there, what changed, and what can be safely removed?

`dux` 解决清理空间前最常见的问题：哪里占空间、文件数有多少、最近是否变化、哪些内容可以安全清理。

## Highlights / 亮点

- **Multi-threaded indexing**: scans directory trees with worker threads and stores aggregate metadata in SQLite.
- **多线程统计**：使用 worker 线程遍历目录树，把聚合后的大小、文件数、目录数写入 SQLite。
- **Terminal UI**: browse large trees over SSH without a desktop environment.
- **终端可视化**：纯命令行 UI，适合 SSH 和服务器环境。
- **Sort by the metric that matters**: size, recursive file count, modification time, or name.
- **多维排序**：支持按大小、递归文件数、修改时间、名称排序。
- **Size and file count together**: find both storage-heavy and inode-heavy directories.
- **大小和文件数同时展示**：既能找占容量的目录，也能找小文件数量爆炸的目录。
- **Local refresh**: `dux index /some/subtree` refreshes only that subtree and propagates deltas to indexed parents.
- **局部刷新**：对变化的子树重新 `index` 即可，父路径聚合值会自动更新。
- **Partial navigation**: ancestors of indexed subtrees are kept as navigation placeholders; unindexed live entries are shown as `unindexed`.
- **部分索引导航**：已统计子树的父路径会保留导航骨架，未统计的现场条目标记为 `unindexed`。
- **Cursor and batch delete**: use `Delete` or `Shift+Delete` to delete the current row, or `Space` to mark multiple rows and delete them together.
- **光标和批量删除**：`Delete` 或 `Shift+Delete` 删除当前行；也可以用 `Space` 标记多行后一起删除。
- **Responsive deletion**: deletion runs in background workers with a status line showing progress, rate, current path, and index-sync phase; press `x` to cancel the latest active delete job.
- **响应式删除**：删除在后台 worker 中执行，状态栏会显示进度、速度、当前路径和索引同步阶段；按 `x` 可取消最近启动的删除任务。
- **Parallel cleanup**: multiple selected roots can be deleted concurrently; each directory tree is scanned and unlinked with worker threads.
- **并行清理**：多个选中根目录可以并发删除；单个目录树内部也会用 worker 线程并行扫描和 unlink。
- **Explicit confirmation**: destructive UI deletes always show a confirmation dialog before running.
- **明确确认**：UI 中的破坏性删除都会先弹出确认框。
- **Persistent default database**: default DB is `~/.cache/dux/dux.db`; use `--db` for project-specific indexes.
- **默认持久数据库**：默认数据库是 `~/.cache/dux/dux.db`，也可以用 `--db` 指定项目数据库。
- **Simple install**: Python + SQLite; no desktop stack or custom C database runtime required.
- **安装简单**：只依赖 Python 和 SQLite，不需要桌面环境或额外 C 数据库运行时。

## Quick Start / 快速开始

Install from a checkout:

从源码目录安装：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Index a directory:

统计一个目录：

```bash
dux index /data/project
```

Open the terminal UI:

打开终端 UI：

```bash
dux ui /data/project
```

List children from the CLI:

在命令行列出子目录/文件：

```bash
dux ls /data/project --sort size
dux ls /data/project --sort count
dux ls /data/project --sort mtime
dux ls /data/project --sort name
```

Refresh only a changed subtree:

只刷新发生变化的子树：

```bash
dux index /data/project/checkpoints
```

Delete while keeping the index consistent:

删除并同步更新索引：

```bash
dux delete /data/project/tmp/run-001 --trash
dux delete /data/project/tmp/run-001 --permanent
```

Use a specific database:

使用指定数据库：

```bash
dux --db ~/.cache/dux/project.db index /data/project
dux --db ~/.cache/dux/project.db ui /data/project
```

Tune concurrency:

调整并发：

```bash
dux --workers 16 index /data/project
```

## UI Controls / UI 快捷键

- `Enter` / `Right`: open selected directory.
- `Enter` / `Right`：进入当前目录。
- `Backspace` / `Left`: go to parent directory.
- `Backspace` / `Left`：返回父目录。
- `Space`: select or unselect the current row; selected rows are highlighted and prefixed with `[x]`.
- `Space`：选择或取消选择当前行；选中行会高亮并显示 `[x]`。
- `Delete` / `Shift+Delete`: delete marked rows if any rows are marked; otherwise delete the current row. Files within a directory and multiple delete jobs run concurrently while sharing a global concurrency limit of 256. Press `y` to confirm, `n` or `Esc` to cancel.
- `Delete` / `Shift+Delete`：如果有标记行，则删除所有标记行；否则删除当前光标行。目录内部文件和多个删除任务都会并行删除，并共享全局 256 并发限制。按 `y` 确认，按 `n` 或 `Esc` 取消。
- `r`: refresh the current subtree.
- `r`：刷新当前子树。
- `f`: recursively find file/directory basenames using shell globs such as `a*`, with optional exclude-path pruning; it remains available while deletion runs.
- `f`：在当前目录下递归使用 `a*` 等 shell 通配符匹配文件或目录 basename，可填写 exclude 关键字剪枝；删除期间仍可使用。
- `x`: cancel the most recently started job that has not already received a cancellation request. Press repeatedly to cancel the remaining jobs one by one. Completed filesystem deletions are flushed to SQLite before each cancellation finishes.
- `x`：取消最近启动且尚未请求取消的任务；重复按下可依次取消其余任务。每个任务取消完成前，已经删除的文件和目录都会同步写入 SQLite。
- `s`: sort by size.
- `s`：按大小排序。
- `c`: sort by recursive file count.
- `c`：按递归文件数排序。
- `m`: sort by modification time.
- `m`：按修改时间排序。
- `n`: sort by name.
- `n`：按名称排序。
- `q`: quit.
- `q`：退出。

The graph column follows the active metric: size mode uses bytes, count mode uses recursive file count.

右侧比例条跟随当前排序指标：大小模式按 bytes，文件数模式按递归文件数。

All sort keys use descending order, and unindexed entries are always listed after indexed entries.

所有排序键都按从大到小排列，未统计项始终排在已统计项之后。

During deletion, the status line reports the active phase. File removal shows a progress bar, processed entry count, throughput, ETA, and current path. Successfully removed entries are continuously sent to a batched SQLite writer, which deletes their nodes and propagates size/file/directory deltas to every indexed parent. Cancelling does not rescan the target.

删除过程中，状态栏会显示当前阶段。文件删除阶段会显示进度条、已处理条目数、吞吐、ETA 和当前路径。成功删除的条目会持续投递给 SQLite writer 批量删节点，并把大小、文件数和目录数变化同步到所有已索引父级；取消时不会重新扫描目标。

Filter matching is case-sensitive and applies a shell glob to each entry's basename only; `/` is not part of the match. For example, `a*` matches names beginning with `a`. When a directory matches, it is returned and its descendants are not scanned, following `find ... -prune` semantics. If the relative path contains the exclude keyword, that entry is skipped; excluded directories are not entered.

筛选只对每个条目的 basename 做大小写敏感的 shell 通配符匹配，不涉及 `/`；例如 `a*` 匹配所有以 `a` 开头的名称。目录命中后返回该目录且不再扫描其子目录，语义与 `find ... -prune` 一致。相对路径包含 exclude 关键字的条目会被跳过，其中目录不会继续进入。

In the filtered-result delete confirmation, `n` or `Esc` returns to the result table with the previous selections preserved; only `y` closes the result table and starts deletion.

在筛选结果的删除确认中，按 `n` 或 `Esc` 会返回结果表并保留原选择；只有按 `y` 才会关闭结果表并开始删除。

## Indexing Semantics / 索引语义

`dux index` is both the initial indexing command and the refresh command.

`dux index` 同时用于首次统计和局部刷新。

If a path is new, it is inserted. If it already exists, that subtree is replaced and the size/count delta is propagated to indexed parents.

如果路径是新的，会插入索引；如果已经存在，会替换该子树，并把大小/文件数变化同步到已索引的父路径。

During indexing, `dux` scans into a temporary staging SQLite database first, then swaps the finished subtree into the main database in a short transaction. This keeps `dux ui` usable during long scans; the UI reads the previous committed snapshot until the final swap lands.

索引过程中，`dux` 会先扫描到临时 staging SQLite 数据库，再用一个短事务把完成的子树替换进主数据库。因此长时间扫描时 `dux ui` 仍可使用；最终 swap 完成前，UI 读取的是上一个已提交快照。

UI startup does not refresh navigation placeholders or perform any other bookkeeping write, so it can open while an index merge holds the SQLite writer lock.

UI 启动时不会刷新导航占位或执行其他维护写入，因此索引合并持有 SQLite 写锁时仍可打开。

Ancestors of an indexed subtree are stored as `indexed=0` placeholders so the UI can navigate upward even when only a subtree has been scanned.

已统计子树的祖先路径会以 `indexed=0` 占位形式保存，因此即使只统计了一个子目录，UI 也可以向上导航。

When the UI visits a directory that is only partially indexed, indexed children are always shown from the DB, and a limited number of live filesystem entries are shown as `unindexed`.

当 UI 打开部分索引目录时，已索引的子项总是从 DB 展示；未统计的现场文件系统条目会限量显示，并标记为 `unindexed`。

Progress is printed every 10,000 scanned files by default:

默认每扫描 10,000 个文件输出一次进度：

```text
scanned_files=10000 current=/data/project/checkpoints/run-42/model.bin
scanned_files=20000 current=/data/project/logs/train/events.out
```

The final line includes throughput:

结束时会输出吞吐：

```text
indexed /data/project size=128849018880 files=240381 dirs=1842 elapsed=12.431s files_per_sec=19336.2 dirs_per_sec=148.2
```

## Worker Count / 并发设置

The default scanner concurrency is **256 worker threads**. This keeps many metadata requests in flight on large directory trees; use `--workers` to tune it for a different filesystem.

默认扫描并发是 **256 个 worker 线程**，可在大型目录树上同时发出更多元数据请求；不同文件系统可通过 `--workers` 调整。

More threads are not always faster. In the measured directory-heavy tree, 256 workers outperformed 128, while 512 regressed because the metadata service was already saturated.

线程越多不一定越快。在实测的多目录树中，256 个 worker 快于 128 个，而 512 个因元数据服务达到饱和反而变慢。

## Install Options / 安装方式

Minimal system packages:

最小系统依赖：

```bash
sudo apt-get install -y python3 python3-venv python3-pip sqlite3
```

If `python3-venv` is unavailable, `uv` also works:

如果没有 `python3-venv`，也可以使用 `uv`：

```bash
uv venv .venv
. .venv/bin/activate
uv pip install -e .
```

Optional wrapper:

可选：安装 shell wrapper：

```bash
mkdir -p ~/bin
ln -sf "$(pwd)/.venv/bin/dux" ~/bin/dux
```

After that, run `dux` without activating the environment.

之后无需手动激活环境即可运行 `dux`。

## Data Model / 数据模型

The index stores one row per path:

索引中每个路径对应一行：

```text
path
parent_path
name
is_dir
indexed
depth
size_bytes
file_count
dir_count
updated_at
```

This model makes common operations straightforward: list children, sort by size/count/date/name, refresh one subtree, update parent totals, navigate through partially indexed ancestors, and keep the DB consistent after deletes.

这个模型让常用操作更直接：列出子项、按大小/文件数/日期/名称排序、刷新单个子树、更新父路径聚合值、在部分索引祖先间导航，并在删除后保持数据库一致。

Subtree delete uses the path primary key as a range index: `path = root OR root/ <= path < root0`. The service reads the root aggregate once, deletes the subtree rows in SQLite, then applies one delta to ancestors instead of recomputing every parent from scratch.

子树删除会利用 path 主键做范围删除：`path = root OR root/ <= path < root0`。服务只读取一次根节点聚合值，在 SQLite 中删除整棵子树后，再把一个 delta 应用到祖先路径，而不是逐层重新统计。

## Development / 开发

Run tests:

运行测试：

```bash
python -m unittest discover -s tests -v
```

Run syntax checks:

运行语法检查：

```bash
python -m compileall -q src tests
```

Architecture notes live in [ARCHITECTURE.md](ARCHITECTURE.md).

架构说明见 [ARCHITECTURE.md](ARCHITECTURE.md)。
