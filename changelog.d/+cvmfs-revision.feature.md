`share/cvmfsrev.py` reads a `cvmfs_config stat` file and prints the client revision. With no `--min` it exits 0. `--min N` exits 1 when the client revision is older than N. It does not remount.
