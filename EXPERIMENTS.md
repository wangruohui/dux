# S3 删除并发消融

## 2026-09-07：AOSS 批量删除

目标：在不清空测试 prefix 的前提下，确定 AOSS 批量删除的合适并发，并验证并行 listing 与取消响应。

锁定对象：

- `s3://wangruohui/laion400m-webdataset-512px-le/`
- `s3://wangruohui/laion400m-test-data/`

方法：先完成无破坏单测和只读能力检查；真实测试只使用预先截断的固定 key 列表，总删除量不超过约 3 万个对象。比较多个 workers 和 batch size，记录 listing 耗时、删除吞吐、错误数与数据库同步结果。禁止对整个 prefix 发起递归删除。

预期：批量 API 显著减少请求数；并发提高后吞吐先上升后趋于饱和。选择达到平台期的最小并发作为默认值，避免只增加连接压力。

实际结果：共删除 29,800 个预先截断且互不重叠的对象，全部成功，无整目录删除。`laion400m-webdataset-512px-le/` 实时 listing 已为空，因此未从该 prefix 删除对象；样本均来自仍有对象的 `laion400m-test-data/`。首次并行取得 20,800 个 key 耗时 10.27 秒，补充取得 9,000 个 key 耗时 11.71 秒。

固定 batch size 50、每档 4,000 个对象时：16/32/64/128/256 workers 分别达到约 1025/285/1286/502/901 objects/s。补充测试受到明显服务端限流和长尾影响，但再次确认提高到 128/256 没有稳定收益。64 workers 是出现最高吞吐的最小高并发档。

batch size 结果存在请求顺序和限流扰动；补充测试中 500-key batch 达到约 416 objects/s，1000-key batch 约 183 objects/s，小 batch 会产生更多请求并更容易触发长尾。最终采用 S3 默认 64 workers、500 keys/batch；本地文件系统默认 256 workers 不变，用户仍可用 `--workers` 显式覆盖。
