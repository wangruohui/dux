# S3 删除并发消融

## 2026-09-07：AOSS 批量删除

目标：在不清空测试 prefix 的前提下，确定 AOSS 批量删除的合适并发，并验证并行 listing 与取消响应。

锁定对象：

- `s3://wangruohui/laion400m-webdataset-512px-le/`
- `s3://wangruohui/laion400m-test-data/`

方法：先完成无破坏单测和只读能力检查；真实测试只使用预先截断的固定 key 列表，总删除量不超过约 3 万个对象。比较多个 workers 和 batch size，记录 listing 耗时、删除吞吐、错误数与数据库同步结果。禁止对整个 prefix 发起递归删除。

预期：批量 API 显著减少请求数；并发提高后吞吐先上升后趋于饱和。选择达到平台期的最小并发作为默认值，避免只增加连接压力。

实际结果：待运行。
