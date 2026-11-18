Replace invalid UTF-8 bytes with `?` when encoding RFC 5424 syslog and journald payloads in Zig `syslog`, matching Python `encode("utf-8", "replace")`.
