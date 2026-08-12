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
