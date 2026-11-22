The Zig restore helper writes each restore script with one `write(2)`, as Python does, so a short write leaves a short script that bash runs instead of failing the restore.
