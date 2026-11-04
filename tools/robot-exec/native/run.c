#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* Single-threaded POSIX evidence executor. It grants no execution permissions.
 * Rich diagnostics and administrative operations reuse the unchanged Python
 * implementation adjacent to this binary; the common execution path uses libc.
 */
typedef struct {
  char *p;
  size_t n, cap;
} Buffer;
static char base[PATH_MAX], directory[PATH_MAX], cwd[PATH_MAX];
static char outpath[PATH_MAX], errpath[PATH_MAX], resultpath[PATH_MAX];
static char started[64];
static int evidence_fd = -1;
static int stdout_fd = -1, stderr_fd = -1, metadata_fd = -1;
static bool native_only = false;
static char **command;
static double timeout = 0, grace = 10, start;
static long long inline_bytes = 2048, max_log_bytes = 2LL << 30;
static pid_t child;

static void fail(const char *message) {
  /* Error strings below contain no control characters except strerror text. */
  fprintf(stdout, "{\"status\":\"FAIL\",\"error\":\"%s\"}\n", message);
  exit(2);
}
static void add(Buffer *b, const char *format, ...) {
  va_list ap, copy;
  va_start(ap, format);
  va_copy(copy, ap);
  int n = vsnprintf(NULL, 0, format, copy);
  va_end(copy);
  if (n < 0)
    abort();
  size_t need = b->n + (size_t)n + 1;
  if (need > b->cap) {
    b->cap = need * 2;
    b->p = realloc(b->p, b->cap);
    if (!b->p)
      abort();
  }
  vsnprintf(b->p + b->n, b->cap - b->n, format, ap);
  va_end(ap);
  b->n += (size_t)n;
}
static void quote(Buffer *b, const char *s) {
  add(b, "\"");
  for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
    if (*p == '"' || *p == '\\')
      add(b, "\\%c", *p);
    else if (*p < 32)
      add(b, "\\u%04x", *p);
    else
      add(b, "%c", *p);
  }
  add(b, "\"");
}
static void field(Buffer *b, const char *key, const char *value) {
  add(b, ",\"%s\":", key);
  quote(b, value);
}
static double now(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (double)t.tv_sec + t.tv_nsec / 1e9;
}
static void timestamp(char *dest, size_t size) {
  struct timespec t;
  struct tm tm;
  clock_gettime(CLOCK_REALTIME, &t);
  gmtime_r(&t.tv_sec, &tm);
  char date[32];
  strftime(date, sizeof date, "%Y-%m-%dT%H:%M:%S", &tm);
  snprintf(dest, size, "%s.%06ld+00:00", date, t.tv_nsec / 1000);
}
static void path(char *dest, const char *parent, const char *leaf) {
  if (snprintf(dest, PATH_MAX, "%s/%s", parent, leaf) >= PATH_MAX)
    fail("Path too long");
}
static long long size_of(int fd) {
  struct stat st;
  if (fstat(fd, &st))
    fail("Cannot stat evidence");
  return st.st_size;
}
static const char *signal_name(int sig, char *buf, size_t size) {
  const char *abbr = sigabbrev_np(sig);
  snprintf(buf, size, "SIG%s", abbr ? abbr : "UNKNOWN");
  return buf;
}
static bool alive(pid_t group) {
  return kill(-group, 0) == 0 || errno == EPERM;
}
static void send_group(pid_t group, int sig) { (void)kill(-group, sig); }
static void pending_signals(const sigset_t *mask, int *received) {
  struct timespec immediate = {0, 0};
  int sig;
  while ((sig = sigtimedwait(mask, NULL, &immediate)) >= 0) {
    if (sig == SIGINT || sig == SIGTERM || sig == SIGHUP) {
      if (!*received)
        *received = sig;
      send_group(child, sig);
    }
  }
}
static void nap(double seconds) {
  struct timespec t = {(time_t)seconds,
                       (long)((seconds - (time_t)seconds) * 1e9)};
  while (nanosleep(&t, &t) && errno == EINTR) {
  }
}
static void metadata(const char *status, int code, int returncode, int received,
                     int crash_signal, bool cleanup, long long outs,
                     long long errs) {
  Buffer b = {0};
  add(&b, "{\"schema\":1,\"status\":");
  quote(&b, status);
  field(&b, "cwd", cwd);
  field(&b, "started_at", started);
  field(&b, "stdout", outpath);
  field(&b, "stderr", errpath);
  add(&b, ",\"wrapper_pid\":%ld,\"timeout_seconds\":", (long)getpid());
  if (timeout)
    add(&b, "%.9g", timeout);
  else
    add(&b, "null");
  add(&b, ",\"command\":[");
  for (int i = 0; command[i]; ++i) {
    if (i)
      add(&b, ",");
    quote(&b, command[i]);
  }
  add(&b, "]");
  if (child)
    add(&b, ",\"pid\":%ld,\"process_group\":%ld", (long)child, (long)child);
  if (strcmp(status, "RUNNING")) {
    char ended[64], sig[32];
    timestamp(ended, sizeof ended);
    field(&b, "finished_at", ended);
    add(&b,
        ",\"exit_code\":%d,\"child_returncode\":%d,\"duration_seconds\":%.4f,"
        "\"interrupted_signal\":",
        code, returncode, now() - start);
    if (received)
      add(&b, "%d", received);
    else
      add(&b, "null");
    add(&b, ",\"signal\":");
    if (crash_signal)
      quote(&b, signal_name(crash_signal, sig, sizeof sig));
    else
      add(&b, "null");
    add(&b,
        ",\"background_children_terminated\":%s,\"bytes\":{\"stdout\":%lld,"
        "\"stderr\":%lld}",
        cleanup ? "true" : "false", outs, errs);
  }
  add(&b, "}\n");
  int fd = openat(evidence_fd, "result.tmp",
                  O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (fd < 0)
    fail("Cannot write metadata");
  size_t at = 0;
  while (at < b.n) {
    ssize_t n = write(fd, b.p + at, b.n - at);
    if (n < 0 && errno == EINTR)
      continue;
    if (n <= 0)
      fail("Cannot write metadata");
    at += (size_t)n;
  }
  if (strcmp(status, "RUNNING")) {
    metadata_fd = fcntl(fd, F_DUPFD_CLOEXEC, 3);
    if (metadata_fd < 0)
      fail("Cannot retain completed metadata descriptor");
  }
  if (close(fd) ||
      renameat(evidence_fd, "result.tmp", evidence_fd, "result.json"))
    fail("Cannot finalize metadata");
  free(b.p);
}
static void fallback(int argc, char **argv) {
  if (native_only)
    fail("Unsupported input for native-only execution");
  char original[PATH_MAX];
  path(original, base, "run-original.py");
  char **args = calloc((size_t)argc + 3, sizeof(char *));
  if (!args)
    abort();
  args[0] = "python3";
  args[1] = "-I";
  args[2] = original;
  for (int i = 1; i < argc; ++i)
    args[i + 2] = argv[i];
  execvp(args[0], args);
  fail("Cannot start Python compatibility path");
}
static double number(const char *s) {
  char *end;
  errno = 0;
  double x = strtod(s, &end);
  if (errno || end == s || *end || !isfinite(x))
    fail("Invalid numeric option");
  return x;
}
static long long integer(const char *s) {
  char *end;
  errno = 0;
  long long x = strtoll(s, &end, 10);
  if (errno || end == s || *end)
    fail("Invalid integer option");
  return x;
}
static pid_t sentinel(pid_t group, int *writer, int out, int err,
                      const sigset_t *oldmask) {
  int pipes[2];
  if (pipe2(pipes, O_CLOEXEC))
    fail("Cannot create sentinel pipe");
  pid_t pid = fork();
  if (pid < 0)
    fail("Cannot fork sentinel");
  if (pid == 0) {
    close(pipes[1]);
    close(out);
    close(err);
    (void)setsid();
    sigprocmask(SIG_SETMASK, oldmask, NULL);
    int null = open("/dev/null", O_RDWR);
    if (null >= 0) {
      dup2(null, 0);
      dup2(null, 1);
      dup2(null, 2);
      if (null > 2)
        close(null);
    }
    char c;
    ssize_t n;
    do {
      n = read(pipes[0], &c, 1);
    } while (n < 0 && errno == EINTR);
    if (n != 1 || c != 'd') {
      send_group(group, SIGTERM);
      nap(grace);
      send_group(group, SIGKILL);
    }
    _exit(0);
  }
  close(pipes[0]);
  *writer = pipes[1];
  return pid;
}
static char *read_inline(const char *name, size_t len, bool *simple) {
  char *s = calloc(len + 1, 1);
  if (!s)
    abort();
  int fd = !strcmp(name, "stdout.log") ? stdout_fd : stderr_fd;
  size_t at = 0;
  while (at < len) {
    ssize_t n = pread(fd, s + at, len - at, (off_t)at);
    if (n < 0 && errno == EINTR)
      continue;
    if (n <= 0)
      break;
    at += (size_t)n;
  }
  for (size_t i = 0; i < at; ++i)
    if ((unsigned char)s[i] >= 128 || s[i] == 0 || s[i] == 27)
      *simple = false;
  return s;
}
static void summary(int code, int crash_signal, long long outs, long long errs,
                    const char *status) {
  Buffer b = {0};
  char sig[32];
  add(&b, "{\"status\":");
  quote(&b, status);
  add(&b,
      ",\"exit_code\":%d,\"duration_seconds\":%.4f,\"bytes\":{\"stdout\":%lld,"
      "\"stderr\":%lld}",
      code, now() - start, outs, errs);
  field(&b, "evidence", directory);
  if (crash_signal)
    field(&b, "signal", signal_name(crash_signal, sig, sizeof sig));
  bool simple = true;
  if (outs + errs <= inline_bytes && outs + errs <= 4000) {
    char *out = read_inline("stdout.log", (size_t)outs, &simple),
         *err = read_inline("stderr.log", (size_t)errs, &simple);
    if (outs)
      field(&b, "stdout", out);
    if (errs)
      field(&b, "stderr", err);
    free(out);
    free(err);
  } else if (code)
    simple = false;
  add(&b, ",\"diagnostics\":[]}\n");
  if (!simple || b.n > 4001) {
    char helper[PATH_MAX], limit[32], meta[64], out[64], err[64];
    path(helper, base, "finalize.py");
    snprintf(limit, sizeof limit, "%lld", inline_bytes);
    snprintf(meta, sizeof meta, "/proc/self/fd/%d", metadata_fd);
    snprintf(out, sizeof out, "/proc/self/fd/%d", stdout_fd);
    snprintf(err, sizeof err, "/proc/self/fd/%d", stderr_fd);
    if (fcntl(metadata_fd, F_SETFD, 0) || fcntl(stdout_fd, F_SETFD, 0) ||
        fcntl(stderr_fd, F_SETFD, 0))
      fail("Cannot retain diagnostic descriptors");
    execlp("python3", "python3", "-I", helper, directory, limit, directory,
           meta, out, err, (char *)NULL);
    fail("Cannot start diagnostic extraction");
  }
  fputs(b.p, stdout);
  free(b.p);
}
int main(int argc, char **argv) {
  char binary[PATH_MAX];
  ssize_t n = readlink("/proc/self/exe", binary, sizeof binary - 1);
  if (n < 0)
    fail("Cannot locate executor");
  binary[n] = 0;
  char *slash = strrchr(binary, '/');
  *slash = 0;
  strcpy(base, binary);
  if (argc < 2 || strcmp(argv[1], "run"))
    fallback(argc, argv);
  if (argc > 2 && !strcmp(argv[2], "--native-only")) {
    native_only = true;
    memmove(argv + 2, argv + 3, (size_t)(argc - 2) * sizeof(char *));
    --argc;
  }
  /* Preserve Python's filesystem/surrogate handling for unusual argv bytes. */
  for (int i = 0; i < argc; ++i)
    for (const unsigned char *p = (const unsigned char *)argv[i]; *p; ++p)
      if (*p >= 128)
        fallback(argc, argv);
  const char *cwd_arg = NULL, *shell = NULL, *shell_executable = "/bin/bash";
  int pos = 2;
  for (; pos < argc; ++pos) {
    char *arg = argv[pos];
    if (!strcmp(arg, "--")) {
      ++pos;
      break;
    }
    if (arg[0] != '-' || !strcmp(arg, "-"))
      break;
    if (pos + 1 >= argc)
      fallback(argc, argv);
    const char *value = argv[pos + 1];
    if (!strcmp(arg, "--cwd"))
      cwd_arg = value;
    else if (!strcmp(arg, "--timeout")) {
      timeout = number(value);
      if (timeout <= 0)
        fail("--timeout must be positive");
    } else if (!strcmp(arg, "--kill-after")) {
      grace = number(value);
      if (grace <= 0)
        fail("--kill-after must be positive");
    } else if (!strcmp(arg, "--inline-bytes"))
      inline_bytes = integer(value);
    else if (!strcmp(arg, "--max-log-bytes")) {
      max_log_bytes = integer(value);
      if (max_log_bytes <= 0)
        fail("--max-log-bytes must be positive");
    } else if (!strcmp(arg, "--shell"))
      shell = value;
    else if (!strcmp(arg, "--shell-executable"))
      shell_executable = value;
    else
      fallback(argc, argv);
    ++pos;
  }
  char *shell_command[] = {(char *)shell_executable, "-c", (char *)shell, NULL};
  if (shell) {
    if (pos < argc)
      fail("Use either --shell or argv after --, not both");
    command = shell_command;
  } else {
    if (pos >= argc)
      fail("A command is required after --");
    command = argv + pos;
  }
  if (!realpath(cwd_arg ? cwd_arg : ".", cwd))
    fallback(argc, argv);
  const char *home = getenv("HOME");
  if (!home)
    fallback(argc, argv);
  for (const unsigned char *p = (const unsigned char *)home; *p; ++p)
    if (*p >= 128)
      fallback(argc, argv);
  for (const unsigned char *p = (const unsigned char *)cwd; *p; ++p)
    if (*p >= 128)
      fallback(argc, argv);
  /* The tasks directory is a configured writable root. Anchor every operation
   * to nofollow descriptors: a concurrent symlink swap must not redirect an
   * approved capability's evidence writes or diagnostic reads outside it. */
  char tasks[PATH_MAX], root[PATH_MAX];
  path(tasks, home, ".agents/tasks");
  int tasks_fd = open(tasks, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  if (tasks_fd < 0)
    fail("Cannot open trusted tasks directory");
  if (mkdirat(tasks_fd, "evidence", 0700) && errno != EEXIST)
    fail("Cannot create evidence root");
  int root_fd = openat(tasks_fd, "evidence",
                       O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  close(tasks_fd);
  if (root_fd < 0)
    fail("Cannot open evidence root without following symlinks");
  if (fchmod(root_fd, 0700))
    fail("Cannot protect evidence root");
  path(root, tasks, "evidence");
  time_t t = time(NULL);
  struct tm tm;
  gmtime_r(&t, &tm);
  char prefix[64];
  strftime(prefix, sizeof prefix, "%Y%m%dT%H%M%S-native-XXXXXX", &tm);
  char anchored[PATH_MAX], descriptor[64];
  snprintf(descriptor, sizeof descriptor, "/proc/self/fd/%d", root_fd);
  path(anchored, descriptor, prefix);
  if (!mkdtemp(anchored))
    fail("Cannot create evidence directory");
  const char *leaf = strrchr(anchored, '/') + 1;
  evidence_fd =
      openat(root_fd, leaf, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  close(root_fd);
  if (evidence_fd < 0)
    fail("Cannot open private evidence directory");
  path(directory, root, leaf);
  path(outpath, directory, "stdout.log");
  path(errpath, directory, "stderr.log");
  path(resultpath, directory, "result.json");
  int out = openat(evidence_fd, "stdout.log",
                   O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600),
      err = openat(evidence_fd, "stderr.log",
                   O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (out < 0 || err < 0)
    fail("Cannot create captured logs");
  stdout_fd = out;
  stderr_fd = err;
  timestamp(started, sizeof started);
  start = now();
  metadata("RUNNING", 0, 0, 0, 0, false, 0, 0);
  sigset_t mask, oldmask;
  sigemptyset(&mask);
  sigaddset(&mask, SIGCHLD);
  sigaddset(&mask, SIGINT);
  sigaddset(&mask, SIGTERM);
  sigaddset(&mask, SIGHUP);
  if (sigprocmask(SIG_BLOCK, &mask, &oldmask))
    fail("Cannot block monitoring signals");
  int gate[2];
  if (pipe2(gate, O_CLOEXEC))
    fail("Cannot create startup pipe");
  child = fork();
  if (child < 0)
    fail("Cannot fork command");
  if (child == 0) {
    close(gate[1]);
    char c;
    ssize_t ready;
    do {
      ready = read(gate[0], &c, 1);
    } while (ready < 0 && errno == EINTR);
    close(gate[0]);
    if (ready != 1 || c != 'g')
      _exit(126);
    if (setsid() < 0)
      _exit(126);
    sigprocmask(SIG_SETMASK, &oldmask, NULL);
    int null = open("/dev/null", O_RDONLY);
    if (null >= 0) {
      dup2(null, 0);
      if (null > 2)
        close(null);
    }
    dup2(out, 1);
    dup2(err, 2);
    close(out);
    close(err);
    if (chdir(cwd)) {
      dprintf(2, "OSError: %s\n", strerror(errno));
      _exit(126);
    }
    execvp(command[0], command);
    int e = errno;
    dprintf(2, "%s: %s: %s\n", e == ENOENT ? "FileNotFoundError" : "OSError",
            command[0], strerror(e));
    _exit(e == ENOENT ? 127 : 126);
  }
  close(gate[0]);
  int writer;
  pid_t guard = sentinel(child, &writer, out, err, &oldmask);
  if (write(gate[1], "g", 1) != 1)
    fail("Cannot release command");
  close(gate[1]);
  metadata("RUNNING", 0, 0, 0, 0, false, 0, 0);
  int ws = 0, received = 0;
  bool timed_out = false, limited = false, cleanup = false;
  double stop = 0;
  for (;;) {
    pid_t done = waitpid(child, &ws, WNOHANG);
    if (done == child)
      break;
    if (done < 0 && errno != EINTR)
      fail("Cannot wait for command");
    double elapsed = now() - start;
    if (!received && timeout && elapsed >= timeout && !timed_out) {
      timed_out = true;
      send_group(child, SIGTERM);
    }
    if (!received && !timed_out && !limited &&
        (size_of(out) > max_log_bytes || size_of(err) > max_log_bytes)) {
      limited = true;
      send_group(child, SIGTERM);
    }
    if ((received || timed_out || limited) && !stop)
      stop = now();
    if (stop && now() - stop >= grace)
      send_group(child, SIGKILL);
    struct timespec tick = {0, 25000000};
    int sig = sigtimedwait(&mask, NULL, &tick);
    if (sig == SIGINT || sig == SIGTERM || sig == SIGHUP) {
      if (!received)
        received = sig;
      send_group(child, sig);
    }
  }
  if (alive(child)) {
    cleanup = true;
    send_group(child, SIGTERM);
    double deadline = now() + grace;
    while (alive(child) && now() < deadline) {
      pending_signals(&mask, &received);
      nap(.025);
    }
    send_group(child, SIGKILL);
  }
  ssize_t finished = write(writer, "d", 1);
  if (finished != 1)
    fail("Cannot finalize sentinel");
  close(writer);
  while (waitpid(guard, NULL, 0) < 0 && errno == EINTR) {
  }
  /* A signal queued as waitpid observes exit must not terminate the wrapper
   * before its evidence is finalized. This process exits immediately after
   * summary, so keep control signals blocked through finalization. */
  pending_signals(&mask, &received);
  int returncode = WIFSIGNALED(ws) ? -WTERMSIG(ws) : WEXITSTATUS(ws);
  int crash_signal =
      returncode < 0 && !received && !timed_out && !limited ? -returncode : 0;
  int code = timed_out        ? 124
             : limited        ? 125
             : received       ? 128 + received
             : returncode < 0 ? 128 - returncode
                              : returncode;
  const char *status = timed_out      ? "TIMEOUT"
                       : limited      ? "LOG_LIMIT"
                       : received     ? "INTERRUPTED"
                       : crash_signal ? "CRASHED"
                       : code         ? "FAIL"
                                      : "PASS";
  long long outs = size_of(out), errs = size_of(err);
  metadata(status, code, returncode, received, crash_signal, cleanup, outs,
           errs);
  summary(code, crash_signal, outs, errs, status);
  close(out);
  close(err);
  close(metadata_fd);
  close(evidence_fd);
  return code;
}
