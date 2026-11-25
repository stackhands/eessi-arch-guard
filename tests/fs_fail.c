/* Trace the root process and fail one file system call.

   Usage:
     fs_fail ERRNO OP NTH NEEDLE -- argv...

   OP is open, write, close or fsync. NTH is the 1-based call of that OP
   whose path contains NEEDLE. A write, close or fsync is named by the
   path its descriptor was opened with. An open, write or fsync is not
   made: it returns -ERRNO instead. A close is made, because Linux frees
   the descriptor whatever close returns, and then returns -ERRNO, as
   NFS reports a lost write. ERRNO is injected once. Only the root
   process is traced, so the programs it starts run as they are. The
   system call numbers are those of x86_64.
*/
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/uio.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <unistd.h>

#if !defined(__x86_64__)
#error "fs_fail traces x86_64 system calls only"
#endif

enum { OP_NONE = 0, OP_OPEN, OP_WRITE, OP_CLOSE, OP_FSYNC };
enum { FDMAX = 256, PATHMAX = 768 };

static int fail_errno, op, nth, seen, fired;
static const char *needle;
static int fds[FDMAX];
static char paths[FDMAX][PATHMAX];
static int nfds;

static int read_str(pid_t pid, unsigned long addr, char *buf, size_t n) {
    struct iovec local = {buf, n};
    struct iovec remote = {(void *)addr, n};
    memset(buf, 0, n);
    if (process_vm_readv(pid, &local, 1, &remote, 1, 0) <= 0) return -1;
    buf[n - 1] = 0;
    return 0;
}

static int fd_slot(int fd) {
    for (int i = 0; i < nfds; i++) if (fds[i] == fd) return i;
    return -1;
}

static void remember(int fd, const char *path) {
    if (fd < 0) return;
    int s = fd_slot(fd);
    if (s < 0) {
        if (nfds >= FDMAX) return;
        s = nfds++;
        fds[s] = fd;
    }
    snprintf(paths[s], sizeof paths[s], "%s", path);
}

static void forget(int fd) {
    int s = fd_slot(fd);
    if (s < 0) return;
    fds[s] = fds[nfds - 1];
    memcpy(paths[s], paths[nfds - 1], sizeof paths[s]);
    nfds--;
}

static const char *path_of(int fd) {
    int s = fd_slot(fd);
    return s < 0 ? NULL : paths[s];
}

static int class_of(long nr) {
    if (nr == 2 || nr == 257 || nr == 437) return OP_OPEN;
    if (nr == 1 || nr == 18 || nr == 20 || nr == 296 || nr == 328) return OP_WRITE;
    if (nr == 3) return OP_CLOSE;
    if (nr == 74 || nr == 75) return OP_FSYNC;
    return OP_NONE;
}

/* The path an open names: relative to its directory descriptor, or to the
   working directory of the process for AT_FDCWD. */
static void open_path(pid_t pid, long nr, struct user_regs_struct *regs, char *out) {
    char raw[PATHMAX];
    int dirfd = -100;
    unsigned long p = nr == 2 ? regs->rdi : regs->rsi;
    if (nr != 2) dirfd = (int)regs->rdi;
    out[0] = 0;
    if (read_str(pid, p, raw, sizeof raw) < 0 || !raw[0]) return;
    if (raw[0] == '/') {
        snprintf(out, PATHMAX, "%s", raw);
        return;
    }
    char base[PATHMAX];
    base[0] = 0;
    if (dirfd == -100) {
        char link[64];
        snprintf(link, sizeof link, "/proc/%d/cwd", (int)pid);
        ssize_t n = readlink(link, base, sizeof base - 1);
        base[n > 0 ? n : 0] = 0;
    } else if (path_of(dirfd)) {
        snprintf(base, sizeof base, "%s", path_of(dirfd));
    }
    if (base[0]) snprintf(out, PATHMAX, "%s/%s", base, raw);
    else snprintf(out, PATHMAX, "%s", raw);
}

int main(int argc, char **argv) {
    if (argc < 7 || strcmp(argv[5], "--") != 0) return 2;
    fail_errno = atoi(argv[1]);
    op = !strcmp(argv[2], "open") ? OP_OPEN :
         !strcmp(argv[2], "write") ? OP_WRITE :
         !strcmp(argv[2], "close") ? OP_CLOSE :
         !strcmp(argv[2], "fsync") ? OP_FSYNC : OP_NONE;
    nth = atoi(argv[3]);
    needle = argv[4];
    if (fail_errno <= 0 || op == OP_NONE || nth < 1 || !needle[0]) return 2;

    pid_t pid = fork();
    if (pid < 0) return 1;
    if (pid == 0) {
        if (ptrace(PTRACE_TRACEME, 0, 0, 0) < 0) _exit(126);
        raise(SIGSTOP);
        execvp(argv[6], argv + 6);
        _exit(127);
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) return 1;
    if (ptrace(PTRACE_SETOPTIONS, pid, 0, PTRACE_O_TRACESYSGOOD | PTRACE_O_EXITKILL) < 0) return 1;
    if (ptrace(PTRACE_SYSCALL, pid, 0, 0) < 0) return 1;

    int entering = 1;
    int poisoned = 0;
    int failing = 0;
    char opened[PATHMAX];
    opened[0] = 0;
    for (;;) {
        if (waitpid(pid, &status, 0) < 0) return errno == ECHILD ? 0 : 1;
        if (WIFEXITED(status)) return WEXITSTATUS(status);
        if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
        if (!WIFSTOPPED(status)) continue;
        int sig = WSTOPSIG(status);
        if (sig != (SIGTRAP | 0x80)) {
            if (ptrace(PTRACE_SYSCALL, pid, 0, sig == SIGTRAP ? 0 : sig) < 0) return 1;
            continue;
        }
        struct user_regs_struct regs;
        if (ptrace(PTRACE_GETREGS, pid, 0, &regs) < 0) return 1;
        long nr = (long)regs.orig_rax;
        if (entering) {
            poisoned = failing = 0;
            int cls = class_of(nr);
            const char *named = NULL;
            if (cls == OP_OPEN) {
                open_path(pid, nr, &regs, opened);
                named = opened;
            } else if (cls != OP_NONE) {
                named = path_of((int)regs.rdi);
            }
            if (!fired && cls == op && named && strstr(named, needle) && ++seen == nth) {
                fired = 1;
                if (cls == OP_CLOSE) {
                    failing = 1;
                } else {
                    /* getpid in its place, so the call is not made */
                    regs.orig_rax = 39;
                    if (ptrace(PTRACE_SETREGS, pid, 0, &regs) < 0) return 1;
                    poisoned = 1;
                }
            }
            if (cls == OP_CLOSE) forget((int)regs.rdi);
            entering = 0;
        } else {
            if (poisoned || failing) {
                regs.rax = (unsigned long)(-fail_errno);
                if (ptrace(PTRACE_SETREGS, pid, 0, &regs) < 0) return 1;
            } else if (class_of(nr) == OP_OPEN && (long)regs.rax >= 0 && opened[0]) {
                remember((int)regs.rax, opened);
            } else if ((nr == 32 || nr == 33 || nr == 292) && (long)regs.rax >= 0) {
                const char *p = path_of((int)regs.rdi);
                if (p) {
                    char copy[PATHMAX];
                    snprintf(copy, sizeof copy, "%s", p);
                    remember((int)regs.rax, copy);
                }
            }
            entering = 1;
        }
        if (ptrace(PTRACE_SYSCALL, pid, 0, 0) < 0) return 1;
    }
}
