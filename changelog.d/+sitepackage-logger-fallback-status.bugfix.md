Check `command_succeeded` on `exec_cmd` in `share/lmod/SitePackage.lua` `emit_syslog` so missing or failing `logger` commands return `false` and report `0` flushed records from `flush_usage_records`.
