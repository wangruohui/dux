# 扫描并发优化

目标：测出不写数据库时的扫描并发上限，并消除当前逐文件写入队列造成的同步瓶颈。

- DONE：3.36 万文件子树纯扫描峰值约 34k files/s。
- DONE：完整 `/mnt/afs/video_ckpt/neo` 在 256 线程达到 3,838 entries/s，512 线程回落到 2,649 entries/s。
- DONE：SQLite writer-only 达到 155,691 rows/s，确认 SQLite 引擎不是主要瓶颈。
- DONE：同一子树当前 dux 256 线程仅 5,624 files/s，定位为逐记录队列和计数锁开销。
- DONE：改为按目录批量传递记录和批量更新计数。
- DONE：同窗口集成吞吐由 16.3k 提升到 20.2k files/s，提升约 24%。
- DONE：完整单元测试通过，批量扫描优化已独立提交。
- DONE：128/256/512 完整树耗时分别为 431.1s/335.6s/486.2s，已测最优为 256。
- DONE：默认 worker 已调整为 256，12 项单元测试和 CLI help 验证通过。
- DONE：不更换数据库；真实路径 SQLite 写入 59.6k rows/s，远高于文件系统扫描吞吐。

## UI 递归筛选批量删除

目标：在当前 UI 目录下按名称递归筛选路径，支持 exclude 剪枝、结果多选和并行删除。

- DONE：名称精确匹配、命中目录剪枝和 exclude 路径剪枝已实现，14 项测试通过。
- DONE：已增加 `f` 查询弹窗和筛选结果选择表，支持 Space、全选和取消。
- DONE：筛选结果可二次确认后永久删除，复用并行删除、数据库更新和进度条。
- DONE：删除状态已增加 ETA；headless TUI 完整删除测试通过，exclude 保留且父级文件数从 3 更新为 1。
- DONE：取消删除确认时返回筛选结果页并保留选择。
- DONE：常规删除和多个后台删除 job 共享全局 256 删除并发，独立连接更新数据库。
- DONE：删除期间允许启动 `f` 筛选，不被后台删除 worker 阻塞。
- DONE：支持取消删除 job；成功删除的路径由 SQLite writer 按 5000 条或 0.2 秒批量删索引并同步父级，不重新扫描。
- DONE：`f` 支持仅匹配 basename 的 shell 通配符，例如 `a*`，不处理 `/`。
- DONE：多个删除 job 时顶部固定展示最新可取消 job，并显示 active/cancelling 数量；每次按 `x` 只取消最近一次，再按可逐个取消其余 job。

## UI 与扫描并发

- DONE：UI 启动改为纯读取现有导航索引，不在初始化阶段刷新父级占位；持有 SQLite 写锁的单测和真实共享数据库冒烟均通过。

## 浏览页选择范围

- DONE：常规浏览页选择限定在当前目录页面，进入子目录或返回父目录时清空；删除目标再次与当前可见行取交集，TUI 冒烟通过。

## SQLite 写锁诊断

- DONE：统一登记主库 writer 的 PID、命令、操作、目标和持锁时间；删除等待时在状态栏显示持锁者，外部或旧版 writer 列出候选进程。
- DONE：已有数据库连接不再重复执行建表、建索引和 WAL 初始化，只在确实缺少字段时进入带诊断的迁移写事务。
- DONE：精确 owner、legacy candidate、TUI 等待状态和释放后索引一致性测试通过；真实默认库候选识别为 PID 345634 的 NEO_Unify index。

## Filter 混合扫描

- DONE：filter 合并只读 SQLite 候选与实时文件系统递归结果；只返回现场存在的并集，并显示 indexed-live、live-only 和 stale-db 数量；mismatch 单测与完整 TUI 流程通过。
