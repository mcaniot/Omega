#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/openat2.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

/* Narrow capability: fixed read operations, never arbitrary commands/options. */
#ifndef PROJECT
#define PROJECT "/home/mcaniot/Documents/project"
#endif
#ifndef TOOLS
#define TOOLS "/home/mcaniot/.agents/tools"
#endif
static const char *roots[] = {PROJECT, TOOLS, NULL};

static void reject(const char *reason) {
    fprintf(stderr, "local-read: %s; use the general executor with normal approval.\n", reason);
    exit(64);
}

static long number(const char *text, long maximum) {
    char *end;
    errno = 0;
    long value = strtol(text, &end, 10);
    if (errno || !*text || *end || value < 1 || value > maximum)
        reject("invalid line range");
    return value;
}

static bool sensitive(const char *path) {
    char copy[PATH_MAX];
    if (strlen(path) >= sizeof(copy)) return true;
    strcpy(copy, path);
    char *save;
    for (char *part = strtok_r(copy, "/", &save); part; part = strtok_r(NULL, "/", &save)) {
        if (part[0] == '.' || strcasestr(part, "credential") || strcasestr(part, "secret") ||
            strcasestr(part, "password") || strcasestr(part, "token") ||
            strcasestr(part, "private") || strstr(part, ".pem") || strstr(part, ".key") ||
            strstr(part, ".p12") || strstr(part, ".bak")) return true;
    }
    return false;
}

static int source(const char *path) {
    /* Validate lexical and resolved names; open beneath an anchored root atomically. */
    char absolute[PATH_MAX], resolved[PATH_MAX];
    if (path[0] == '/') {
        if (strlen(path) >= sizeof(absolute)) reject("path too long");
        strcpy(absolute, path);
    } else if (snprintf(absolute, sizeof(absolute), "%s/%s", PROJECT, path) >= (int)sizeof(absolute)) {
        reject("path too long");
    }
    if (!realpath(absolute, resolved)) reject("file does not exist");
    const char *relative = NULL, *root = NULL;
    for (int i = 0; roots[i]; ++i) {
        size_t n = strlen(roots[i]);
        if (!strncmp(resolved, roots[i], n) && resolved[n] == '/') {
            root = roots[i]; relative = resolved + n + 1; break;
        }
    }
    if (!root || sensitive(relative)) reject("path is outside approved source files or is sensitive");
    /* Also reject aliases such as .env -> ordinary-name.py. */
    for (int i = 0; roots[i]; ++i) {
        size_t n = strlen(roots[i]);
        if (!strncmp(absolute, roots[i], n) && absolute[n] == '/' && sensitive(absolute + n + 1))
            reject("sensitive or hidden source path");
    }
    int directory = open(root, O_PATH | O_DIRECTORY | O_CLOEXEC);
    if (directory < 0) reject("cannot open approved root");
    struct open_how how = {.flags = O_RDONLY | O_CLOEXEC | O_NONBLOCK,
                          .resolve = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS};
    int fd = (int)syscall(SYS_openat2, directory, relative, &how, sizeof(how));
    close(directory);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_nlink != 1 ||
        st.st_uid != getuid() || st.st_size > 16 * 1024 * 1024)
        reject("source must be a regular file up to 16 MiB (openat2 required)");
    return fd;
}

static int read_source(int argc, char **argv, bool find) {
    if ((!find && (argc < 3 || argc > 5)) || (find && argc != 4)) reject("invalid read arguments");
    const char *path = find ? argv[3] : argv[2];
    const char *pattern = find ? argv[2] : NULL;
    if (pattern && (!*pattern || strlen(pattern) > 512)) reject("literal must contain 1 to 512 bytes");
    long start = !find && argc >= 4 ? number(argv[3], INT_MAX) : 1;
    long count = !find && argc >= 5 ? number(argv[4], 40) : 40;
    FILE *stream = fdopen(source(path), "r");
    if (!stream) reject("cannot read source");
    char *line = NULL;
    size_t capacity = 0, bytes = 0;
    ssize_t size;
    long index = 0, shown = 0;
    while ((size = getline(&line, &capacity, stream)) >= 0) {
        ++index;
        if (index < start || (find && !strstr(line, pattern))) continue;
        size_t length = (size_t)size;
        if (length > 65536 - bytes) length = 65536 - bytes;
        if (find) printf("%ld:", index);
        fwrite(line, 1, length, stdout);
        bytes += length;
        if (++shown >= count || bytes >= 65536) break;
    }
    free(line);
    int failed = ferror(stream);
    fclose(stream);
    return failed ? 1 : 0;
}

int main(int argc, char **argv) {
    /* A trusted capability must not choose evidence or helper executables from caller env. */
    if (clearenv() || setenv("HOME", "/home/mcaniot", 1) || setenv("PATH", "/usr/bin:/bin", 1))
        reject("cannot establish trusted helper environment");
    if (argc >= 2 && !strcmp(argv[1], "--internal-text")) return read_source(argc, argv, false);
    if (argc >= 2 && !strcmp(argv[1], "--internal-find")) return read_source(argc, argv, true);
    if (argc < 2) reject("expected status, files, text or find");
    char self[PATH_MAX], runner[PATH_MAX];
    ssize_t size = readlink("/proc/self/exe", self, sizeof(self) - 1);
    if (size < 0) reject("cannot locate launcher");
    self[size] = 0;
    char *slash = strrchr(self, '/');
    if (!slash) reject("cannot locate launcher directory");
    *slash = 0;
    if (snprintf(runner, sizeof(runner), "%s/native/run", self) >= (int)sizeof(runner)) reject("runner path too long");
    *slash = '/';
    char *command[32] = {runner, "run", "--native-only", "--cwd", PROJECT, "--timeout", "30", "--max-log-bytes", "1048576", "--"};
    int n = 10;
    if (!strcmp(argv[1], "status") && argc == 2) {
        char *args[] = {"/usr/bin/git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
                        "-c", "core.hooksPath=/dev/null", "-C", PROJECT, "status", "--short", "--branch",
                        "--ignore-submodules=all", NULL};
        for (int i = 0; args[i]; ++i) command[n++] = args[i];
        unsetenv("GIT_CONFIG_PARAMETERS"); unsetenv("GIT_CONFIG_COUNT");
        unsetenv("GIT_DIR"); unsetenv("GIT_WORK_TREE");
    } else if (!strcmp(argv[1], "files") && argc == 2) {
        command[n++] = "/usr/bin/rg"; command[n++] = "--files";
        command[n++] = "--"; command[n++] = PROJECT;
    } else if (!strcmp(argv[1], "text") && argc >= 3 && argc <= 5) {
        close(source(argv[2]));
        if (argc >= 4) number(argv[3], INT_MAX);
        if (argc >= 5) number(argv[4], 40);
        command[n++] = self; command[n++] = "--internal-text";
        for (int i = 2; i < argc; ++i) command[n++] = argv[i];
    } else if (!strcmp(argv[1], "find") && argc == 4) {
        close(source(argv[3]));
        if (!*argv[2] || strlen(argv[2]) > 512) reject("literal must contain 1 to 512 bytes");
        command[n++] = self; command[n++] = "--internal-find";
        command[n++] = argv[2]; command[n++] = argv[3];
    } else {
        reject("unsupported operation or arguments");
    }
    command[n] = NULL;
    execv(runner, command);
    perror("local-read: exec native runner");
    return 127;
}
