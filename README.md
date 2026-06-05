# 阿里云RDS MySQL 8.x 快照备份快速恢复到自建数据库

<p align="center">
  <b>将阿里云 RDS MySQL 云盘实例“下载备份”得到的 SQL 文件目录快速恢复到自建 MySQL 8.x</b><br>
  SQL-only · Python 3.6.8+ · Linux only · Zero dependencies · 表级并发导入
</p>
[![CI](https://github.com/looric/aliyun-rds-mysql8-sql-fast-restore/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/looric/aliyun-rds-mysql8-sql-fast-restore/actions/workflows/ci.yml)

<p align="center">
  <img alt="Version" src="https://img.shields.io/badge/release-v260605-blue">
  <img alt="Python 3.6+" src="https://img.shields.io/badge/python-3.6%2B-blue">
  <img alt="Linux only" src="https://img.shields.io/badge/platform-Linux-lightgrey">
  <img alt="MySQL 8.x" src="https://img.shields.io/badge/mysql-8.x-orange">
  <img alt="SQL only" src="https://img.shields.io/badge/mode-SQL--only-success">
  <img alt="Zero dependencies" src="https://img.shields.io/badge/dependencies-stdlib%20only-lightgrey">
  <img alt="License MIT" src="https://img.shields.io/badge/license-MIT-green">
</p>

## 项目定位

本项目用于将 **阿里云 RDS MySQL 8.x 云盘实例快照备份** 通过“下载备份”功能转换得到的 **SQL 文件目录**，快速恢复到 ECS、本地服务器或其他自建 MySQL 8.x 实例。

它适合这样的场景：你已经下载并解压出按“数据库 / 表 / SQL 分片”组织的目录，希望用一个零依赖、可审计、支持并发和日志的脚本完成恢复。

本项目不是物理备份恢复工具，也不是 DTS、复制迁移、mysqldump 或完整备份平台的替代品。

## 版本

当前初始化发布版本：`v260605`

English quick-start: [README_EN.md](README_EN.md)

查看脚本版本：

```bash
python3 restore_sql_fast.py --version
```

## 运行环境

| 组件 | 要求 / 说明 |
|---|---|
| 操作系统 | Linux only；已在 Alibaba Cloud Linux release 3 (OpenAnolis Edition) 测试通过 |
| Python | Python 3.6.8 或更高；仅使用标准库 |
| MySQL Server | MySQL 8.x；目标环境包括 MySQL 8.0.45 |
| MySQL Client | 建议使用 MySQL 8.0 client，并确保 `mysql` 命令可执行 |
| 解压工具 | `zstd`、`tar` |

Windows / macOS 不作为生产运行目标。

## 功能特性

| 能力 | 说明 |
|---|---|
| SQL-only | 只处理 `.sql` 备份目录，不处理 CSV 文件。 |
| Zero dependencies | 运行时不依赖第三方 Python 包，无需 `pip install`。 |
| 单库 / 多库自动识别 | 支持 flat 单库目录和 nested 多库目录。 |
| 表级并发 | `--workers N` 并发导入多张表；同一张表内分片按顺序导入。 |
| Fail-fast | 任意表导入失败后停止调度新表，并尝试终止仍在运行的 `mysql` 子进程。 |
| source 快速模式 | 默认生成临时 manifest，通过 mysql `source` 批量导入多个 SQL 文件。 |
| stream 兼容模式 | 路径含空格、中文或特殊字符时使用 `--input-mode stream`。 |
| 已有库兼容 | 默认把 `CREATE DATABASE` / `CREATE SCHEMA` 临时改写为 `IF NOT EXISTS`。 |
| 生成列兼容 | 自动识别 generated column，导入数据时把显式值改写为 `DEFAULT`。 |
| 会话级导入优化 | 每个导入会话设置 `foreign_key_checks=0`、`unique_checks=0`、`autocommit=0`。 |
| 安全认证 | 默认使用临时 `--defaults-extra-file`，不支持 `MYSQL_PWD`。 |
| 日志留痕 | 支持 `--log-file`、`--verbose`、`--quiet`。 |
| dry-run | `--dry-run` 只检查目录、执行计划和参数，不连接 MySQL。 |

## 备份下载与解压

### 1. 在 RDS 控制台导出 SQL 文件

在阿里云 RDS 控制台使用 **下载备份** 功能，将 RDS MySQL 云盘实例的快照备份数据转换成 **SQL 文件**。

本项目只处理 SQL 备份目录。如果你选择 CSV 导出，请使用其他工具或自行编写 `LOAD DATA` 流程。

### 2. 下载备份包

```bash
wget -b -c -O backup.tar.zst "<RDS备份下载URL>"
```

参数说明：

- `-b`：后台下载。
- `-c`：断点续传。
- `-O backup.tar.zst`：保存为指定文件名。

不要把真实备份下载 URL 提交到 GitHub；该 URL 可能包含临时授权信息或业务敏感路径。

### 3. 解压 `.tar.zst`

通用格式：

```bash
zstd -d -c <压缩包文件名称>.tar.zst | tar -xvf - -C <解压缩后的文件位置>
```

示例：

```bash
mkdir -p /home/mysql/data
zstd -d -c backup.tar.zst | tar -xvf - -C /home/mysql/data
```

查看解压后的文件：

```bash
find /home/mysql/data -maxdepth 3 -type f | head
```

## 恢复前检查

### 1. 确认自建 MySQL 已开启 `local_infile`

注意：请确保自建 MySQL 数据库已开启 `local_infile` 参数。

查看参数状态，返回值为 `ON` 表示已开启：

```sql
SHOW GLOBAL VARIABLES LIKE 'local_infile';
```

临时开启：

```sql
SET GLOBAL local_infile=1;
```

如果需要重启后仍然生效，请在 MySQL 配置文件中加入：

```ini
[mysqld]
local_infile=1
```

然后重启 MySQL 服务。

说明：本项目只导入 SQL 文件，不导入 CSV 文件。多数 SQL 分片是 `INSERT` 语句，不会实际使用 `local_infile`；但阿里云官方恢复流程将它作为前置检查。若 SQL 文件内部包含 `LOAD DATA LOCAL INFILE`，服务端和客户端都需要允许 `LOCAL` 能力。客户端侧可通过以下方式开启：

```bash
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root \
  --ask-password \
  --mysql-extra-arg=--local-infile=1
```

### 2. 确认目标实例可被覆盖或重复导入

最推荐使用空实例或空库恢复。若目标库已经存在，默认行为是复用数据库并继续导入：

```bash
--existing-database reuse
```

如果你确认目标库可以删除后重建，可以使用：

```bash
--existing-database drop
```

该参数会执行 `DROP DATABASE IF EXISTS`，请勿对生产库误用。

### 3. 先执行 dry-run

```bash
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root \
  --ask-password \
  --dry-run
```

重点检查日志中的：

```text
layout=flat 或 layout=nested
root/database=...
plan: root/databases=..., tables=..., sql_data_files=..., sql_data_size=...
```

## 目录结构

### 单库 flat 布局

传入目录本身就是一个数据库备份目录：

```text
/home/mysql/data/service2023db/
|-- structure.sql
|-- t_week_channel/
|   |-- structure.sql
|   `-- data/
|       `-- t_week_channel_0_part0.sql
`-- t_work_day/
    |-- structure.sql
    `-- data/
        `-- t_work_day_0_part0.sql
```

运行时传入这个数据库目录：

```bash
python3 restore_sql_fast.py /home/mysql/data/service2023db 127.0.0.1 3306 root --ask-password
```

如果根目录 `structure.sql` 能解析到 `CREATE DATABASE` 或 `USE`，通常不需要 `-D`。无法解析或想强制指定目标库名时再加：

```bash
python3 restore_sql_fast.py /home/mysql/data/service2023db 127.0.0.1 3306 root \
  --ask-password \
  -D service2023db
```

### 多库 nested 布局

传入目录下有多个数据库子目录：

```text
/home/mysql/data/
|-- service2023db/
|   |-- structure.sql
|   `-- t_week_channel/
|       |-- structure.sql
|       `-- data/
|           `-- t_week_channel_0_part0.sql
|-- orderdb/
|   |-- structure.sql
|   `-- ...
`-- userdb/
    |-- structure.sql
    `-- ...
```

运行时传入多库总目录，**不要加 `-D`**：

```bash
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root --ask-password --workers 4
```

只恢复一个库：

```bash
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root \
  --ask-password \
  --only-database service2023db \
  --workers 4
```

恢复多个库：

```bash
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root \
  --ask-password \
  --only-database service2023db,orderdb \
  --workers 4
```

## 安装

### 方式一：从 GitHub Release 下载单文件

```bash
wget https://github.com/looric/aliyun-rds-mysql8-sql-fast-restore/releases/latest/download/restore_sql_fast.py
chmod +x restore_sql_fast.py
```

### 方式二：克隆仓库

```bash
git clone https://github.com/looric/aliyun-rds-mysql8-sql-fast-restore.git
cd aliyun-rds-mysql8-sql-fast-restore
chmod +x restore_sql_fast.py
```

确认依赖：

```bash
python3 --version
mysql --version
zstd --version
```

## 快速开始

### 1. 小并发试跑

```bash
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root \
  --ask-password \
  --workers 2 \
  --log-file restore.log
```

### 2. 根据机器能力提高并发

```bash
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root \
  --ask-password \
  --workers 4 \
  --log-file restore.log
```

建议从 `--workers 2` 或 `--workers 4` 开始。观察磁盘 util、redo 写入、CPU、MySQL checkpoint 压力后再调整。

## 认证方式

### 推荐：交互式输入密码

```bash
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root --ask-password
```

脚本会生成临时 MySQL client option file，并以 `--defaults-extra-file` 传给 `mysql` 客户端。临时文件权限为 `0600`，任务结束后自动删除。

### 从文件读取密码

```bash
printf '%s\n' 'your_password' > /root/.mysql_restore_password
chmod 600 /root/.mysql_restore_password

python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root \
  --password-file /root/.mysql_restore_password
```

### 使用手工维护的 MySQL option file

```ini
# /root/.restore-my.cnf
[client]
password="your_password"
```

```bash
chmod 600 /root/.restore-my.cnf
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root \
  --defaults-extra-file /root/.restore-my.cnf
```

### 使用 login-path

```bash
mysql_config_editor set --login-path=restore --host=127.0.0.1 --port=3306 --user=root --password
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root --login-path=restore
```

### 不支持 `MYSQL_PWD`

本项目不支持 `MYSQL_PWD`，也不支持从环境变量读取密码。即使父进程环境里存在 `MYSQL_PWD`，脚本也会在启动 `mysql` 子进程前移除它。

请使用 `--ask-password`、`--password-file`、`--defaults-extra-file` 或 `--login-path`。

## 常用参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--workers N` | `1` | 表级并发数量。一个 worker 负责一张表的数据文件顺序导入。 |
| `--input-mode source` | `source` | 使用 manifest + `source` 批量导入，通常更快。 |
| `--input-mode stream` | - | 通过 stdin 流式写入 SQL，适合路径含空格、中文或特殊字符。 |
| `--generated-column-mode default` | `default` | 遇到生成列时，把数据 SQL 中对生成列的显式值改写为 `DEFAULT`。 |
| `--generated-column-mode off` | - | 不做生成列数据改写，保留原始 SQL。 |
| `--skip-structure` | 关闭 | 跳过结构，只导数据。 |
| `--skip-data` | 关闭 | 只导结构，跳过数据。 |
| `--only-database DB` | - | 多库 nested 模式下只恢复指定库。可重复或逗号分隔。 |
| `-D DB` | - | 单库 flat 模式下手动指定目标库。多库模式不要用。 |
| `--existing-database reuse` | `reuse` | 已有库时把 `CREATE DATABASE` 临时改为 `IF NOT EXISTS`。 |
| `--existing-database drop` | - | 导入结构前 `DROP DATABASE IF EXISTS`，危险但最干净。 |
| `--fail-fast` | 开启 | 任一表失败后停止调度新表。 |
| `--no-fail-fast` | 关闭 | 继续导剩余表，最后统一报告失败。 |
| `--detect-limit 8M` | `8M` | 扫描结构文件以检测库名和建库语句；`0` 表示完整扫描。 |
| `--disable-binlog` | 关闭 | 当前导入会话执行 `SET SESSION sql_log_bin=0`。有复制、GTID、PITR 时慎用。 |
| `--max-allowed-packet 1G` | `1G` | MySQL client 侧 packet 上限；服务端也要配置。 |
| `--mysql-extra-arg ARG` | - | 追加一个 MySQL 客户端参数，可重复。 |
| `--mysql-extra-args "..."` | - | 传递多个 MySQL 客户端参数，例如 SSL 或 local infile。 |
| `--log-file restore.log` | - | 同时写 DEBUG 级别日志到文件。 |
| `--dry-run` | 关闭 | 只检查计划，不执行导入。 |

## 路径与导入模式

默认 `source` 模式会生成临时 manifest，例如：

```sql
source /home/mysql/data/service2023db/t_work_day/data/t_work_day_0_part0.sql
```

为了避免 `source` 命令被空格或特殊字符干扰，source 模式只允许路径包含：

```text
字母、数字、下划线、点、斜杠、中划线
```

如果备份目录路径包含空格、中文、`$`、`;`、`|`、`&` 等字符，请使用：

```bash
python3 restore_sql_fast.py "/home/mysql/data with space" 127.0.0.1 3306 root \
  --ask-password \
  --input-mode stream
```

## 生成列 generated column 兼容

部分 SQL 导出文件会在 `INSERT` 列表中包含 MySQL 生成列，例如：

```sql
INSERT IGNORE INTO `service2024report`.`metabase_field`
  (`id`, `name`, `unique_field_helper`)
VALUES
  (1, 'SEATS', 0);
```

如果 `unique_field_helper` 在表结构中是 generated column，MySQL 8.x 会拒绝显式写入普通值，并报：

```text
ERROR 3105 (HY000): The value specified for generated column ... is not allowed.
```

本项目默认启用：

```bash
--generated-column-mode default
```

脚本会从每张表的 `structure.sql` 中识别 generated column，并在数据导入阶段把对应值改写为 `DEFAULT`。该改写只发生在数据流进入 `mysql` 客户端前，不修改原始备份 SQL 文件。

如果某张表存在 generated column，脚本会自动将这张表切换到 stream 导入；其他普通表仍继续使用默认 source 模式。

## MySQL 服务端参数优化

下面参数适合“可重跑恢复任务”的自建 MySQL 恢复实例。不要在承载线上业务的生产实例上直接照搬。

### 建议优先检查

```ini
[mysqld]
local_infile=1
max_allowed_packet=1G
innodb_buffer_pool_size=70%-80%_RAM
innodb_redo_log_capacity=8G
innodb_log_buffer_size=256M
innodb_io_capacity=1000
innodb_io_capacity_max=2500
```

说明：

- `local_infile`：阿里云官方恢复流程要求开启；若 SQL 文件包含 `LOAD DATA LOCAL INFILE`，客户端也要开启 `--local-infile=1`。
- `max_allowed_packet`：客户端和服务端都需要足够大。本脚本默认给 mysql client 传 `--max_allowed_packet=1G`，服务端仍需要你配置。
- `innodb_buffer_pool_size`：专用恢复实例通常可给到物理内存较大比例，注意给 OS、连接线程、临时空间预留内存。
- `innodb_redo_log_capacity`：MySQL 8.0.30+ 使用该参数控制 redo 总容量。大批量导入时过小会导致 checkpoint 频繁。
- `innodb_log_buffer_size`：大事务、大 INSERT 分片较多时可适当增大。
- `innodb_io_capacity` / `innodb_io_capacity_max`：按磁盘能力调整。普通云盘不要设置过高；本地 NVMe 可更高，需要压测。

### 导入期间可临时调整的动态参数

适合临时恢复库、失败后可以删库重导的场景：

```sql
SET GLOBAL innodb_flush_log_at_trx_commit = 2;
SET GLOBAL sync_binlog = 0;
```

导入完成后恢复：

```sql
SET GLOBAL innodb_flush_log_at_trx_commit = 1;
SET GLOBAL sync_binlog = 1;
```

风险：如果在导入期间发生电源故障、OS 崩溃或 mysqld 异常，可能丢失最近事务或造成恢复结果不完整。恢复库建议保留原始备份，必要时删库重导。

### 关于 binlog

如果恢复实例不需要复制、GTID、审计或基于 binlog 的时间点恢复，可以考虑启动实例时禁用 binlog，或运行脚本时加：

```bash
--disable-binlog
```

`--disable-binlog` 会在每个导入会话执行：

```sql
SET SESSION sql_log_bin=0;
```

这需要相应权限，并且会影响 GTID / 复制链路。恢复到主从架构或需要 PITR 时不要随意使用。

## 并发建议

| 环境 | 建议起步 |
|---|---:|
| 机械盘 / 远程盘 / 低 IOPS 云盘 | `--workers 1` 或 `2` |
| 普通 SSD / ESSD 云盘 | `--workers 2` 或 `4` |
| 本地 NVMe / 多核 / 多表很多 | `--workers 4` 到 `8` |

本项目的并发粒度是 **表级并发**。同一张表的 SQL 分片不会并发写入，目的是降低同表二级索引维护、自增锁、页竞争和死锁风险。

## 故障排查

### `Can't create database ... database exists`

默认 `--existing-database reuse` 会将根 `structure.sql` 中的：

```sql
CREATE DATABASE `db`;
```

临时改写为：

```sql
CREATE DATABASE IF NOT EXISTS `db`;
```

如果你使用了 `--existing-database error`，会保留原始 SQL，目标库存在时就会报错。

### `Table ... already exists`

说明表已经存在。推荐使用空实例或删库重导：

```bash
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root \
  --ask-password \
  --existing-database drop
```

### `file path contains characters unsafe for mysql source mode`

source 模式路径包含空格、中文或特殊字符。改用：

```bash
--input-mode stream
```

或者把备份目录移动到简单路径，例如 `/home/mysql/data`。

### `The value specified for generated column ... is not allowed`

这是 MySQL 生成列限制，不是连接或权限问题。默认 `--generated-column-mode default` 会处理该问题。请确认没有加：

```bash
--generated-column-mode off
```

### `ERROR 3948` / `ERROR 3950` / `Loading local data is disabled`

说明 SQL 文件中包含 `LOAD DATA LOCAL INFILE`，但服务端或客户端没有开启 `LOCAL` 能力。

服务端检查：

```sql
SHOW GLOBAL VARIABLES LIKE 'local_infile';
```

临时开启：

```sql
SET GLOBAL local_infile=1;
```

客户端开启：

```bash
python3 restore_sql_fast.py /home/mysql/data 127.0.0.1 3306 root \
  --ask-password \
  --mysql-extra-arg=--local-infile=1
```

### `Packet too large` / `Lost connection to MySQL server during query`

通常是单条 INSERT 或单个分片中某条语句过大。确认服务端：

```sql
SHOW VARIABLES LIKE 'max_allowed_packet';
```

必要时调大服务端参数，并确认脚本客户端参数：

```bash
--max-allowed-packet 1G
```

### 密码输入含不可见字符

如果交互式粘贴密码时混入末尾 `NUL` 字符，脚本会自动移除并输出 warning。若 `NUL` 位于密码中间，或者密码包含换行 / 回车，脚本会拒绝执行，避免生成错误的 MySQL option file。

遇到复杂密码或不确定终端输入是否可靠时，建议使用 `--password-file`、`--defaults-extra-file` 或 `--login-path`。

## 开发与测试

语法检查：

```bash
python3 -m py_compile restore_sql_fast.py
```

单元测试：

```bash
python3 -m unittest discover -s tests -v
```

项目包含 GitHub Actions CI 示例：

```text
.github/workflows/ci.yml
```

CI 默认在 Python 3.8 / 3.10 / 3.12 上运行语法检查和单元测试，并保留 Python 3.6 grammar 兼容检查。Python 3.6.8 是运行兼容底线；如果你有自托管 runner，也可以额外加入 Python 3.6.8 实机测试。

## 非目标

本项目不做：

- CSV 导入。
- 物理备份恢复。
- 自动断点续导。
- 自动比对行数、checksum、业务一致性。
- 在线无锁迁移或双写同步。
- Windows / macOS 生产支持。

## 参考文档

- 阿里云：RDS MySQL 快照备份文件恢复到自建数据库  
  https://help.aliyun.com/zh/rds/apsaradb-rds-for-mysql/restore-the-data-of-an-apsaradb-rds-for-mysql-instance-to-a-self-managed-mysql-instance-by-using-a-csv-file-or-an-sql-file
- MySQL 8.0：Environment Variables / `MYSQL_PWD`  
  https://dev.mysql.com/doc/refman/8.0/en/environment-variables.html
- MySQL 8.0：Security Considerations for LOAD DATA LOCAL / `local_infile`  
  https://dev.mysql.com/doc/refman/8.0/en/load-data-local-security.html
- MySQL 8.0：Bulk Data Loading for InnoDB Tables  
  https://dev.mysql.com/doc/refman/8.0/en/optimizing-innodb-bulk-data-loading.html
- MySQL 8.0：Packet Too Large / `max_allowed_packet`  
  https://dev.mysql.com/doc/refman/8.0/en/packet-too-large.html
- MySQL 8.0：Redo Log / `innodb_redo_log_capacity`  
  https://dev.mysql.com/doc/refman/8.0/en/innodb-redo-log.html
- MySQL 8.0：Binary Logging Options / `sync_binlog` / `sql_log_bin`  
  https://dev.mysql.com/doc/refman/8.0/en/replication-options-binary-log.html

## 许可协议

本项目使用 MIT License，详见 [LICENSE](LICENSE)。
