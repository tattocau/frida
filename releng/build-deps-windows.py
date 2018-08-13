import codecs
import datetime
import glob
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import winreg


BOOTSTRAP_TOOLCHAIN_URL = "https://build.frida.re/toolchain-20180731-windows-x86.exe"
XZ_COMPRESSION_LEVEL = 0 #9


releng_dir = os.path.abspath(os.path.dirname(sys.argv[0]))
root_dir = os.path.dirname(releng_dir)
build_dir = os.path.join(root_dir, "build")
bootstrap_toolchain_dir = os.path.join(build_dir, "fts-toolchain-windows")

cached_meson_params = {}
cached_msvs_dir = None
cached_msvc_dir = None
cached_winxpsdk = None
cached_win10sdk = None

build_platform = 'x86_64' if platform.machine().endswith("64") else 'x86'


def check_environment():
    ensure_bootstrap_toolchain()

    try:
        get_msvs_installation_dir()
        get_winxp_sdk()
        get_win10_sdk()
    except MissingDependencyError as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        sys.exit(1)

    for tool in ["git", "py"]:
        if shutil.which(tool) is None:
            print("ERROR: {} not found".format(tool), file=sys.stderr)
            sys.exit(1)


def build_meson_modules(platform, configuration):
    modules = [
        ("zlib", "zlib.pc", []),
        ("libffi", "libffi.pc", []),
        ("sqlite", "sqlite3.pc", []),
        ("glib", "glib-2.0.pc", ["internal_pcre=true", "tests=false"]),
        ("glib-schannel", "glib-schannel-static.pc", []),
        ("libgee", "gee-0.8.pc", []),
        ("json-glib", "json-glib-1.0.pc", ["introspection=false", "tests=false"]),
        ("libpsl", "libpsl.pc", []),
        ("libxml2", "libxml-2.0.pc", []),
        ("libsoup", "libsoup-2.4.pc", ["gssapi=false", "tls_check=false", "gnome=false", "introspection=false", "tests=false"]),
        ("vala", "valac-0.42.exe", []),
    ]
    for (name, artifact_name, options) in modules:
        if artifact_name.endswith(".pc"):
            artifact_subpath = os.path.join("lib", "pkgconfig", artifact_name)
            runtime_flavors = ['static', 'dynamic']
        elif artifact_name.endswith(".exe"):
            artifact_subpath = os.path.join("bin", artifact_name)
            runtime_flavors = ['static']
        else:
            raise NotImplementedError("Unsupported artifact type")
        for runtime in runtime_flavors:
            artifact_path = os.path.join(get_prefix_path(platform, configuration, runtime), artifact_subpath)
            if not os.path.exists(artifact_path):
                build_meson_module(name, platform, configuration, runtime, options)

def build_meson_module(name, platform, configuration, runtime, options):
    print("*** Building name={} platform={} runtime={} configuration={}".format(name, platform, configuration, runtime))
    env_dir, shell_env = get_meson_params(platform, configuration, runtime)

    source_dir = os.path.join(root_dir, name)
    build_dir = os.path.join(env_dir, name)
    build_type = 'minsize' if configuration == 'Release' else 'debug'
    prefix = get_prefix_path(platform, configuration, runtime)
    option_flags = ["-D" + option for option in options]

    if not os.path.exists(source_dir):
        perform("git", "clone", "--recurse-submodules", "git://github.com/frida/{}.git".format(name), cwd=root_dir)

    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)

    perform(
        "py", "-3", os.path.join(releng_dir, "meson", "meson.py"),
        build_dir,
        "--buildtype", build_type,
        "--msvcrt", runtime,
        "--prefix", prefix,
        "--default-library", "static",
        "--backend", "ninja",
        *option_flags,
        cwd=source_dir,
        env=shell_env
    )

    perform("ninja", "install", cwd=build_dir, env=shell_env)

def get_meson_params(platform, configuration, runtime):
    global cached_meson_params

    identifier = ":".join([platform, configuration, runtime])

    params = cached_meson_params.get(identifier, None)
    if params is None:
        params = generate_meson_params(platform, configuration, runtime)
        cached_meson_params[identifier] = params

    return params

def generate_meson_params(platform, configuration, runtime):
    env = generate_meson_env(platform, configuration, runtime)
    return (env.path, env.shell_env)

def generate_meson_env(platform, configuration, runtime):
    prefix = get_prefix_path(platform, configuration, runtime)
    env_dir = get_tmp_path(platform, configuration, runtime)
    if not os.path.exists(env_dir):
        os.makedirs(env_dir)

    vc_dir = os.path.join(get_msvs_installation_dir(), "VC")
    vc_install_dir = vc_dir + "\\"

    msvc_platform = platform_to_msvc(platform)
    msvc_dir = get_msvc_tool_dir()
    msvc_bin_dir = os.path.join(msvc_dir, "bin", "Host" + platform_to_msvc(build_platform), msvc_platform)

    msvc_dll_dirs = []
    if platform != build_platform:
        build_msvc_platform = platform_to_msvc(build_platform)
        msvc_dll_dirs.append(os.path.join(msvc_dir, "bin", "Host" + build_msvc_platform, build_msvc_platform))

    (winxp_sdk_dir, winxp_sdk_version) = get_winxp_sdk()
    if platform == 'x86':
        winxp_bin_dir = os.path.join(winxp_sdk_dir, "Bin")
        winxp_lib_dir = os.path.join(winxp_sdk_dir, "Lib")
    else:
        winxp_bin_dir = os.path.join(winxp_sdk_dir, "Bin", msvc_platform)
        winxp_lib_dir = os.path.join(winxp_sdk_dir, "Lib", msvc_platform)
    winxp_flags = "/D" + " /D".join([
      "_USING_V110_SDK71_",
      "_UNICODE",
      "UNICODE"
    ])

    (win10_sdk_dir, win10_sdk_version) = get_win10_sdk()

    m4_path = os.path.join(bootstrap_toolchain_dir, "bin", "m4.exe")
    bison_pkgdatadir = os.path.join(bootstrap_toolchain_dir, "share", "bison")

    valac = "valac-0.42.exe"

    exe_path = ";".join([
        os.path.join(prefix, "bin"),
        env_dir,
        os.path.join(bootstrap_toolchain_dir, "bin"),
        winxp_bin_dir,
        msvc_bin_dir,
    ] + msvc_dll_dirs)

    include_path = ";".join([
        os.path.join(msvc_dir, "include"),
        os.path.join(msvc_dir, "atlmfc", "include"),
        os.path.join(vc_dir, "Auxiliary", "VS", "include"),
        os.path.join(win10_sdk_dir, "Include", win10_sdk_version, "ucrt"),
        os.path.join(winxp_sdk_dir, "Include"),
    ])

    library_path = ";".join([
        os.path.join(msvc_dir, "lib", msvc_platform),
        os.path.join(msvc_dir, "atlmfc", "lib", msvc_platform),
        os.path.join(vc_dir, "Auxiliary", "VS", "lib", msvc_platform),
        os.path.join(win10_sdk_dir, "Lib", win10_sdk_version, "ucrt", msvc_platform),
        winxp_lib_dir,
    ])

    env_path = os.path.join(env_dir, "env.bat")
    with codecs.open(env_path, "w", 'utf-8') as f:
        f.write("""@ECHO OFF
set PATH={exe_path};%PATH%
set INCLUDE={include_path}
set LIB={library_path}
set CL={cl_flags}
set VCINSTALLDIR={vc_install_dir}
set Platform={platform}
set M4={m4_path}
set BISON_PKGDATADIR={bison_pkgdatadir}
set VALAC={valac}
""".format(
            exe_path=exe_path,
            include_path=include_path,
            library_path=library_path,
            cl_flags=winxp_flags,
            vc_install_dir=vc_install_dir,
            platform=msvc_platform,
            m4_path=m4_path,
            bison_pkgdatadir=bison_pkgdatadir,
            valac=valac
        ))

    rc_path = os.path.join(winxp_bin_dir, "rc.exe")
    rc_wrapper_path = os.path.join(env_dir, "rc.bat")
    with codecs.open(rc_wrapper_path, "w", 'utf-8') as f:
        f.write("""@ECHO OFF
SETLOCAL EnableExtensions
SET _res=0
"{rc_path}" {flags} %* || SET _res=1
ENDLOCAL & SET _res=%_res%
EXIT /B %_res%""".format(rc_path=rc_path, flags=winxp_flags))

    pkgconfig_path = os.path.join(bootstrap_toolchain_dir, "bin", "pkg-config.exe")
    pkgconfig_lib_dir = os.path.join(prefix, "lib", "pkgconfig")
    pkgconfig_wrapper_path = os.path.join(env_dir, "pkg-config.bat")
    with codecs.open(pkgconfig_wrapper_path, "w", 'utf-8') as f:
        f.write("""@ECHO OFF
SETLOCAL EnableExtensions
SET _res=0
SET PKG_CONFIG_PATH={pkgconfig_lib_dir}
"{pkgconfig_path}" --static %* || SET _res=1
ENDLOCAL & SET _res=%_res%
EXIT /B %_res%""".format(pkgconfig_path=pkgconfig_path, pkgconfig_lib_dir=pkgconfig_lib_dir))

    shell_env = {}
    shell_env.update(os.environ)
    shell_env["PATH"] = exe_path + ";" + shell_env["PATH"]
    shell_env["INCLUDE"] = include_path
    shell_env["LIB"] = library_path
    shell_env["CL"] = winxp_flags
    shell_env["VCINSTALLDIR"] = vc_install_dir
    shell_env["Platform"] = msvc_platform
    shell_env["M4"] = m4_path
    shell_env["BISON_PKGDATADIR"] = bison_pkgdatadir
    shell_env["VALAC"] = valac

    return MesonEnv(env_dir, shell_env)


class MesonEnv(object):
    def __init__(self, path, shell_env):
        self.path = path
        self.shell_env = shell_env


def package():
    now = datetime.datetime.now()

    toolchain_filename = now.strftime("toolchain-%Y%m%d-windows-x86.exe")
    toolchain_path = os.path.join(root_dir, toolchain_filename)

    sdk_filename = now.strftime("sdk-%Y%m%d-windows-any.exe")
    sdk_path = os.path.join(root_dir, sdk_filename)

    if os.path.exists(toolchain_path) and os.path.exists(sdk_path):
        return

    print("About to assemble:")
    print("\t* " + toolchain_filename)
    print("\t* " + sdk_filename)
    print()
    print("Determining what to include...")

    prefixes_dir = os.path.join(build_dir, "fts-windows")
    prefixes_skip_len = len(prefixes_dir) + 1

    sdk_built_files = []
    for prefix in glob.glob(os.path.join(prefixes_dir, "*-static")):
        for root, dirs, files in os.walk(prefix):
            relpath = root[prefixes_skip_len:]
            included_files = map(lambda name: os.path.join(relpath, name),
                filter(lambda filename: file_is_sdk_related(relpath, filename), files))
            sdk_built_files.extend(included_files)
        dynamic_libs = glob.glob(os.path.join(prefix[:-7] + "-dynamic", "lib", "**", "*.a"), recursive=True)
        dynamic_libs = [path[prefixes_skip_len:] for path in dynamic_libs]
        sdk_built_files.extend(dynamic_libs)

    toolchain_files = []
    for root, dirs, files in os.walk(get_prefix_path('x86', 'Release', 'static')):
        relpath = root[prefixes_skip_len:]
        included_files = map(lambda name: os.path.join(relpath, name),
            filter(lambda filename: file_is_vala_toolchain_related(relpath, filename), files))
        toolchain_files.extend(included_files)

    toolchain_mixin_files = []
    for root, dirs, files in os.walk(bootstrap_toolchain_dir):
        relpath = root[len(bootstrap_toolchain_dir) + 1:]
        included_files = map(lambda name: os.path.join(relpath, name),
            filter(lambda filename: not file_is_vala_toolchain_related(relpath, filename), files))
        toolchain_mixin_files.extend(included_files)

    sdk_built_files.sort()
    toolchain_files.sort()

    print("Copying files...")
    tempdir = tempfile.mkdtemp(prefix="frida-package")
    copy_files(prefixes_dir, sdk_built_files, os.path.join(tempdir, "sdk-windows"), transform_sdk_dest)
    copy_files(prefixes_dir, toolchain_files, os.path.join(tempdir, "toolchain-windows"), transform_toolchain_dest)
    copy_files(bootstrap_toolchain_dir, toolchain_mixin_files, os.path.join(tempdir, "toolchain-windows"))

    print("Compressing...")
    prevdir = os.getcwd()
    os.chdir(tempdir)

    compression_switch = "-mx{}".format(XZ_COMPRESSION_LEVEL)

    perform("7z", "a", compression_switch, "-sfx7zCon.sfx", "-r", toolchain_path, "toolchain-windows")

    perform("7z", "a", compression_switch, "-sfx7zCon.sfx", "-r", sdk_path, "sdk-windows")

    os.chdir(prevdir)
    shutil.rmtree(tempdir)

    print("All done.")

def file_is_sdk_related(directory, filename):
    parts = directory.split("\\")
    rootdir = parts[0]
    subdir = parts[1]
    subpath = "\\".join(parts[1:])

    if subdir == "bin":
        return False

    if subdir == "lib" and ("vala" in subpath or "vala" in filename):
        return False

    base, ext = os.path.splitext(filename)
    ext = ext[1:]
    if ext == "pc":
        return False

    if ext == "h" and base.startswith("vala"):
        return False

    if ext in ("vapi", "deps"):
        return not directory.endswith("share\\vala-0.42\\vapi")

    return "\\share\\" not in directory

def file_is_vala_toolchain_related(directory, filename):
    base, ext = os.path.splitext(filename)
    ext = ext[1:]
    if ext in ('vapi', 'deps'):
        return directory.endswith("share\\vala-0.42\\vapi")
    return filename == "valac-0.42.exe"

def transform_identity(srcfile):
    return srcfile

def transform_sdk_dest(srcfile):
    parts = os.path.dirname(srcfile).split("\\")
    rootdir = parts[0]
    subpath = "\\".join(parts[1:])

    filename = os.path.basename(srcfile)

    platform, configuration, runtime = rootdir.split("-")
    rootdir = "-".join([
        platform_to_msvc(platform),
        configuration.title()
    ])

    if runtime == 'dynamic' and subpath.split("\\")[0] == "lib":
        subpath = "lib-dynamic" + subpath[3:]

    if filename.endswith(".a"):
        if filename.startswith("lib"):
            filename = filename[3:]
        stem, ext = os.path.splitext(filename)
        filename = stem + ".lib"

    return os.path.join(rootdir, subpath, filename)

def transform_toolchain_dest(srcfile):
    return srcfile[srcfile.index("\\") + 1:]


def ensure_bootstrap_toolchain():
    if os.path.exists(bootstrap_toolchain_dir):
        return

    print("Downloading bootstrap toolchain...")
    with urllib.request.urlopen(BOOTSTRAP_TOOLCHAIN_URL) as response, \
            tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as archive:
        shutil.copyfileobj(response, archive)
        toolchain_archive_path = archive.name

    print("Extracting bootstrap toolchain...")
    try:
        tempdir = tempfile.mkdtemp(prefix="frida-bootstrap-toolchain")
        try:
            try:
                subprocess.check_output([
                    toolchain_archive_path,
                    "-o" + tempdir,
                    "-y"
                ])
            except subprocess.CalledProcessError as e:
                print("Oops:", e.output.decode('utf-8'))
                raise e
            shutil.move(os.path.join(tempdir, "toolchain-windows"), bootstrap_toolchain_dir)
        finally:
            shutil.rmtree(tempdir)
    finally:
        os.unlink(toolchain_archive_path)

def get_prefix_path(platform, configuration, runtime):
    return os.path.join(build_dir, "fts-windows", "{}-{}-{}".format(platform, configuration.lower(), runtime))

def get_tmp_path(platform, configuration, runtime):
    return os.path.join(build_dir, "fts-tmp-windows", "{}-{}-{}".format(platform, configuration.lower(), runtime))

def platform_to_msvc(platform):
    return 'x64' if platform == 'x86_64' else 'x86'

def get_msvs_installation_dir():
    global cached_msvs_dir
    if cached_msvs_dir is None:
        installations = json.loads(subprocess.check_output([
            os.path.join(bootstrap_toolchain_dir, "bin", "vswhere.exe"),
            "-version", "15.0",
            "-format", "json",
            "-property", "installationPath"
        ]))
        if len(installations) == 0:
            raise MissingDependencyError("Visual Studio 2017 is not installed")
        cached_msvs_dir = installations[0]['installationPath']
    return cached_msvs_dir

def get_msvc_tool_dir():
    global cached_msvc_dir
    if cached_msvc_dir is None:
        msvs_dir = get_msvs_installation_dir()
        version = sorted(glob.glob(os.path.join(msvs_dir, "VC", "Tools", "MSVC", "*.*.*")))[-1]
        cached_msvc_dir = os.path.join(msvs_dir, "VC", "Tools", "MSVC", version)
    return cached_msvc_dir

def get_winxp_sdk():
    global cached_winxpsdk
    if cached_winxpsdk is None:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Microsoft SDKs\Windows\v7.1A")
            try:
                (root_dir, _) = winreg.QueryValueEx(key, "InstallationFolder")
                (version, _) = winreg.QueryValueEx(key, "ProductVersion")
                cached_winxpsdk = (root_dir, version)
            finally:
                winreg.CloseKey(key)
        except Exception as e:
            raise MissingDependencyError("Windows XP SDK is not installed")
    return cached_winxpsdk

def get_win10_sdk():
    global cached_win10sdk
    if cached_win10sdk is None:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows Kits\Installed Roots")
            try:
                (root_dir, _) = winreg.QueryValueEx(key, "KitsRoot10")
                version = os.path.basename(sorted(glob.glob(os.path.join(root_dir, "Include", "*.*.*")))[-1])
                cached_win10sdk = (root_dir, version)
            finally:
                winreg.CloseKey(key)
        except Exception as e:
            raise MissingDependencyError("Windows 10 SDK is not installed")
    return cached_win10sdk


def perform(*args, **kwargs):
    print(" ".join(args))
    subprocess.check_call(args, **kwargs)

def copy_files(fromdir, files, todir, transformdest=transform_identity):
    for file in files:
        src = os.path.join(fromdir, file)
        dst = os.path.join(todir, transformdest(file))
        dstdir = os.path.dirname(dst)
        if not os.path.isdir(dstdir):
            os.makedirs(dstdir)
        shutil.copyfile(src, dst)


class MissingDependencyError(Exception):
    pass


if __name__ == '__main__':
    check_environment()

    ##for platform in ["x86_64", "x86"]:
    for platform in ["x86"]:
        #for configuration in ["Debug", "Release"]:
        for configuration in ["Release"]:
            build_meson_modules(platform, configuration)

    package()
