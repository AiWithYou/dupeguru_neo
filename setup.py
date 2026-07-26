import os
import shutil
import sys
import tempfile

from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext
from setuptools.command.build_py import build_py
from setuptools.errors import CompileError, ExecError

HELP_LANGUAGES = ("en", "de", "fr", "hy", "ru", "uk")


class BuildPyWithRuntimeData(build_py):
    """Build the help and gettext catalogs that the installed GUI opens."""

    def run(self):
        super().run()
        self._build_localizations()
        self._build_help()
        self._remove_generated_bytecode()

    def byte_compile(self, files):
        """Keep build-root-specific code filenames out of distributable wheels."""

    def _remove_generated_bytecode(self):
        build_root = Path(self.build_lib)
        for pattern in ("*.pyc", "*.pyo"):
            for bytecode in build_root.rglob(pattern):
                bytecode.unlink()
        for cache_directory in sorted(
            build_root.rglob("__pycache__"),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            shutil.rmtree(cache_directory)

    def _build_localizations(self):
        import polib

        source_root = Path("locale")
        destination_root = Path(self.build_lib, "locale")
        shutil.rmtree(destination_root, ignore_errors=True)
        compiled = 0
        for source in sorted(source_root.glob("*/LC_MESSAGES/*.po")):
            destination = destination_root.joinpath(source.relative_to(source_root)).with_suffix(".mo")
            destination.parent.mkdir(parents=True, exist_ok=True)
            polib.pofile(str(source)).save_as_mofile(str(destination))
            compiled += 1
        if compiled == 0:
            raise RuntimeError("wheel build did not find any gettext PO catalogs")

    def _build_help(self):
        build_library = str(Path(self.build_lib).resolve())
        sys.path.insert(0, build_library)
        try:
            from hscommon import sphinxgen
        finally:
            sys.path.remove(build_library)

        source_root = Path("help")
        destination_root = Path(self.build_lib, "help")
        shutil.rmtree(destination_root, ignore_errors=True)
        with tempfile.TemporaryDirectory(prefix="dupeguru-wheel-help-") as temporary:
            staging_root = Path(temporary)
            for language in HELP_LANGUAGES:
                language_source = source_root.joinpath(language)
                if not language_source.is_dir():
                    raise RuntimeError("wheel help source is missing language {!r}".format(language))
                staged_source = staging_root.joinpath(language)
                shutil.copytree(
                    language_source,
                    staged_source,
                    ignore=shutil.ignore_patterns("conf.py", "changelog.rst", "__pycache__", "*.pyc"),
                )
                if language == "en":
                    # API/autodoc pages belong in the source distribution. The
                    # installed desktop help is deliberately self-contained and
                    # must not import the application (or all runtime
                    # dependencies) inside PEP 517's isolated build environment.
                    shutil.rmtree(staged_source.joinpath("developer"), ignore_errors=True)
                    index_path = staged_source.joinpath("index.rst")
                    index_path.write_text(
                        index_path.read_text(encoding="utf-8").replace("    developer/index\n", ""),
                        encoding="utf-8",
                        newline="\n",
                    )
                    contribute_path = staged_source.joinpath("contribute.rst")
                    contribute_path.write_text(
                        contribute_path.read_text(encoding="utf-8").replace(
                            ":doc:`developer documentation </developer/index>`",
                            "developer documentation in the source distribution",
                        ),
                        encoding="utf-8",
                        newline="\n",
                    )
                destination = destination_root.joinpath(language)
                sphinxgen.gen(
                    staged_source,
                    destination,
                    source_root.joinpath("changelog"),
                    "https://github.com/AiWithYou/dupeguru_neo/issues/{}",
                    {"language": language},
                    source_root.joinpath("conf.tmpl"),
                    source_root.joinpath("changelog.tmpl"),
                )
                shutil.rmtree(destination.joinpath(".doctrees"), ignore_errors=True)
                destination.joinpath(".buildinfo").unlink(missing_ok=True)
                if not destination.joinpath("index.html").is_file():
                    raise RuntimeError("wheel help build did not produce {!r}".format(language))


class ReproducibleBuildExt(build_ext):
    """Remove build-root identity from native release artifacts."""

    def build_extensions(self):
        if os.environ.get("SOURCE_DATE_EPOCH") is not None:
            source_root = Path(__file__).resolve().parent
            compiler_type = self.compiler.compiler_type
            if compiler_type == "msvc":
                self._configure_msvc(source_root)
            elif compiler_type == "unix":
                self._configure_unix(source_root)
                if sys.platform == "darwin":
                    self._configure_darwin()
        super().build_extensions()

    def _configure_msvc(self, source_root):
        compile_args = (
            "/Brepro",
            "/experimental:deterministic",
            f"/pathmap:{source_root}=.",
        )
        for extension in self.extensions:
            extension.extra_compile_args = _append_unique(extension.extra_compile_args, compile_args)
            extension.extra_link_args = _append_unique(extension.extra_link_args, ("/Brepro",))

    def _configure_unix(self, source_root):
        candidates = (
            f"-ffile-prefix-map={source_root}=.",
            f"-fdebug-prefix-map={source_root}=.",
        )
        compile_args = self._supported_compile_args(candidates)
        for extension in self.extensions:
            extension.extra_compile_args = _append_unique(extension.extra_compile_args, compile_args)

    def _configure_darwin(self):
        for extension in self.extensions:
            extension.extra_link_args = _append_unique(
                extension.extra_link_args,
                ("-Wl,-reproducible",),
            )

    def _supported_compile_args(self, candidates):
        supported = []
        with tempfile.TemporaryDirectory(prefix="dupeguru-compiler-probe-") as temporary:
            probe_root = Path(temporary)
            probe_source = probe_root.joinpath("prefix_map_probe.c")
            probe_source.write_text("int dupeguru_prefix_map_probe(void) { return 0; }\n", encoding="ascii")
            for index, candidate in enumerate(candidates):
                output_directory = probe_root.joinpath(f"object-{index}")
                try:
                    self.compiler.compile(
                        [str(probe_source)],
                        output_dir=str(output_directory),
                        extra_postargs=[candidate],
                    )
                except (CompileError, ExecError):
                    self.warn(f"native compiler does not support reproducible-build flag {candidate!r}")
                else:
                    supported.append(candidate)
        return tuple(supported)


def _append_unique(arguments, additions):
    result = list(arguments or ())
    result.extend(argument for argument in additions if argument not in result)
    return result


def _complete_windows_abi_config():
    """Fill the release/debug bit omitted by some embeddable Windows Pythons."""
    if sys.platform != "win32":
        return
    import sysconfig

    config_vars = sysconfig.get_config_vars()
    if config_vars.get("Py_DEBUG") is None:
        config_vars["Py_DEBUG"] = int(hasattr(sys, "gettotalrefcount"))


exts = [
    Extension(
        "core.pe._block",
        [
            str(Path("core", "pe", "modules", "block.c")),
            str(Path("core", "pe", "modules", "common.c")),
        ],
        include_dirs=[str(Path("core", "pe", "modules"))],
    ),
    Extension(
        "core.pe._cache",
        [
            str(Path("core", "pe", "modules", "cache.c")),
            str(Path("core", "pe", "modules", "common.c")),
        ],
        include_dirs=[str(Path("core", "pe", "modules"))],
    ),
    Extension("qt.pe._block_qt", [str(Path("qt", "pe", "modules", "block.c"))]),
]

_complete_windows_abi_config()

setup(
    ext_modules=exts,
    license_expression="GPL-3.0-only",
    cmdclass={
        "build_ext": ReproducibleBuildExt,
        "build_py": BuildPyWithRuntimeData,
    },
)
