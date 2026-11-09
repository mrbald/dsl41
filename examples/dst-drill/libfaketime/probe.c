#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <gnu/libc-version.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdarg.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>

#ifndef _STAT_VER
#error "Target glibc headers must expose their legacy _STAT_VER; do not guess its value"
#endif

static int channel, file_fd, generation;
static char **original_argv;
static pid_t child_pid = -1;
static int (*legacy_stat)(int, int, struct stat64 *);
static struct stat reference;

static void fail(const char *what)
{
    perror(what);
    _exit(70);
}

static void emit(const char *format, ...)
{
    char data[4096];
    va_list args;
    va_start(args, format);
    int length = vsnprintf(data, sizeof(data), format, args);
    va_end(args);
    if (length < 0 || length >= (int)sizeof(data)) fail("event length");
    if (send(channel, data, (size_t)length, MSG_NOSIGNAL) != length) fail("send");
}

static int64_t reading(clockid_t clock)
{
    struct timespec value;
    if (clock_gettime(clock, &value)) fail("clock_gettime");
    if (value.tv_nsec < 0 || value.tv_nsec >= 1000000000) fail("clock nanoseconds");
    return (int64_t)value.tv_sec * 1000000000 + value.tv_nsec;
}

static const char *shared(void)
{
    const char *value = getenv("FAKETIME_SHARED");
    return value == NULL ? "" : value;
}

static unsigned long long start_token(void)
{
    char data[4096];
    int fd = open("/proc/self/stat", O_RDONLY);
    if (fd < 0) fail("open proc stat");
    ssize_t count = read(fd, data, sizeof(data) - 1);
    if (count <= 0) fail("read proc stat");
    close(fd);
    data[count] = '\0';
    char *tail = strrchr(data, ')');
    if (tail == NULL) fail("parse proc stat");
    char *save, *word = strtok_r(tail + 2, " ", &save);
    for (int field = 3; field < 22 && word != NULL; field++)
        word = strtok_r(NULL, " ", &save);
    if (word == NULL) fail("parse proc start token");
    return strtoull(word, NULL, 10);
}

static void identity(const char *kind, const char *origin)
{
    emit("{\"kind\":\"%s\",\"origin\":\"%s\",\"pid\":%ld,\"ppid\":%ld,"
         "\"token\":%llu,\"generation\":%d,\"shared\":\"%s\",\"fake_pid\":%s}",
         kind, origin, (long)syscall(SYS_getpid), (long)getppid(), start_token(),
         generation, shared(), getenv("FAKETIME_FAKEPID") == NULL ? "false" : "true");
}

static void sample(void)
{
    int64_t mono = reading(CLOCK_MONOTONIC);
    int64_t wall = reading(CLOCK_REALTIME);
    emit("{\"kind\":\"sample\",\"generation\":%d,\"mono\":%" PRId64
         ",\"wall\":%" PRId64 "}", generation, mono, wall);
}

#define METADATA(s) (uintmax_t)(s).st_dev, (uintmax_t)(s).st_ino, \
    (uintmax_t)(s).st_mode, (uintmax_t)(s).st_nlink, (uintmax_t)(s).st_uid, \
    (uintmax_t)(s).st_gid, (uintmax_t)(s).st_rdev, (intmax_t)(s).st_size, \
    (intmax_t)(s).st_blksize, (intmax_t)(s).st_blocks
#define TIMES(s) (intmax_t)(s).st_atim.tv_sec * 1000000000 + (s).st_atim.tv_nsec, \
    (intmax_t)(s).st_mtim.tv_sec * 1000000000 + (s).st_mtim.tv_nsec, \
    (intmax_t)(s).st_ctim.tv_sec * 1000000000 + (s).st_ctim.tv_nsec
#define STAT_FORMAT "\"meta\":[%ju,%ju,%ju,%ju,%ju,%ju,%ju,%jd,%jd,%jd]," \
    "\"times\":[%jd,%jd,%jd]}"
#define SAME_METADATA(a, b) ((a).st_dev == (b).st_dev && (a).st_ino == (b).st_ino && \
    (a).st_mode == (b).st_mode && (a).st_nlink == (b).st_nlink && \
    (a).st_uid == (b).st_uid && (a).st_gid == (b).st_gid && (a).st_rdev == (b).st_rdev && \
    (a).st_size == (b).st_size && (a).st_blksize == (b).st_blksize && (a).st_blocks == (b).st_blocks)

static void stats(void)
{
    struct stat normal;
    struct stat64 legacy;
    if (fstat(file_fd, &normal)) fail("fstat");
    if (legacy_stat(_STAT_VER, file_fd, &legacy)) fail("__fxstat64");
    emit("{\"kind\":\"stat\",\"api\":\"fstat\"," STAT_FORMAT,
         METADATA(normal), TIMES(normal));
    emit("{\"kind\":\"stat\",\"api\":\"__fxstat64\"," STAT_FORMAT,
         METADATA(legacy), TIMES(legacy));
}

struct thread_result { int count, error; int64_t first, last, wall_first, wall_last; };

static void *thread_read(void *argument)
{
    struct thread_result *result = argument;
    int64_t previous = -1, previous_wall = -1;
    for (int i = 0; i < 64; i++) {
        struct stat normal;
        struct stat64 legacy;
        int64_t mono = reading(CLOCK_MONOTONIC), wall = reading(CLOCK_REALTIME);
        if (fstat(file_fd, &normal) || legacy_stat(_STAT_VER, file_fd, &legacy) ||
            !SAME_METADATA(normal, reference) || !SAME_METADATA(legacy, reference) ||
            mono < previous || wall < previous_wall) result->error = 1;
        if (!i) { result->first = mono; result->wall_first = wall; }
        result->last = mono;
        result->wall_last = wall;
        previous = mono;
        previous_wall = wall;
        result->count++;
    }
    return NULL;
}

static void threads(void)
{
    pthread_t ids[2];
    struct thread_result results[2] = {{0}, {0}};
    for (int i = 0; i < 2; i++)
        if (pthread_create(&ids[i], NULL, thread_read, &results[i])) fail("pthread_create");
    for (int i = 0; i < 2; i++) {
        if (pthread_join(ids[i], NULL)) fail("pthread_join");
        emit("{\"kind\":\"thread\",\"index\":%d,\"count\":%d,\"error\":%d,"
             "\"first\":%" PRId64 ",\"last\":%" PRId64 ",\"wall_first\":%" PRId64
             ",\"wall_last\":%" PRId64 "}", i, results[i].count, results[i].error,
             results[i].first, results[i].last, results[i].wall_first, results[i].wall_last);
    }
}

static void spawn_descendant(void)
{
    if (generation >= 2 || child_pid != -1) fail("descendant bound");
    child_pid = fork();
    if (child_pid < 0) fail("fork");
    if (child_pid == 0) {
        close(channel);
        channel = socket(AF_UNIX, SOCK_SEQPACKET, 0);
        if (channel < 0) fail("socket");
        struct sockaddr_un address = {.sun_family = AF_UNIX};
        if (strlen(original_argv[3]) >= sizeof(address.sun_path)) fail("socket path");
        strcpy(address.sun_path, original_argv[3]);
        if (connect(channel, (struct sockaddr *)&address, sizeof(address))) fail("connect");
        generation++;
        identity("gate", "loaded_descendant");
        int permission_pipe[2];
        if (pipe(permission_pipe)) fail("pipe");
        union { struct cmsghdr alignment; char bytes[CMSG_SPACE(sizeof(int))]; } ancillary = {0};
        char tag = 'P';
        struct iovec iov = {.iov_base = &tag, .iov_len = 1};
        struct msghdr message = {.msg_iov = &iov, .msg_iovlen = 1,
                                 .msg_control = ancillary.bytes, .msg_controllen = sizeof(ancillary.bytes)};
        struct cmsghdr *header = CMSG_FIRSTHDR(&message);
        header->cmsg_level = SOL_SOCKET;
        header->cmsg_type = SCM_RIGHTS;
        header->cmsg_len = CMSG_LEN(sizeof(int));
        memcpy(CMSG_DATA(header), &permission_pipe[1], sizeof(int));
        if (sendmsg(channel, &message, MSG_NOSIGNAL) != 1) fail("send permission pipe");
        close(permission_pipe[1]);
        char permission;
        if (read(permission_pipe[0], &permission, 1) != 1 || permission != 'G') _exit(71);
        close(permission_pipe[0]);
        char fd_text[24], generation_text[24];
        snprintf(fd_text, sizeof(fd_text), "%d", channel);
        snprintf(generation_text, sizeof(generation_text), "%d", generation);
        char *arguments[] = {original_argv[0], "timeline", fd_text, original_argv[3],
                             original_argv[4], generation_text, NULL};
        execv(arguments[0], arguments);
        fail("execv descendant");
    }
    emit("{\"kind\":\"spawn\",\"pid\":%ld,\"generation\":%d}",
         (long)child_pid, generation + 1);
}

int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "abi")) {
        printf("{\"stat_ver\":%d,\"futex_syscall\":%d,\"sigusr1\":%d,\"glibc\":\"%s\"}\n",
               _STAT_VER, SYS_futex, SIGUSR1, gnu_get_libc_version());
        return 0;
    }
    if (argc != 6) return 64;
    original_argv = argv;
    channel = atoi(argv[2]);
    generation = atoi(argv[5]);
    if (generation < 0 || generation > 2) return 64;
    legacy_stat = dlsym(RTLD_DEFAULT, "__fxstat64");
    if (!legacy_stat) fail("dlsym __fxstat64");
    Dl_info clock_info, stat_info, fstat_info;
    if (!dladdr(dlsym(RTLD_DEFAULT, "clock_gettime"), &clock_info) ||
        !dladdr((void *)legacy_stat, &stat_info) ||
        !dladdr(dlsym(RTLD_DEFAULT, "fstat"), &fstat_info)) fail("dladdr");
    identity("ready", "preloaded");
    emit("{\"kind\":\"library\",\"clock\":\"%s\",\"legacy\":\"%s\","
         "\"fstat\":\"%s\",\"stat_ver\":%d}", clock_info.dli_fname,
         stat_info.dli_fname, fstat_info.dli_fname, _STAT_VER);
    file_fd = open(argv[4], O_RDONLY | O_CLOEXEC);
    if (file_fd < 0) fail("open fixture");
    if (fstat(file_fd, &reference)) fail("fstat reference");
    for (;;) {
        char command;
        if (recv(channel, &command, 1, 0) != 1) return 72;
        if (command == 'S') sample();
        else if (command == 'M') stats();
        else if (command == 'T' && generation == 0) threads();
        else if (command == 'F') spawn_descendant();
        else if (command == 'Q') {
            if (child_pid > 0) {
                int status;
                if (waitpid(child_pid, &status, 0) != child_pid ||
                    !WIFEXITED(status) || WEXITSTATUS(status)) fail("waitpid");
            }
            emit("{\"kind\":\"complete\",\"generation\":%d}", generation);
            printf("COMPLETE %s %d\n", argv[1], generation);
            fflush(stdout);
            close(file_fd);
            return 0;
        } else return 73;
    }
}
