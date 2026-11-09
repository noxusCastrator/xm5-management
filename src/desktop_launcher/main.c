/* XM5 Studio's asInvoker Windows GUI launcher. No shell or command script. */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shellapi.h>
#include <shobjidl.h>
#include <wchar.h>

#define APP_MUTEX L"Global\\XM5Studio.Application.167851CF-0701-4B99-8FE4-AC2EB066C7D8"
#define SETUP_MUTEX L"Global\\XM5Studio.Setup.167851CF-0701-4B99-8FE4-AC2EB066C7D8"
#define CAPACITY 32768

static int fail(const wchar_t *message) {
    MessageBoxW(NULL, message, L"XM5 Studio", MB_OK | MB_ICONERROR);
    return 1;
}

static BOOL append(wchar_t *line, size_t *length, wchar_t ch) {
    if (*length >= CAPACITY - 1) return FALSE;
    line[(*length)++] = ch;
    line[*length] = 0;
    return TRUE;
}

/* Windows CRT quoting, including trailing backslashes and literal quotes. */
static BOOL argument(wchar_t *line, size_t *length, const wchar_t *value) {
    size_t slashes = 0;
    if (*length && !append(line, length, L' ')) return FALSE;
    if (!append(line, length, L'"')) return FALSE;
    for (;;) {
        wchar_t ch = *value++;
        if (ch == L'\\') { slashes++; continue; }
        size_t count = (ch == L'"' || ch == 0) ? slashes * 2 : slashes;
        for (size_t n = 0; n < count; n++) if (!append(line, length, L'\\')) return FALSE;
        slashes = 0;
        if (ch == L'"' && !append(line, length, L'\\')) return FALSE;
        if (!ch) break;
        if (!append(line, length, ch)) return FALSE;
    }
    return append(line, length, L'"');
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR command, int show) {
    (void)instance; (void)previous; (void)command; (void)show;
    wchar_t root[CAPACITY], python[CAPACITY], bootstrap[CAPACITY], line[CAPACITY] = L"";
    DWORD length = GetModuleFileNameW(NULL, root, CAPACITY);
    if (!length || length >= CAPACITY) return fail(L"Cannot locate the application directory.");
    wchar_t *end = wcsrchr(root, L'\\');
    if (!end) return fail(L"Cannot locate the application directory.");
    *end = 0;
    if (wcslen(root) + 32 >= CAPACITY) return fail(L"The application path is too long.");
    swprintf(python, CAPACITY, L"%ls\\vendor\\python\\pythonw.exe", root);
    swprintf(bootstrap, CAPACITY, L"%ls\\bootstrap.py", root);
    if (GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES ||
        GetFileAttributesW(bootstrap) == INVALID_FILE_ATTRIBUTES)
        return fail(L"The bundled runtime is missing. Extract the complete portable package or reinstall XM5 Studio.");

    /* Finish-page launches wait for setup to exit before starting Python. */
    for (int n = 0;; n++) {
        HANDLE setup = OpenMutexW(SYNCHRONIZE, FALSE, SETUP_MUTEX);
        if (!setup) {
            if (GetLastError() != ERROR_FILE_NOT_FOUND) return fail(L"Cannot inspect the setup guard.");
            break;
        }
        CloseHandle(setup);
        if (n >= 150) return fail(L"XM5 Studio setup is running. Finish or close setup, then open the application.");
        Sleep(200);
    }
    HANDLE activity = CreateMutexW(NULL, FALSE, APP_MUTEX);
    if (!activity) return fail(L"Cannot create the application activity guard.");
    HANDLE setup = OpenMutexW(SYNCHRONIZE, FALSE, SETUP_MUTEX);
    if (setup) {
        CloseHandle(setup); CloseHandle(activity);
        return fail(L"XM5 Studio setup started. Finish or close setup, then open the application.");
    }
    int argc = 0;
    LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    size_t used = 0;
    BOOL ok = argv && argument(line, &used, python) && argument(line, &used, L"-B") && argument(line, &used, bootstrap);
    for (int n = 1; ok && n < argc; n++) ok = argument(line, &used, argv[n]);
    if (argv) LocalFree(argv);
    if (!ok) { CloseHandle(activity); return fail(L"The command line is too long."); }
    SetCurrentProcessExplicitAppUserModelID(L"XM5Studio.Desktop");
    SetDllDirectoryW(L"");
    SetEnvironmentVariableW(L"PYTHONHOME", NULL);
    SetEnvironmentVariableW(L"PYTHONPATH", NULL);
    SetEnvironmentVariableW(L"PYTHONNOUSERSITE", L"1");
    STARTUPINFOW startup = {0}; startup.cb = sizeof(startup);
    PROCESS_INFORMATION process = {0};
    ok = CreateProcessW(python, line, NULL, NULL, FALSE,
                        CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW, NULL, root, &startup, &process);
    if (!ok) { CloseHandle(activity); return fail(L"Cannot start the bundled Python runtime. Reinstall XM5 Studio or use XM5Library.cmd --check for diagnostics."); }
    CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hProcess);
    CloseHandle(activity);
    return (int)code;
}
