"""Generic Python code generation utilities. Implemented on demand."""

from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime

from styx.pycodegen.utils import linebreak_paragraph

LineBuffer = list[str]
INDENT = "    "


def indent(lines: LineBuffer, level: int = 1) -> LineBuffer:
    """Indent a LineBuffer by a given level."""
    if level == 0:
        return lines
    return [f"{INDENT * level}{line}" for line in lines]


def comment(lines: LineBuffer) -> LineBuffer:
    """Add a comment to a LineBuffer."""
    return [f"# {line}" for line in lines]


def collapse(lines: LineBuffer) -> str:
    """Collapse a LineBuffer into a single string."""
    return "\n".join(lines)


def expand(text: str) -> LineBuffer:
    """Expand a string into a LineBuffer."""
    return text.splitlines()


def concat(line_buffers: list[LineBuffer]) -> LineBuffer:
    """Concatenate multiple LineBuffers."""
    return [line for buf in line_buffers for line in buf]


def blank_before(lines: LineBuffer, blanks: int = 1) -> LineBuffer:
    """Add blank lines at the beginning of a LineBuffer if it is not empty."""
    return [*([""] * blanks), *lines] if len(lines) > 0 else lines


def blank_after(lines: LineBuffer, blanks: int = 1) -> LineBuffer:
    """Add blank lines at the end of a LineBuffer if it is not empty."""
    return [*lines, *([""] * blanks)] if len(lines) > 0 else lines


class PyGen(ABC):
    def generate(self) -> LineBuffer:
        """Generate the code."""
        raise NotImplementedError

    def text(self) -> str:
        """Generate the code and collapse it into a single string."""
        return collapse(self.generate())


@dataclass
class PyArg:
    """Python function argument."""

    name: str
    type: str
    default: str | None
    docstring: str


@dataclass
class PyFunc(PyGen):
    """Python function."""

    name: str
    args: list[PyArg]
    docstring_body: str
    body: LineBuffer
    return_descr: str
    return_type: str | None = None

    def generate(self) -> LineBuffer:
        buf = []

        # Sort arguments so default arguments come last
        self.args.sort(key=lambda a: a.default is not None)

        # Function signature
        buf.append(f"def {self.name}(")

        # Add arguments
        for arg in self.args:
            if arg.default is None:
                buf.extend(indent([f"{arg.name}: {arg.type},"]))
            else:
                buf.extend(indent([f"{arg.name}: {arg.type} = {arg.default},"]))
        buf.append(f") -> {self.return_type}:")

        arg_docstr_buf = []
        for arg in self.args:
            arg_docstr = linebreak_paragraph(
                f"{arg.name}: {arg.docstring}", width=80 - 12 - (len(arg.name) + 2), first_line_width=80 - 8
            )
            arg_docstr_buf.append(arg_docstr[0])
            arg_docstr_buf.extend(indent(arg_docstr[1:]))

        # Add docstring (Google style)
        buf.extend(
            indent([
                '"""',
                f"{self.docstring_body}",
                "",
                "Args:",
                *indent(arg_docstr_buf),
                "Returns:",
                *indent([f"{self.return_descr}"]),
                '"""',
            ])
        )

        # Add function body
        buf.extend(indent(self.body))
        return buf


@dataclass
class PyModule(PyGen):
    """Python module."""

    imports: LineBuffer = field(default_factory=list)
    header: LineBuffer = field(default_factory=list)
    funcs: list[PyFunc] = field(default_factory=list)
    footer: LineBuffer = field(default_factory=list)

    def generate(self) -> LineBuffer:
        timestamp = datetime.now().isoformat()
        return blank_after([
            *comment([
                "This file was auto generated by styx-boutiques-codegen",
                "Do not edit this file directly",
                f"Timestamp: {timestamp}",
            ]),
            *blank_before(self.imports),
            *blank_before(self.header),
            *[line for func in self.funcs for line in blank_before(func.generate(), 2)],
            *blank_before(self.footer),
        ])
