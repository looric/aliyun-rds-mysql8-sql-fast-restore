# Contributing

感谢你对本项目的关注。提交 Issue 或 PR 时，请尽量提供：

- 工具版本：`python3 restore_sql_fast.py --version`
- 操作系统版本
- Python 版本
- MySQL Server / Client 版本
- 备份目录布局：flat 或 nested
- 执行命令，注意脱敏密码和真实 RDS 下载 URL
- 完整错误日志中最早出现的 MySQL 报错

代码要求：保持 Python 3.6+ 兼容、零第三方运行时依赖，并补充或更新标准库 `unittest` 测试。
