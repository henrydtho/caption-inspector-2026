/*
 * Caption Inspector - bundle launcher.
 *
 * The main executable of the self-contained .app. It sets the environment the
 * embedded interpreter needs and execs it; that is the whole job.
 *
 * It is a compiled binary rather than the obvious shell script because macOS
 * treats the two very differently. A .app whose CFBundleExecutable is a script
 * signs as "app bundle with generic", `codesign --verify` on it fails, and
 * Gatekeeper will not accept it on a machine that downloaded it - which makes
 * the app undistributable no matter how carefully everything inside it was
 * signed. A Mach-O main executable signs and notarizes normally.
 *
 * Everything it points at is inside the bundle:
 *
 *     <app>/Contents/MacOS/caption-inspector-app   this binary
 *     <app>/Contents/Resources/runtime/...         the interpreter
 *     <app>/Contents/Resources/vendor/bin          ffmpeg, ffprobe
 *     <app>/Contents/Resources/python              the app
 *
 * PATH is replaced rather than prepended, so a Homebrew ffmpeg on the target
 * machine cannot be picked up by accident.
 */

#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Longest path we build is <resources>/python/launch_app.py plus slack. */
#define CI_PATH_MAX (PATH_MAX * 2)

static void fail(const char *message) {
    /*
     * A .app launched from Finder has nowhere to print, so failures that stop
     * the launch put up an alert. Without this the app just silently does
     * nothing, which is the worst possible report.
     */
    char script[CI_PATH_MAX];
    snprintf(script, sizeof(script),
             "display alert \"Caption Inspector\" message \"%s\" as warning",
             message);

    pid_t child = fork();
    if (child == 0) {
        execl("/usr/bin/osascript", "osascript", "-e", script, (char *)NULL);
        _exit(127);
    }
    fprintf(stderr, "caption-inspector: %s\n", message);
}

/* Directory containing this executable, resolved through any symlinks. */
static int executable_directory(char *out, size_t size) {
    char raw[CI_PATH_MAX];
    uint32_t length = (uint32_t)sizeof(raw);

    if (_NSGetExecutablePath(raw, &length) != 0) {
        return -1;
    }

    char resolved[CI_PATH_MAX];
    if (realpath(raw, resolved) == NULL) {
        /* Not fatal: an unresolvable path still usually has a usable dirname. */
        snprintf(resolved, sizeof(resolved), "%s", raw);
    }

    char *directory = dirname(resolved);
    if (directory == NULL) {
        return -1;
    }

    snprintf(out, size, "%s", directory);
    return 0;
}

static int path_exists(const char *path) {
    return access(path, F_OK) == 0;
}

int main(int argc, char *argv[]) {
    char macos_dir[CI_PATH_MAX];
    if (executable_directory(macos_dir, sizeof(macos_dir)) != 0) {
        fail("Could not work out where the application is installed.");
        return 1;
    }

    char resources[CI_PATH_MAX];
    snprintf(resources, sizeof(resources), "%s/../Resources", macos_dir);

    char resolved_resources[CI_PATH_MAX];
    if (realpath(resources, resolved_resources) == NULL) {
        fail("This copy of Caption Inspector is incomplete: its Resources folder is missing.");
        return 1;
    }

    char runtime[CI_PATH_MAX];
    snprintf(runtime, sizeof(runtime),
             "%s/runtime/Python.framework/Versions/Current", resolved_resources);

    char python_bin[CI_PATH_MAX];
    snprintf(python_bin, sizeof(python_bin), "%s/bin/python3", runtime);

    if (!path_exists(python_bin)) {
        fail("This copy of Caption Inspector is incomplete: the bundled Python runtime "
             "is missing. Please install it again from the disk image.");
        return 1;
    }

    char entry_point[CI_PATH_MAX];
    snprintf(entry_point, sizeof(entry_point), "%s/python/launch_app.py", resolved_resources);
    if (!path_exists(entry_point)) {
        fail("This copy of Caption Inspector is incomplete: its application files are missing.");
        return 1;
    }

    char python_path[CI_PATH_MAX];
    snprintf(python_path, sizeof(python_path), "%s/python:%s/vendor/site-packages",
             resolved_resources, resolved_resources);

    char search_path[CI_PATH_MAX];
    snprintf(search_path, sizeof(search_path), "%s/vendor/bin:/usr/bin:/bin:/usr/sbin:/sbin",
             resolved_resources);

    setenv("PYTHONHOME", runtime, 1);
    setenv("PYTHONPATH", python_path, 1);
    setenv("PATH", search_path, 1);
    setenv("CAPTION_INSPECTOR_RESOURCES", resolved_resources, 1);

    /*
     * Writing bytecode into the interpreter's own framework adds files to a
     * sealed bundle and invalidates its code signature, so the app would break
     * its own signature simply by being run.
     */
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);

    /* No model downloads, no hub lookups, no telemetry. The weights are local. */
    setenv("HF_HUB_OFFLINE", "1", 1);
    setenv("TRANSFORMERS_OFFLINE", "1", 1);
    setenv("HF_HUB_DISABLE_TELEMETRY", "1", 1);
    setenv("TOKENIZERS_PARALLELISM", "false", 1);

    if (chdir(resolved_resources) != 0) {
        fail("Could not open the application's own folder.");
        return 1;
    }

    /* python3 launch_app.py [args the app was opened with] */
    char **child_argv = calloc((size_t)argc + 3, sizeof(char *));
    if (child_argv == NULL) {
        fail("Out of memory while starting.");
        return 1;
    }

    int index = 0;
    child_argv[index++] = python_bin;
    child_argv[index++] = entry_point;
    for (int i = 1; i < argc; i++) {
        child_argv[index++] = argv[i];
    }
    child_argv[index] = NULL;

    execv(python_bin, child_argv);

    /* execv only returns on failure. */
    char message[CI_PATH_MAX];
    snprintf(message, sizeof(message),
             "Caption Inspector could not start its interpreter (%s).", strerror(errno));
    fail(message);
    free(child_argv);
    return 1;
}
