/* SPDX-License-Identifier: MIT */
/* Main executable of HandNavTracker.app: a windowless app that runs run_tracker.command.
 *
 * macOS grants camera access to the application responsible for a process. FreeCAD.app declares
 * no camera use, and with a script as the app's executable macOS holds Python itself responsible
 * and refuses without asking. A native executable makes this app the responsible one: macOS asks
 * the user once, on behalf of "HandNav Tracker". It must stay alive as the tracker's parent.
 */
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(-child, sig);   /* the whole group: shell and Python */
}

int main(void) {
    char raw[PATH_MAX], dir[PATH_MAX], path[PATH_MAX + 32];
    uint32_t size = sizeof raw;
    if (_NSGetExecutablePath(raw, &size) != 0 || !realpath(raw, dir)) return 1;
    for (int i = 0; i < 4; i++) {       /* HandNav/HandNavTracker.app/Contents/MacOS/HandNavTracker -> HandNav */
        char *slash = strrchr(dir, '/');
        if (!slash) return 1;
        *slash = 0;
    }
    if (chdir(dir) != 0) return 1;

    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    child = fork();
    if (child < 0) return 1;
    if (child == 0) {
        setpgid(0, 0);
        signal(SIGTERM, SIG_DFL);
        signal(SIGINT, SIG_DFL);
        signal(SIGHUP, SIG_DFL);
        int in = open("/dev/null", O_RDONLY);
        int log = open("tracker.log", O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (in >= 0) dup2(in, 0);
        if (log >= 0) { dup2(log, 1); dup2(log, 2); }
        snprintf(path, sizeof path, "%s/run_tracker.command", dir);
        execl("/bin/zsh", "zsh", path, (char *)NULL);
        _exit(127);
    }
    setpgid(child, child);              /* also here, whichever of the two runs first */
    int status = 0;
    while (waitpid(child, &status, 0) < 0)
        if (errno != EINTR) return 1;   /* EINTR: a signal was forwarded, keep waiting */
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
