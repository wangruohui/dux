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

Enter open  Backspace parent  Space select  Shift+Del delete selected
s size  c count  m date  n name  r refresh  d trash  D delete  q quit
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
- **Selection and batch delete**: use `Space` to mark rows and `Shift+Delete` to delete selected items after confirmation.
- **多选批量删除**：`Space` 标记多行，`Shift+Delete` 确认后批量永久删除。
- **Responsive deletion**: deletion runs in background workers with a status line showing progress, rate, current path, and index-update phase.
- **响应式删除**：删除在后台 worker 中执行，状态栏会显示进度、速度、当前路径和索引更新阶段。
- **Parallel cleanup**: multiple selected roots can be deleted concurrently; each directory tree is scanned and unlinked with worker threads.
- **并行清理**：多个选中根目录可以并发删除；单个目录树内部也会用 worker 线程并行扫描和 unlink。
- **Safe trash flow**: move a single item to `~/trash` with `d`, or explicitly choose permanent deletion.
- **安全清理流程**：单项可用 `d` 移动到 `~/trash`，永久删除需要明确触发。
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
- `Shift+Delete`: open a confirmation dialog for selected rows; press `y` to confirm, `n` or `Esc` to cancel.
- `Shift+Delete`：打开选中项删除确认弹窗；按 `y` 确认，按 `n` 或 `Esc` 取消。
- `d`: move the current row to `~/trash`.
- `d`：把当前项移动到 `~/trash`。
- `D`: permanently delete the current row after confirmation.
- `D`：确认后永久删除当前项。
- `r`: refresh the current subtree.
- `r`：刷新当前子树。
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

During deletion, the status line reports the active phase. File removal shows a progress bar, processed entry count, throughput, and current path; after files are removed, `Updating index...` means SQLite is removing the subtree rows and propagating parent totals.

删除过程中，状态栏会显示当前阶段。文件删除阶段会显示进度条、已处理条目数、吞吐和当前路径；文件删完后出现 `Updating index...` 表示 SQLite 正在删除子树索引并同步父级聚合值。

## Indexing Semantics / 索引语义

`dux index` is both the initial indexing command and the refresh command.

`dux index` 同时用于首次统计和局部刷新。

If a path is new, it is inserted. If it already exists, that subtree is replaced and the size/count delta is propagated to indexed parents.

如果路径是新的，会插入索引；如果已经存在，会替换该子树，并把大小/文件数变化同步到已索引的父路径。

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

The default scanner concurrency is **8 worker threads**.

默认扫描并发是 **8 个 worker 线程**。

More threads are not always faster because directory traversal, Python scheduling, and SQLite writes share one pipeline. Increase `--workers` only after measuring your own tree.

线程越多不一定越快，因为目录遍历、Python 调度和 SQLite 写入共享同一条流水线。建议根据自己的目录结构实测后再提高 `--workers`。

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
