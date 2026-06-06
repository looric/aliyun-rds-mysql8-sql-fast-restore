# Security Policy

请不要在公开 Issue、PR、截图或日志中包含以下信息：

- 真实 RDS 备份下载 URL
- 数据库密码、临时 token、AK/SK
- 公网 IP、内网 IP、业务域名
- 包含业务数据的 SQL 片段或恢复日志
- 解压后的备份文件

本项目不支持 `MYSQL_PWD`。推荐使用 `--ask-password`、`--password-file`、`--defaults-extra-file` 或 `--login-path`。
