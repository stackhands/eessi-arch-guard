/* Run a program under ptrace and fail one close(2) it makes.

   Usage: close_fail ERRNO PATH -- argv...

   The first close of a descriptor that names PATH runs, so the descriptor
   is gone and its locks are released, as after a real failed close. Its
   result is then replaced by -ERRNO. The tracer assumes a single-threaded
   tracee: there is no PTRACE_O_TRACECLONE, so only the program's first
   thread is traced, not the threads or children it starts, and a close
   made by another thread is missed. The guard closes the lock from its
   only thread. A SIGTERM sent to close_fail is passed on to the program.
   The exit status is the program's, or 128 plus the signal that killed
   it. The Zig guard is static, so the tracer has to be its parent. Exit
   125 means the trace could not be set up.
*/
#define _GNU_SOURCE
#include <elf.h>
#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <unistd.h>

#if defined(__x86_64__)
#define SYSCALL_NR(r) ((r).orig_rax)
#define ARG0(r) ((r).rdi)
#define RESULT(r) ((r).rax)
#elif defined(__aarch64__)
#define SYSCALL_NR(r) ((r).regs[8])
#define ARG0(r) ((r).regs[0])
#define RESULT(r) ((r).regs[0])
#else
#error "close_fail supports x86_64 and aarch64"
#endif

static volatile sig_atomic_t child;

static void relay(int sig) {
    if (child > 0) kill(child, sig);
}

static int regs(pid_t pid, struct user_regs_struct *r, int set) {
    struct iovec io = {r, sizeof *r};
    return ptrace(set ? PTRACE_SETREGSET : PTRACE_GETREGSET, pid, (void *)NT_PRSTATUS, &io);
}

/* waitpid for the traced program, again when the relayed SIGTERM cuts it
   short, so that the program is stopped or gone when it returns. */
static int wait_child(pid_t pid, int *status) {
    for (;;) {
        pid_t got = waitpid(pid, status, 0);
        if (got == pid) return 0;
        if (got < 0 && errno == EINTR) continue;
        return -1;
    }
}

static int names(pid_t pid, unsigned long long fd, const char *path) {
    char link[64], target[PATH_MAX];
    if (fd > INT_MAX) return 0;
    snprintf(link, sizeof link, "/proc/%d/fd/%llu", (int)pid, fd);
    ssize_t n = readlink(link, target, sizeof target - 1);
    if (n < 0) return 0;
    target[n] = 0;
    return strcmp(target, path) == 0;
}

int main(int argc, char **argv) {
    if (argc < 5 || strcmp(argv[3], "--") != 0) {
        fprintf(stderr, "usage: close_fail ERRNO PATH -- argv...\n");
        return 125;
    }
    long fail = strtol(argv[1], NULL, 10);
    const char *path = argv[2];
    pid_t pid = fork();
    if (pid < 0) return 125;
    if (pid == 0) {
        if (ptrace(PTRACE_TRACEME, 0, NULL, NULL) != 0) {
            perror("close_fail: ptrace");
            _exit(125);
        }
        raise(SIGSTOP);
        execvp(argv[4], argv + 4);
        perror("close_fail: exec");
        _exit(125);
    }
    child = pid;
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = relay;
    sa.sa_flags = SA_RESTART;
    sigaction(SIGTERM, &sa, NULL);

    int status;
    if (wait_child(pid, &status) != 0 || !WIFSTOPPED(status)) return 125;
    if (ptrace(PTRACE_SETOPTIONS, pid, NULL,
               (void *)(PTRACE_O_TRACESYSGOOD | PTRACE_O_TRACEEXEC | PTRACE_O_EXITKILL)) != 0)
        return 125;
    int entering = 1, target = 0, fired = 0, sig = 0;
    for (;;) {
        if (ptrace(PTRACE_SYSCALL, pid, NULL, (void *)(long)sig) != 0) return 125;
        sig = 0;
        if (wait_child(pid, &status) != 0) return 125;
        if (WIFEXITED(status)) return WEXITSTATUS(status);
        if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
        if (!WIFSTOPPED(status)) continue;
        if (WSTOPSIG(status) == (SIGTRAP | 0x80)) {
            struct user_regs_struct r;
            if (regs(pid, &r, 0) != 0) return 125;
            if (entering) {
                target = !fired && SYSCALL_NR(r) == SYS_close && names(pid, ARG0(r), path);
            } else if (target) {
                target = 0;
                fired = 1;
                if (RESULT(r) == 0) {
                    RESULT(r) = (unsigned long long)-fail;
                    if (regs(pid, &r, 1) != 0) return 125;
                }
            }
            entering = !entering;
        } else if (WSTOPSIG(status) == SIGTRAP && (status >> 16) != 0) {
            /* An exec event stop, not a signal. */
        } else {
            sig = WSTOPSIG(status);
        }
    }
}
