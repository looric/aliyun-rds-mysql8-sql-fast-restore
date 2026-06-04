# Security Policy

## Sensitive information

Do not publish real RDS backup download URLs, passwords, IP addresses, table data,
or complete restore logs in public issues or pull requests. RDS backup URLs may
contain temporary authorization information or business-sensitive paths.

## Supported authentication methods

This project intentionally does not support `MYSQL_PWD`. Use one of the following
methods instead:

- `--ask-password`
- `--password-file`
- `--defaults-extra-file`
- `--login-path`

The default `--ask-password` and `--password-file` paths create a temporary MySQL
client option file with `0600` permissions and remove it after use.

## Reporting a security issue

If the repository has private vulnerability reporting enabled, please use that
GitHub feature. Otherwise, contact the maintainers privately before opening a
public issue.
