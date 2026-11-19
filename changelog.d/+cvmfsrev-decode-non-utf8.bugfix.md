`share/cvmfsrev.py` decodes `--stat` input with UTF-8 replacement characters so non-UTF-8 bytes in surrounding lines are replaced instead of aborting with `UnicodeDecodeError`.
