# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/nedbat/coveragepy/blob/master/NOTICE.txt

"""Base test case class for coverage.py testing."""

from __future__ import annotations

import contextlib
import datetime
import difflib
import glob
import io
import os
import os.path
import random
import re
import shlex
import sys

from types import ModuleType
from typing import (
    Any, Collection, Dict, Iterable, Iterator, List, Mapping, Optional,
    Sequence, Tuple, Union,
)

import coverage
from coverage import Coverage
from coverage.cmdline import CoverageScript
from coverage.data import CoverageData
from coverage.misc import import_local_file
from coverage.types import TArc, TLineNo

from tests.helpers import arcs_to_arcz_repr, arcz_to_arcs, assert_count_equal
from tests.helpers import nice_file, run_command
from tests.mixins import PytestBase, StdStreamCapturingMixin, RestoreModulesMixin, TempDirMixin


# Status returns for the command line.
OK, ERR = 0, 1

# The coverage/tests directory, for all sorts of finding test helping things.
TESTS_DIR = os.path.dirname(__file__)

# Install arguments to pass to pip when reinstalling ourselves.
# Defaults to the top of the source tree, but can be overridden if we need
# some help on certain platforms.
COVERAGE_INSTALL_ARGS = os.getenv("COVERAGE_INSTALL_ARGS", nice_file(TESTS_DIR, ".."))


class CoverageTest(
    StdStreamCapturingMixin,
    RestoreModulesMixin,
    TempDirMixin,
    PytestBase,
):
    """A base class for coverage.py test cases."""

    # Standard unittest setting: show me diffs even if they are very long.
    maxDiff = None

    # Tell newer unittest implementations to print long helpful messages.
    longMessage = True

    # Let stderr go to stderr, pytest will capture it for us.
    show_stderr = True

    def setUp(self) -> None:
        super().setUp()

        # Attributes for getting info about what happened.
        self.last_command_status: Optional[int] = None
        self.last_command_output: Optional[str] = None
        self.last_module_name: Optional[str] = None

    def start_import_stop(
        self,
        cov: Coverage,
        modname: str,
        modfile: Optional[str] = None
    ) -> ModuleType:
        """Start coverage, import a file, then stop coverage.

        `cov` is started and stopped, with an `import_local_file` of
        `modname` in the middle. `modfile` is the file to import as `modname`
        if it isn't in the current directory.

        The imported module is returned.

        """
        with cov.collect():
            # Import the Python file, executing it.
            return import_local_file(modname, modfile)

    def get_report(self, cov: Coverage, squeeze: bool = True, **kwargs: Any) -> str:
        """Get the report from `cov`, and canonicalize it."""
        repout = io.StringIO()
        kwargs.setdefault("show_missing", False)
        cov.report(file=repout, **kwargs)
        report = repout.getvalue().replace('\\', '/')
        print(report)   # When tests fail, it's helpful to see the output
        if squeeze:
            report = re.sub(r" +", " ", report)
        return report

    def get_module_name(self) -> str:
        """Return a random module name to use for this test run."""
        self.last_module_name = 'coverage_test_' + str(random.random())[2:]
        return self.last_module_name

    def _check_arcs(
        self,
        a1: Optional[Iterable[TArc]],
        a2: Optional[Iterable[TArc]],
        arc_type: str,
    ) -> str:
        """Check that the arc lists `a1` and `a2` are equal.

        If they are equal, return empty string. If they are unequal, return
        a string explaining what is different.
        """
        # Make them into multi-line strings so we can see what's going wrong.
        s1 = arcs_to_arcz_repr(a1)
        s2 = arcs_to_arcz_repr(a2)
        if s1 != s2:
            lines1 = s1.splitlines(True)
            lines2 = s2.splitlines(True)
            diff = "".join(difflib.ndiff(lines1, lines2))
            return "\n" + arc_type + " arcs differ: minus is expected, plus is actual\n" + diff
        else:
            return ""

    def check_coverage(
        self,
        text: str,
        lines: Optional[Union[Sequence[TLineNo], Sequence[List[TLineNo]]]] = None,
        missing: Union[str, Sequence[str]] = "",
        report: str = "",
        excludes: Optional[Iterable[str]] = None,
        partials: Iterable[str] = (),
        arcz: Optional[str] = None,
        arcz_missing: Optional[str] = None,
        arcz_unpredicted: Optional[str] = None,
        arcs: Optional[Iterable[TArc]] = None,
        arcs_missing: Optional[Iterable[TArc]] = None,
        arcs_unpredicted: Optional[Iterable[TArc]] = None,
    ) -> Coverage:
        """Check the coverage measurement of `text`.

        The source `text` is run and measured.  `lines` are the line numbers
        that are executable, or a list of possible line numbers, any of which
        could match. `missing` are the lines not executed, `excludes` are
        regexes to match against for excluding lines, and `report` is the text
        of the measurement report.

        For arc measurement, `arcz` is a string that can be decoded into arcs
        in the code (see `arcz_to_arcs` for the encoding scheme).
        `arcz_missing` are the arcs that are not executed, and
        `arcz_unpredicted` are the arcs executed in the code, but not deducible
        from the code.  These last two default to "", meaning we explicitly
        check that there are no missing or unpredicted arcs.

        Returns the Coverage object, in case you want to poke at it some more.

        """
        __tracebackhide__ = True    # pytest, please don't show me this function.

        # We write the code into a file so that we can import it.
        # Coverage.py wants to deal with things as modules with file names.
        modname = self.get_module_name()

        self.make_file(modname + ".py", text)

        if arcs is None and arcz is not None:
            arcs = arcz_to_arcs(arcz)
        if arcs_missing is None and arcz_missing is not None:
            arcs_missing = arcz_to_arcs(arcz_missing)
        if arcs_unpredicted is None and arcz_unpredicted is not None:
            arcs_unpredicted = arcz_to_arcs(arcz_unpredicted)

        # Start up coverage.py.
        cov = coverage.Coverage(branch=True)
        cov.erase()
        for exc in excludes or []:
            cov.exclude(exc)
        for par in partials or []:
            cov.exclude(par, which='partial')

        mod = self.start_import_stop(cov, modname)

        # Clean up our side effects
        del sys.modules[modname]

        # Get the analysis results, and check that they are right.
        analysis = cov._analyze(mod)
        statements = sorted(analysis.statements)
        if lines is not None and len(lines) != 0:
            if isinstance(lines[0], int):
                # lines is just a list of numbers, it must match the statements
                # found in the code.
                assert statements == lines, f"lines: {statements!r} != {lines!r}"
            else:
                # lines is a list of possible line number lists, one of them
                # must match.
                for line_list in lines:
                    if statements == line_list:
                        break
                else:
                    assert False, f"None of the lines choices matched {statements!r}"

            missing_formatted = analysis.missing_formatted()
            if isinstance(missing, str):
                msg = f"missing: {missing_formatted!r} != {missing!r}"
                assert missing_formatted == missing, msg
            else:
                for missing_list in missing:
                    if missing_formatted == missing_list:
                        break
                else:
                    assert False, f"None of the missing choices matched {missing_formatted!r}"

        if arcs is not None:
            # print("Possible arcs:")
            # print(" expected:", arcs)
            # print(" actual:", analysis.arc_possibilities())
            # print("Executed:")
            # print(" actual:", sorted(set(analysis.arcs_executed())))
            # TODO: this would be nicer with pytest-check, once we can run that.
            msg = (
                self._check_arcs(arcs, analysis.arc_possibilities(), "Possible") +
                self._check_arcs(arcs_missing, analysis.arcs_missing(), "Missing") +
                self._check_arcs(arcs_unpredicted, analysis.arcs_unpredicted(), "Unpredicted")
            )
            if msg:
                assert False, msg

        if report:
            frep = io.StringIO()
            cov.report(mod, file=frep, show_missing=True)
            rep = " ".join(frep.getvalue().split("\n")[2].split()[1:])
            assert report == rep, f"{report!r} != {rep!r}"

        return cov

    def make_data_file(
        self,
        basename: Optional[str] = None,
        suffix: Optional[str] = None,
        lines: Optional[Mapping[str, Collection[TLineNo]]] = None,
        arcs: Optional[Mapping[str, Collection[TArc]]] = None,
        file_tracers: Optional[Mapping[str, str]] = None,
    ) -> CoverageData:
        """Write some data into a coverage data file."""
        data = coverage.CoverageData(basename=basename, suffix=suffix)
        assert lines is None or arcs is None
        if lines:
            data.add_lines(lines)
        if arcs:
            data.add_arcs(arcs)
        if file_tracers:
            data.add_file_tracers(file_tracers)
        data.write()
        return data

    @contextlib.contextmanager
    def assert_warnings(
        self,
        cov: Coverage,
        warnings: Iterable[str],
        not_warnings: Iterable[str] = (),
    ) -> Iterator[None]:
        """A context manager to check that particular warnings happened in `cov`.

        `cov` is a Coverage instance.  `warnings` is a list of regexes.  Every
        regex must match a warning that was issued by `cov`.  It is OK for
        extra warnings to be issued by `cov` that are not matched by any regex.
        Warnings that are disabled are still considered issued by this function.

        `not_warnings` is a list of regexes that must not appear in the
        warnings.  This is only checked if there are some positive warnings to
        test for in `warnings`.

        If `warnings` is empty, then `cov` is not allowed to issue any
        warnings.

        """
        __tracebackhide__ = True
        saved_warnings = []
        def capture_warning(
            msg: str,
            slug: Optional[str] = None,
            once: bool = False,                 # pylint: disable=unused-argument
        ) -> None:
            """A fake implementation of Coverage._warn, to capture warnings."""
            # NOTE: we don't implement `once`.
            if slug:
                msg = f"{msg} ({slug})"
            saved_warnings.append(msg)

        original_warn = cov._warn
        cov._warn = capture_warning             # type: ignore[method-assign]

        try:
            yield
        except:                     # pylint: disable=try-except-raise
            raise
        else:
            if warnings:
                for warning_regex in warnings:
                    for saved in saved_warnings:
                        if re.search(warning_regex, saved):
                            break
                    else:
                        msg = f"Didn't find warning {warning_regex!r} in {saved_warnings!r}"
                        assert False, msg
                for warning_regex in not_warnings:
                    for saved in saved_warnings:
                        if re.search(warning_regex, saved):
                            msg = f"Found warning {warning_regex!r} in {saved_warnings!r}"
                            assert False, msg
            else:
                # No warnings expected. Raise if any warnings happened.
                if saved_warnings:
                    assert False, f"Unexpected warnings: {saved_warnings!r}"
        finally:
            cov._warn = original_warn           # type: ignore[method-assign]

    def assert_same_files(self, flist1: Iterable[str], flist2: Iterable[str]) -> None:
        """Assert that `flist1` and `flist2` are the same set of file names."""
        flist1_nice = [nice_file(f) for f in flist1]
        flist2_nice = [nice_file(f) for f in flist2]
        assert_count_equal(flist1_nice, flist2_nice)

    def assert_exists(self, fname: str) -> None:
        """Assert that `fname` is a file that exists."""
        assert os.path.exists(fname), f"File {fname!r} should exist"

    def assert_doesnt_exist(self, fname: str) -> None:
        """Assert that `fname` is a file that doesn't exist."""
        assert not os.path.exists(fname), f"File {fname!r} shouldn't exist"

    def assert_file_count(self, pattern: str, count: int) -> None:
        """Assert that there are `count` files matching `pattern`."""
        files = sorted(glob.glob(pattern))
        msg = "There should be {} files matching {!r}, but there are these: {}"
        msg = msg.format(count, pattern, files)
        assert len(files) == count, msg

    def assert_recent_datetime(
        self,
        dt: datetime.datetime,
        seconds: int = 10,
        msg: Optional[str] = None,
    ) -> None:
        """Assert that `dt` marks a time at most `seconds` seconds ago."""
        age = datetime.datetime.now() - dt
        assert age.total_seconds() >= 0, msg
        assert age.total_seconds() <= seconds, msg

    def command_line(self, args: str, ret: int = OK) -> None:
        """Run `args` through the command line.

        Use this when you want to run the full coverage machinery, but in the
        current process.  Exceptions may be thrown from deep in the code.
        Asserts that `ret` is returned by `CoverageScript.command_line`.

        Compare with `run_command`.

        Returns None.

        """
        ret_actual = command_line(args)
        assert ret_actual == ret, f"{ret_actual!r} != {ret!r}"

    # Some distros rename the coverage command, and need a way to indicate
    # their new command name to the tests. This is here for them to override,
    # for example:
    # https://salsa.debian.org/debian/pkg-python-coverage/-/blob/master/debian/patches/02.rename-public-programs.patch
    coverage_command = "coverage"

    def run_command(self, cmd: str) -> str:
        """Run the command-line `cmd` in a sub-process.

        `cmd` is the command line to invoke in a sub-process. Returns the
        combined content of `stdout` and `stderr` output streams from the
        sub-process.

        See `run_command_status` for complete semantics.

        Use this when you need to test the process behavior of coverage.

        Compare with `command_line`.

        """
        _, output = self.run_command_status(cmd)
        return output

    def run_command_status(self, cmd: str) -> Tuple[int, str]:
        """Run the command-line `cmd` in a sub-process, and print its output.

        Use this when you need to test the process behavior of coverage.

        Compare with `command_line`.

        Handles the following command names specially:

        * "python" is replaced with the command name of the current
            Python interpreter.

        * "coverage" is replaced with the command name for the main
            coverage.py program.

        Returns a pair: the process' exit status and its stdout/stderr text,
        which are also stored as `self.last_command_status` and
        `self.last_command_output`.

        """
        # Make sure "python" and "coverage" mean specifically what we want
        # them to mean.
        split_commandline = cmd.split()
        command_name = split_commandline[0]
        command_args = split_commandline[1:]

        if command_name == "python":
            # Running a Python interpreter in a sub-processes can be tricky.
            # Use the real name of our own executable. So "python foo.py" might
            # get executed as "python3.3 foo.py". This is important because
            # Python 3.x doesn't install as "python", so you might get a Python
            # 2 executable instead if you don't use the executable's basename.
            command_words = [os.path.basename(sys.executable)]

        elif command_name == "coverage":
            # The invocation requests the coverage.py program.  Substitute the
            # actual coverage.py main command name.
            command_words = [self.coverage_command]

        else:
            command_words = [command_name]

        cmd = " ".join([shlex.quote(w) for w in command_words] + command_args)

        self.last_command_status, self.last_command_output = run_command(cmd)
        print(self.last_command_output)
        return self.last_command_status, self.last_command_output

    def add_test_modules_to_pythonpath(self) -> None:
        """Add our test modules directory to PYTHONPATH."""
        # Check that there isn't already a PYTHONPATH.
        assert os.getenv("PYTHONPATH") is None
        testmods = nice_file(self.working_root(), "tests/modules")
        zipfile = nice_file(self.working_root(), "tests/zipmods.zip")
        self.set_environ("PYTHONPATH", testmods + os.pathsep + zipfile)

    def working_root(self) -> str:
        """Where is the root of the coverage.py working tree?"""
        return os.path.dirname(nice_file(__file__, ".."))

    def report_from_command(self, cmd: str) -> str:
        """Return the report from the `cmd`, with some convenience added."""
        report = self.run_command(cmd).replace('\\', '/')
        assert "error" not in report.lower()
        return report

    def report_lines(self, report: str) -> List[str]:
        """Return the lines of the report, as a list."""
        lines = report.split('\n')
        assert lines[-1] == ""
        return lines[:-1]

    def line_count(self, report: str) -> int:
        """How many lines are in `report`?"""
        return len(self.report_lines(report))

    def squeezed_lines(self, report: str) -> List[str]:
        """Return a list of the lines in report, with the spaces squeezed."""
        lines = self.report_lines(report)
        return [re.sub(r"\s+", " ", l.strip()) for l in lines]

    def last_line_squeezed(self, report: str) -> str:
        """Return the last line of `report` with the spaces squeezed down."""
        return self.squeezed_lines(report)[-1]

    def get_measured_filenames(self, coverage_data: CoverageData) -> Dict[str, str]:
        """Get paths to measured files.

        Returns a dict of {filename: absolute path to file}
        for given CoverageData.
        """
        return {os.path.basename(filename): filename
                for filename in coverage_data.measured_files()}

    def get_missing_arc_description(self, cov: Coverage, start: TLineNo, end: TLineNo) -> str:
        """Get the missing-arc description for a line arc in a coverage run."""
        # ugh, unexposed methods??
        assert self.last_module_name is not None
        filename = self.last_module_name + ".py"
        fr = cov._get_file_reporter(filename)
        arcs_executed = cov._analyze(filename).arcs_executed()
        return fr.missing_arc_description(start, end, arcs_executed)


class UsingModulesMixin:
    """A mixin for importing modules from tests/modules and tests/moremodules."""

    def setUp(self) -> None:
        super().setUp()             # type: ignore[misc]

        # Parent class saves and restores sys.path, we can just modify it.
        sys.path.append(nice_file(TESTS_DIR, "modules"))
        sys.path.append(nice_file(TESTS_DIR, "moremodules"))
        sys.path.append(nice_file(TESTS_DIR, "zipmods.zip"))


def command_line(args: str) -> int:
    """Run `args` through the CoverageScript command line.

    Returns the return code from CoverageScript.command_line.

    """
    script = CoverageScript()
    ret = script.command_line(shlex.split(args))
    return ret
