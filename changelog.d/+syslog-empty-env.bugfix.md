An empty `EESSI_ARCH_GUARD_SYSLOG` hid the site config `syslog`
key. The guard now treats that empty value as unset. The configured
target stays in use.
