`share/cvmfsrev.py` opens `--stat` paths with `O_NONBLOCK` and checks `stat.S_ISREG` so FIFO files, directories, and dangling symlinks exit `2` promptly.
