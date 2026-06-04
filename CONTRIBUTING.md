# Contributing

Thanks for improving this tool.

Before opening a pull request, please run:

```bash
python3 -m py_compile restore_sql_fast.py
python3 -m unittest discover -s tests -v
```

Guidelines:

- Keep runtime dependencies at zero; standard library only.
- Keep Python 3.6.8 compatibility.
- Do not commit real RDS backup URLs, passwords, IP addresses, logs containing
  business data, or extracted backup files.
- Update README and examples when command-line behavior changes.
- Prefer small, focused PRs.
