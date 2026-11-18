Validate octal digits explicitly in `share/sitepath.py` `_unescape` so non-octal `\8xx` or `\9xx` sequences do not raise `ValueError`.
