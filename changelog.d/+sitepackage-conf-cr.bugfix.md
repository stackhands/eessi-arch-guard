`conf_value` kept a carriage return inside one line, so a later
`hide_toolchains` key in `eessi-arch-guard.conf` did not win. A line
now ends on `\n`, `\r\n`, or a lone `\r`, as `parseConfigBytes` does.
